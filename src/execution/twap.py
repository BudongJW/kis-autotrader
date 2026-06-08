"""TWAP (Time-Weighted Average Price) 분할 주문 엔진.

대량 주문을 3~5개 트랜치로 분할하여 시장 충격(market impact)을 최소화한다.
루프 모드에서 1분 간격으로 트랜치를 순차 실행.

사용법:
    engine = TWAPEngine()

    # 매수 주문 등록
    engine.submit("005930", total_qty=100, side="buy", name="삼성전자")

    # 매 루프마다 실행
    executed = engine.tick(client, dry_run=False)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils.logger import log

TWAP_STATE_PATH = Path("logs/twap_state.json")

# 분할 설정
MIN_TRANCHES = 3
MAX_TRANCHES = 5
MIN_QTY_PER_TRANCHE = 1          # 최소 1주
TRANCHE_INTERVAL_SEC = 60        # 트랜치 간 간격 (1분)
SMALL_ORDER_THRESHOLD = 50_000   # 5만원 이하 → 분할 없이 일괄


@dataclass
class Tranche:
    """개별 트랜치 정보."""
    qty: int
    executed: bool = False
    fill_price: int = 0
    executed_at: str = ""


@dataclass
class TWAPOrder:
    """TWAP 분할 주문."""
    symbol: str
    side: str              # "buy" / "sell"
    name: str
    total_qty: int
    signal_price: int      # 주문 시점 시그널 가격 (슬리피지 추적용)
    reason: str = ""       # AI 판단 근거 (왜 샀나/팔았나) — 거래기록에 전달
    tranches: list[Tranche] = field(default_factory=list)
    created_at: str = ""
    last_tranche_time: float = 0.0  # epoch

    @property
    def remaining_qty(self) -> int:
        return sum(t.qty for t in self.tranches if not t.executed)

    @property
    def executed_qty(self) -> int:
        return sum(t.qty for t in self.tranches if t.executed)

    @property
    def is_complete(self) -> bool:
        return all(t.executed for t in self.tranches)

    @property
    def avg_fill_price(self) -> float:
        fills = [(t.qty, t.fill_price) for t in self.tranches if t.executed and t.fill_price > 0]
        if not fills:
            return 0.0
        total_cost = sum(q * p for q, p in fills)
        total_qty = sum(q for q, _ in fills)
        return total_cost / total_qty if total_qty > 0 else 0.0

    @property
    def slippage_bps(self) -> float:
        """슬리피지 (bps). 양수 = 불리한 체결."""
        avg = self.avg_fill_price
        if avg <= 0 or self.signal_price <= 0:
            return 0.0
        if self.side == "buy":
            return (avg - self.signal_price) / self.signal_price * 10000
        else:  # sell
            return (self.signal_price - avg) / self.signal_price * 10000


def _calc_num_tranches(total_qty: int, price: int) -> int:
    """주문 규모에 따라 트랜치 수 결정."""
    order_value = total_qty * price
    if order_value <= SMALL_ORDER_THRESHOLD:
        return 1  # 소액은 분할 불필요
    if total_qty < MIN_TRANCHES:
        return total_qty  # 주수가 적으면 1주씩
    if order_value < 200_000:
        return MIN_TRANCHES
    if order_value < 500_000:
        return 4
    return MAX_TRANCHES


def _split_qty(total_qty: int, num_tranches: int) -> list[int]:
    """수량을 균등 분할. 나머지는 첫 트랜치에 추가."""
    if num_tranches <= 1:
        return [total_qty]
    base = total_qty // num_tranches
    remainder = total_qty % num_tranches
    result = [base + (1 if i < remainder else 0) for i in range(num_tranches)]
    return [q for q in result if q >= MIN_QTY_PER_TRANCHE]


class TWAPEngine:
    """TWAP 분할 주문 관리."""

    def __init__(self) -> None:
        self.orders: list[TWAPOrder] = []
        self._load_state()

    def _load_state(self) -> None:
        """디스크에서 미완료 주문 복원. 전일 주문은 폐기."""
        if not TWAP_STATE_PATH.exists():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with TWAP_STATE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for od in data:
                tranches = [Tranche(**t) for t in od.pop("tranches", [])]
                order = TWAPOrder(**od, tranches=tranches)
                # 전일 주문은 폐기 (날짜 넘김 방지)
                if not order.created_at.startswith(today):
                    log.info("twap_stale_order_discarded", symbol=order.symbol,
                             created=order.created_at)
                    continue
                if not order.is_complete:
                    self.orders.append(order)
        except Exception as e:
            log.error("twap_load_failed", error=str(e))

    def _save_state(self) -> None:
        """미완료 주문을 디스크에 저장."""
        TWAP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for o in self.orders:
            d = {
                "symbol": o.symbol,
                "side": o.side,
                "name": o.name,
                "total_qty": o.total_qty,
                "signal_price": o.signal_price,
                "reason": o.reason,
                "created_at": o.created_at,
                "last_tranche_time": o.last_tranche_time,
                "tranches": [asdict(t) for t in o.tranches],
            }
            data.append(d)
        with TWAP_STATE_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def has_pending(self, symbol: str | None = None) -> bool:
        """미완료 주문 존재 여부."""
        if symbol:
            return any(not o.is_complete and o.symbol == symbol for o in self.orders)
        return any(not o.is_complete for o in self.orders)

    def submit(self, symbol: str, total_qty: int, side: str,
               name: str, signal_price: int, reason: str = "") -> TWAPOrder:
        """TWAP 주문 등록."""
        # 동일 종목 기존 주문 제거
        self.orders = [o for o in self.orders if not (o.symbol == symbol and o.side == side)]

        num = _calc_num_tranches(total_qty, signal_price)
        qtys = _split_qty(total_qty, num)
        tranches = [Tranche(qty=q) for q in qtys]

        order = TWAPOrder(
            symbol=symbol,
            side=side,
            name=name,
            total_qty=total_qty,
            signal_price=signal_price,
            reason=reason,
            tranches=tranches,
            created_at=datetime.now().isoformat(timespec="seconds"),
            last_tranche_time=0.0,
        )
        self.orders.append(order)
        self._save_state()

        print(f"  [TWAP] {side.upper()} {name}({symbol}) "
              f"{total_qty}주 → {len(tranches)}트랜치 분할")
        return order

    def tick(self, client: Any, dry_run: bool = False) -> list[dict]:
        """1회 틱: 실행 가능한 트랜치를 처리.

        Returns:
            실행된 트랜치 정보 리스트
        """
        from src.tracker import log_trade
        from src.risk_manager import record_buy, remove_position

        now = time.time()
        executed = []

        # 동시호가 시간 진입 시 매수 트랜치 차단 (15:15 이후 신규 매수 거부됨)
        # KIS는 15:20부터 동시호가 처리. 15:15 이후 매수 주문은 거부될 가능성 큼.
        # 5-27 사례: 13:48 매수 결정 → TWAP 분할 → 15:20:34 트랜치 실행 → KIS 거부.
        from datetime import datetime, time as dtime
        from zoneinfo import ZoneInfo
        _kst = ZoneInfo("Asia/Seoul")
        _now_t = datetime.now(_kst).time()
        _CUTOFF = dtime(15, 15)
        _block_buy_late = _now_t >= _CUTOFF

        for order in self.orders:
            if order.is_complete:
                continue

            # 15:15 이후 매수 트랜치 자동 차단 (KIS 동시호가 거부 회피)
            if _block_buy_late and order.side == "buy":
                # 남은 트랜치를 강제 완료 처리 (skip)
                pending = [t for t in order.tranches if not t.executed]
                if pending:
                    print(f"  [TWAP] {order.name} 매수 트랜치 {len(pending)}개 "
                          f"15:15 이후 자동 SKIP (동시호가 거부 회피)")
                    log.warning("twap_late_buy_blocked",
                                symbol=order.symbol, pending=len(pending))
                    for t in pending:
                        t.executed = True  # skip 처리 (완료로 마킹)
                continue

            # 트랜치 간격 체크
            if now - order.last_tranche_time < TRANCHE_INTERVAL_SEC:
                continue

            # 다음 미실행 트랜치 찾기
            tranche = next((t for t in order.tranches if not t.executed), None)
            if not tranche:
                continue

            # 현재 가격 확인
            from src.bot.single_run import get_price
            current_price = get_price(client, order.symbol)
            if current_price <= 0:
                continue

            tranche_idx = order.tranches.index(tranche) + 1
            total_tranches = len(order.tranches)
            print(f"  [TWAP] {order.side.upper()} {order.name} "
                  f"트랜치 {tranche_idx}/{total_tranches}: "
                  f"{tranche.qty}주 @ ~{current_price:,}원")

            if not dry_run:
                # 사전 안전장치 (killswitch, 가격, 블랙리스트 등)
                from src.safety.order_gates import check_order
                gate_ok, gate_reason = check_order(
                    order.symbol, tranche.qty, current_price, order.side
                )
                if not gate_ok:
                    print(f"    ⚠️ TWAP 주문 차단: {gate_reason}")
                    log.warning("twap_gate_blocked",
                                symbol=order.symbol, reason=gate_reason)
                    continue
                resp = client.order_cash(
                    order.symbol, qty=tranche.qty, side=order.side,
                    price=current_price,
                )
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "E":
                    log.warning("twap_order_error",
                                symbol=order.symbol, msg=resp.get("msg1", ""))
                    continue  # HTTP 에러 → 다음 틱에서 재시도
                if rt == "0":
                    tranche.executed = True
                    tranche.fill_price = current_price
                    tranche.executed_at = datetime.now().isoformat(timespec="seconds")
                    order.last_tranche_time = now

                    log_trade(order.symbol, order.name, order.side,
                              tranche.qty, current_price,
                              reason=order.reason or f"{order.side} (TWAP 분할체결)")

                    if order.side == "buy":
                        record_buy(order.symbol, current_price, tranche.qty)

                    executed.append({
                        "symbol": order.symbol,
                        "side": order.side,
                        "qty": tranche.qty,
                        "price": current_price,
                        "tranche": f"{tranche_idx}/{total_tranches}",
                    })
                else:
                    log.warning("twap_tranche_failed",
                                symbol=order.symbol, rt_cd=rt)
            else:
                tranche.executed = True
                tranche.fill_price = current_price
                tranche.executed_at = datetime.now().isoformat(timespec="seconds")
                order.last_tranche_time = now
                print("    (dry-run)")
                executed.append({
                    "symbol": order.symbol,
                    "side": order.side,
                    "qty": tranche.qty,
                    "price": current_price,
                    "tranche": f"{tranche_idx}/{total_tranches}",
                })

            # 주문 완료 시 슬리피지 리포트
            if order.is_complete:
                slippage = order.slippage_bps
                avg_fill = order.avg_fill_price
                print(f"  [TWAP] {order.name} 완료: "
                      f"평균 체결가 {avg_fill:,.0f}원 | "
                      f"시그널가 {order.signal_price:,}원 | "
                      f"슬리피지 {slippage:+.1f}bps")

                # 슬리피지 로그 저장
                _log_slippage(order)

                if order.side == "sell":
                    remove_position(order.symbol)

        # 완료된 주문 제거
        self.orders = [o for o in self.orders if not o.is_complete]
        self._save_state()
        return executed

    def cancel_all(self, symbol: str | None = None) -> None:
        """미완료 주문 취소."""
        if symbol:
            self.orders = [o for o in self.orders if o.symbol != symbol]
        else:
            self.orders = []
        self._save_state()

    def get_pending_summary(self) -> str:
        """미완료 주문 요약."""
        if not self.orders:
            return "대기 주문 없음"
        lines = []
        for o in self.orders:
            lines.append(f"  {o.side.upper()} {o.name}: "
                         f"{o.executed_qty}/{o.total_qty}주 완료")
        return "\n".join(lines)


def _log_slippage(order: TWAPOrder) -> None:
    """슬리피지 데이터를 파일에 기록."""
    slippage_path = Path("logs/slippage.json")
    slippage_path.parent.mkdir(parents=True, exist_ok=True)

    records = []
    if slippage_path.exists():
        try:
            with slippage_path.open("r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception:
            records = []

    records.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "symbol": order.symbol,
        "name": order.name,
        "side": order.side,
        "total_qty": order.total_qty,
        "signal_price": order.signal_price,
        "avg_fill_price": round(order.avg_fill_price),
        "slippage_bps": round(order.slippage_bps, 1),
        "num_tranches": len(order.tranches),
    })

    # 최근 500건만 유지
    records = records[-500:]
    with slippage_path.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
