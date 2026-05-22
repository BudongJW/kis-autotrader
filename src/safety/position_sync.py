"""포지션 자동 동기화 — KIS 잔고와 내부 positions.json 일치화.

기업행동 (액면분할·배당락·유상증자) 발생 시 KIS는 잔고 조회 응답에서
자동으로 수량·평단가를 보정한다. 봇은 KIS 잔고를 신뢰하고 internal
positions.json을 자동 동기화하면 됨.

봇 시작 시 호출:
  from src.safety.position_sync import sync_from_broker
  changes = sync_from_broker(client)
  if changes:
      # 이벤트 로그 + 알림

비교 항목:
  - qty 차이      → 액면분할(상승) / 배당(미변동) / 매수·매도(체결 후)
  - avg_price 차이 → 액면분할 시 단가 자동 조정
  - 새로운 종목   → 사용자 수동 매수 (broker_only)
  - 사라진 종목   → 사용자 수동 매도 (ledger_only)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.kis_client import KISClient
from src.risk_manager import load_positions, save_positions
from src.utils.logger import log


def _fetch_broker_holdings(client: KISClient) -> dict[str, dict[str, Any]]:
    """KIS get_balance에서 보유 종목 dict로 추출."""
    result = {}
    try:
        resp = client.get_balance()
        if resp.get("rt_cd") != "0":
            return result
        for item in resp.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty <= 0:
                continue
            sym = item.get("pdno", "")
            result[sym] = {
                "qty": qty,
                "avg_price": float(item.get("pchs_avg_pric", 0)),
                "current_price": int(item.get("prpr", 0)),
                "name": item.get("prdt_name", sym),
            }
    except Exception as e:
        log.error("position_sync_fetch_failed", error=str(e))
    return result


def sync_from_broker(client: KISClient, market: str = "KR") -> dict[str, list]:
    """KIS 잔고 → internal positions.json 동기화.

    Returns:
        {
            'qty_changed': [(symbol, old_qty, new_qty)],          # 액분 등
            'price_changed': [(symbol, old_avg, new_avg)],        # 배당 조정
            'new_in_broker': [(symbol, qty, avg)],                 # 사용자 수동 매수
            'removed_from_broker': [(symbol, old_qty)],            # 사용자 수동 매도
        }
    """
    changes: dict[str, list] = {
        "qty_changed": [],
        "price_changed": [],
        "new_in_broker": [],
        "removed_from_broker": [],
    }

    broker = _fetch_broker_holdings(client)
    internal = load_positions()  # {symbol: {buy_price, qty, atr_at_buy, ...}}

    all_symbols = set(broker.keys()) | set(internal.keys())

    for sym in all_symbols:
        bk = broker.get(sym)
        it = internal.get(sym)

        if bk and not it:
            # 사용자 수동 매수 — 봇 positions에 없는 종목이 broker에 있음
            changes["new_in_broker"].append((sym, bk["qty"], bk["avg_price"]))
            # 봇이 자동 관리하지 않도록 internal에 추가하지 않음 (manual로 간주)
            continue

        if it and not bk:
            # broker에서 사라짐 — 사용자가 수동 매도했거나 봇이 매도 후 미동기화
            changes["removed_from_broker"].append((sym, it.get("qty", 0)))
            # internal에서 제거 (이미 broker엔 없으므로)
            del internal[sym]
            continue

        if bk and it:
            old_qty = int(it.get("qty", 0))
            new_qty = bk["qty"]
            old_avg = float(it.get("buy_price", 0))
            new_avg = bk["avg_price"]

            # 수량 차이 (액면분할 등)
            if old_qty != new_qty:
                changes["qty_changed"].append((sym, old_qty, new_qty))
                it["qty"] = new_qty

            # 평단가 차이 (액면분할로 인한 단가 조정 또는 배당락 보정)
            if old_avg > 0 and abs(new_avg - old_avg) / old_avg > 0.001:
                changes["price_changed"].append((sym, old_avg, new_avg))
                it["buy_price"] = new_avg
                # peak_price도 조정 (액분 비율 추정)
                if old_qty > 0 and new_qty > 0:
                    ratio = old_qty / new_qty  # 액분 1→2 시 ratio=0.5
                    if "peak_price" in it:
                        it["peak_price"] = it["peak_price"] * ratio
                    if "atr_at_buy" in it:
                        it["atr_at_buy"] = it["atr_at_buy"] * ratio

    save_positions(internal)

    # 변경 사항이 있으면 ledger 이벤트 + 알림
    has_changes = any(changes.values())
    if has_changes:
        try:
            from src.safety.ledger import log_event
            log_event("position_sync", "info", {
                "market": market,
                "summary": {k: len(v) for k, v in changes.items()},
                "details": {
                    "qty_changed": [{"sym": s, "old": o, "new": n}
                                    for s, o, n in changes["qty_changed"]],
                    "price_changed": [{"sym": s, "old": round(o, 2), "new": round(n, 2)}
                                       for s, o, n in changes["price_changed"]],
                    "new_in_broker": [{"sym": s, "qty": q, "avg": round(a, 2)}
                                      for s, q, a in changes["new_in_broker"]],
                    "removed_from_broker": [{"sym": s, "old_qty": q}
                                            for s, q in changes["removed_from_broker"]],
                },
            })
        except Exception:
            pass

        # 액면분할 등 중요 변경은 텔레그램 알림
        if changes["qty_changed"] or changes["price_changed"]:
            try:
                from src.safety.notifier import _send
                lines = ["<b>📋 포지션 동기화</b>"]
                for sym, old, new in changes["qty_changed"]:
                    lines.append(f"  {sym}: {old}주 → {new}주 (액분 추정)")
                for sym, old, new in changes["price_changed"]:
                    lines.append(f"  {sym}: 평단 ₩{old:,.0f} → ₩{new:,.0f}")
                _send("\n".join(lines))
            except Exception:
                pass

    return changes
