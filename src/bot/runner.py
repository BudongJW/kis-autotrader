"""실시간 봇 러너 — 변동성 돌파 전략 + 안전장치.

사용:
    # dry-run (신호만 확인, 주문 안 보냄)
    python -m src.bot.runner --strategy volatility_breakout --symbol 005930 --live --dry-run

    # 실전 매매
    python -m src.bot.runner --strategy volatility_breakout --symbol 005930 --live

흐름 (변동성 돌파):
  09:00  전일 보유분 시가 매도
  09:01~ 목표가 돌파 감시 (1분 간격)
  15:20  미매도 포지션 강제 청산
  15:30  장 종료, 봇 대기

안전 원칙:
  - .env MODE=live + --live 플래그 둘 다 일치해야 실전 진입
  - --auto-confirm 없으면 실전 모드 진입 시 사용자 확인 필요
  - 모든 주문·신호를 JSON 로그에 기록
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, time as dtime

import pandas as pd

from src.config import Mode, settings
from src.kis_client import KISClient
from src.strategies.base import BaseStrategy, SignalType
from src.strategies.golden_cross import GoldenCrossStrategy
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.utils.logger import log

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "golden_cross": GoldenCrossStrategy,
    "volatility_breakout": VolatilityBreakoutStrategy,
}

POLL_INTERVAL_SEC = 60  # 1분마다 시세 확인

# 한국 장 시간
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 20)  # 15:30 정규 마감 전 여유
MARKET_END = dtime(15, 30)


def is_market_hours() -> bool:
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_END


def confirm_live_mode() -> bool:
    print("\n" + "=" * 60)
    print("  LIVE MODE - 실전 계좌로 매매합니다.")
    print(f"   계좌:       {settings.account_full}")
    print(f"   Base URL:   {settings.base_url}")
    print("=" * 60)
    answer = input("정말로 실전 모드로 실행하시겠습니까? (yes): ").strip()
    return answer == "yes"


def fetch_recent_history(client: KISClient, symbol: str, days: int = 30) -> pd.DataFrame:
    resp = client.get_daily_price(symbol)
    if resp.get("rt_cd") != "0":
        log.error("daily_price_failed", resp=resp)
        raise RuntimeError(f"일봉 조회 실패: {resp.get('msg1')}")

    rows = resp.get("output", [])
    if not rows:
        raise RuntimeError("일봉 데이터 비어있음")

    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "stck_bsop_date": "date", "stck_oprc": "open",
        "stck_hgpr": "high", "stck_lwpr": "low",
        "stck_clpr": "close", "acml_vol": "volume",
    })
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").set_index("date")
    return df[["open", "high", "low", "close", "volume"]].tail(days)


def get_holding_qty(client: KISClient, symbol: str) -> int:
    """해당 종목 보유 수량 조회."""
    try:
        resp = client.get_balance()
        if resp.get("rt_cd") != "0":
            return 0
        for item in resp.get("output1", []):
            if item.get("pdno") == symbol:
                return int(item.get("hldg_qty", 0))
    except Exception as e:
        log.error("balance_check_failed", error=str(e))
    return 0


def calc_buy_qty(client: KISClient, price: float) -> int:
    """매수 가능 수량 계산.

    ord_psbl_cash(주문가능현금) > 0이면 그 값을 사용하고,
    0이면 dnca_tot_amt(예수금총액)을 대신 사용한다.
    D+2 결제 등으로 주문가능현금이 아직 반영 안 된 경우 대비.
    """
    try:
        resp = client.get_balance()
        if resp.get("rt_cd") != "0":
            return 0
        cash_info = resp.get("output2", [{}])
        if not cash_info:
            return 0
        c = cash_info[0]
        available = int(c.get("ord_psbl_cash", 0))
        if available <= 0:
            available = int(c.get("dnca_tot_amt", 0))
        if available <= 0:
            return 0
        # 수수료(0.015%) 감안해서 99.9%까지 사용
        qty = int(available * 0.999 // price)
        log.info("calc_buy_qty", available=available, price=price, qty=qty)
        return max(qty, 0)
    except Exception as e:
        log.error("calc_buy_qty_failed", error=str(e))
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 자동매매 봇")
    parser.add_argument("--strategy", required=True, choices=list(STRATEGY_REGISTRY.keys()))
    parser.add_argument("--symbol", required=True, help="6자리 종목코드")
    parser.add_argument("--live", action="store_true", help="실전 모드")
    parser.add_argument("--dry-run", action="store_true", help="신호만 확인, 주문 안 보냄")
    parser.add_argument("--auto-confirm", action="store_true", help="실전 모드 자동 확인 (스케줄러용)")
    args = parser.parse_args()

    # 안전장치
    if args.live and settings.mode != Mode.LIVE:
        print(f"  .env MODE={settings.mode.value}인데 --live 플래그. 불일치로 종료.")
        sys.exit(1)
    if settings.mode == Mode.LIVE and not args.live:
        print("  .env MODE=live인데 --live 플래그 없음. 종료.")
        sys.exit(1)
    if settings.mode == Mode.LIVE and not args.auto_confirm:
        if not confirm_live_mode():
            print("실전 모드 취소.")
            sys.exit(0)

    strategy = STRATEGY_REGISTRY[args.strategy]()
    client = KISClient()

    log.info("bot_started", strategy=strategy.name, symbol=args.symbol,
             mode=settings.mode.value, dry_run=args.dry_run)
    print(f"[BOT] {strategy.name} | {args.symbol} | mode={settings.mode.value} | dry_run={args.dry_run}")

    holding = False  # 당일 매수 여부 추적
    sold_today = False  # 당일 매도 완료 여부

    try:
        while True:
            now = datetime.now()

            if not is_market_hours():
                # 장외 시간: 상태 초기화 후 대기
                if now.time() > MARKET_END:
                    holding = False
                    sold_today = False
                next_check = 60 if now.time() < MARKET_OPEN else 300
                print(f"[{now:%H:%M:%S}] 장외 시간. {next_check}초 후 재확인.")
                time.sleep(next_check)
                continue

            try:
                # 현재가 조회
                price_resp = client.get_price(args.symbol)
                if price_resp.get("rt_cd") != "0":
                    log.error("price_query_failed", resp=price_resp)
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                cur_price = int(price_resp["output"]["stck_prpr"])

                # ── 09:00~09:05 전일 보유분 매도 ──
                if MARKET_OPEN <= now.time() <= dtime(9, 5) and not sold_today:
                    held_qty = get_holding_qty(client, args.symbol)
                    if held_qty > 0:
                        print(f"[{now:%H:%M:%S}] 전일 보유분 매도: {held_qty}주 @ ~{cur_price:,}원")
                        if not args.dry_run:
                            resp = client.order_cash(args.symbol, qty=held_qty, side="sell")
                            log.info("morning_sell", qty=held_qty, price=cur_price, resp=resp)
                            print(f"  매도 주문 응답: rt_cd={resp.get('rt_cd')}, msg={resp.get('msg1', '')}")
                        else:
                            log.info("morning_sell_dry", qty=held_qty, price=cur_price)
                    sold_today = True

                # ── 장중: 목표가 돌파 감시 ──
                if not holding and now.time() <= MARKET_CLOSE:
                    history = fetch_recent_history(client, args.symbol)
                    signal = strategy.generate_signal(args.symbol, history)

                    log.info("signal", type=signal.type.value, price=signal.price, reason=signal.reason)
                    print(f"[{now:%H:%M:%S}] {signal.type.value} @ {cur_price:,}원 — {signal.reason}")

                    if signal.type == SignalType.BUY:
                        qty = calc_buy_qty(client, cur_price)
                        if qty > 0:
                            print(f"  매수 신호! {qty}주 @ {cur_price:,}원")
                            if not args.dry_run:
                                resp = client.order_cash(args.symbol, qty=qty, side="buy")
                                log.info("buy_order", qty=qty, price=cur_price, resp=resp)
                                print(f"  매수 주문 응답: rt_cd={resp.get('rt_cd')}, msg={resp.get('msg1', '')}")
                                if resp.get("rt_cd") == "0":
                                    holding = True
                            else:
                                log.info("buy_dry", qty=qty, price=cur_price)
                                holding = True
                        else:
                            print(f"  매수 가능 수량 0 (예수금 부족)")

                # ── 15:20 이후: 미매도 포지션 정리 ──
                if now.time() > MARKET_CLOSE and holding:
                    held_qty = get_holding_qty(client, args.symbol)
                    if held_qty > 0:
                        print(f"[{now:%H:%M:%S}] 장 마감 전 청산: {held_qty}주 @ ~{cur_price:,}원")
                        if not args.dry_run:
                            resp = client.order_cash(args.symbol, qty=held_qty, side="sell")
                            log.info("eod_sell", qty=held_qty, price=cur_price, resp=resp)
                    holding = False

            except Exception as e:
                log.exception("loop_error", error=str(e))
                print(f"[{now:%H:%M:%S}] ERROR: {e}")

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        log.info("bot_stopped_by_user")
        print("\n[BOT] 정상 종료.")


if __name__ == "__main__":
    main()
