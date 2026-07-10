"""사용자 지시 수동 롱 — 069500 3주 시장가 매수 + 체결확인 + 봇 청산관리 등록.

회복 목적. 봇이 손절(-1%)/트레일링/15:00 강제청산으로 관리. 오버나이트 금지.
연결 불안정 대비 중복주문 방지 가드(이미 보유 시 재매수 안 함). 실전 주문(MODE=live).
debug-once: script=scripts.manual_long
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from src.kis_client import KISClient

SYMBOL = "069500"
QTY = 3


def _held(c: KISClient) -> tuple[int, int]:
    """(보유수량, 매입평균가)."""
    bal = c.get_balance()
    for it in (bal.get("output1") or []):
        if it.get("pdno") == SYMBOL:
            q = int(float(it.get("hldg_qty", 0) or 0))
            a = int(float(it.get("pchs_avg_pric", 0) or 0))
            return q, a
    return 0, 0


def _register(avg: int, qty: int) -> None:
    """봇 청산관리 등록: positions.json + trades.csv + morning_positions.json."""
    try:
        from src.risk_manager import record_buy
        record_buy(SYMBOL, avg, qty)
    except Exception as e:  # noqa: BLE001
        print("  record_buy 경고:", e)
    try:
        from src.tracker import log_trade
        log_trade(SYMBOL, "KODEX 200", "buy", qty, avg * 100, market="KR",
                  reason="사용자 지시 수동 롱(회복목적, 봇 손절/트레일 관리, 오버나이트 금지)")
    except Exception as e:  # noqa: BLE001
        print("  log_trade 경고:", e)
    mp = Path("logs/morning_positions.json")
    st = {}
    if mp.exists():
        try:
            st = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            st = {}
    st[SYMBOL] = {"direction": "long", "entry_price": avg, "qty": qty,
                  "name": "KODEX 200", "peak": avg,
                  "date": datetime.now().strftime("%Y-%m-%d")}
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    print("등록완료: 봇 손절 -1% / 트레일링(+1.5%후) / 15:00 강제청산(오버나이트 금지)")


def main() -> None:
    c = KISClient()
    held, avg = _held(c)  # 중복주문 방지 (연결 불안정 재시도 안전)
    if held >= QTY:
        print(f"[가드] 이미 {SYMBOL} {held}주 보유(매입 {avg:,}) — 재매수 안 함. 관리 등록만.")
        _register(avg, held)
        return
    if held > 0:
        print(f"[가드] {SYMBOL} {held}주 이미 보유 — 이상상황, 재매수 중단. 수동 확인 필요.")
        return

    pr = c.get_price(SYMBOL)
    price = int(float(pr.get("output", {}).get("stck_prpr", 0) or 0)) if pr.get("rt_cd") == "0" else 0
    print(f"[매수 전] {SYMBOL} 현재가 {price:,}원, {QTY}주 예상 {price*QTY:,}원")

    resp = c.order_cash(SYMBOL, QTY, side="buy", order_type="01")  # 01=시장가
    print(f"[매수주문] rt_cd={resp.get('rt_cd')} msg={resp.get('msg1', '')}")
    if resp.get("rt_cd") != "0":
        print("!! 매수 실패 — 중단 !!")
        return

    time.sleep(2.5)
    h2, a2 = _held(c)
    print(f"[체결확인] {SYMBOL} 보유 {h2}주 | 매입가 {a2:,}원 | 매입금액 {a2*h2:,}원")
    _register(a2 or price, h2 or QTY)


if __name__ == "__main__":
    main()
