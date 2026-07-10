"""사용자 지시 수동 청산 — 069500 보유분 전량 시장가 매도 + 저널 기록.

회복 목적 롱 익절 청산. 실전(MODE=live). debug-once: script=scripts.manual_sell
"""
from __future__ import annotations

import time

from src.kis_client import KISClient

SYMBOL = "069500"


def main() -> None:
    c = KISClient()
    bal = c.get_balance()
    held = 0
    avg = 0
    for it in (bal.get("output1") or []):
        if it.get("pdno") == SYMBOL:
            held = int(float(it.get("hldg_qty", 0) or 0))
            avg = int(float(it.get("pchs_avg_pric", 0) or 0))
    if held <= 0:
        print(f"[청산] {SYMBOL} 보유 없음 — 매도할 것 없음(이미 청산됨?).")
        return
    print(f"[청산 전] {SYMBOL} 보유 {held}주, 매입가 {avg:,}")

    resp = c.order_cash(SYMBOL, held, side="sell", order_type="01")  # 시장가
    print(f"[매도주문] rt_cd={resp.get('rt_cd')} msg={resp.get('msg1', '')}")
    if resp.get("rt_cd") != "0":
        print("!! 매도 실패 — 중단 !!")
        return

    time.sleep(2.5)
    bal2 = c.get_balance()
    left = 0
    for it in (bal2.get("output1") or []):
        if it.get("pdno") == SYMBOL:
            left = int(float(it.get("hldg_qty", 0) or 0))
    price = 0
    try:
        pr = c.get_price(SYMBOL)
        price = int(float(pr.get("output", {}).get("stck_prpr", 0) or 0))
    except Exception:
        pass
    print(f"[체결확인] {SYMBOL} 잔여 {left}주 | 매도가 ~{price:,}")
    if price and avg:
        print(f"  손익 추정: {(price-avg)*held:+,}원 ({(price-avg)/avg*100:+.2f}%)")
    try:
        from src.risk_manager import remove_position
        remove_position(SYMBOL)
    except Exception as e:  # noqa: BLE001
        print("  remove_position 경고:", e)
    try:
        from src.tracker import log_trade
        log_trade(SYMBOL, "KODEX 200", "sell", held, (price or avg) * 100, market="KR",
                  reason="사용자 지시 수동 청산(회복 목적 롱 익절)")
    except Exception as e:  # noqa: BLE001
        print("  log_trade 경고:", e)
    print("청산 완료.")


if __name__ == "__main__":
    main()
