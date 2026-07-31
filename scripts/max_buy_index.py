"""사용자 지시 인덱스 최대 매수 — 069500(KODEX 200)을 현금 여력 전량으로 매수.

2026-07-15 사용자 명시 지시("최대한 매수 진행"). 기존 sizing_ramp 상한(65%)을 넘는
집중이므로 사용자 결정으로만 실행한다. 실전(MODE=live).

경계(그대로 지킴):
  - **현금(미수없는) 한도로만 매수.** 미수/신용(max_buy_qty)은 쓰지 않는다 — 빌린 돈은
    반대매매 리스크가 붙는 별개 문제이고 지시 범위 밖.
  - 레버리지/곱버스 아님(069500은 1x 인덱스).
debug-once: script=scripts.max_buy_index
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from src.kis_client import KISClient

SYMBOL = "069500"
NAME = "KODEX 200"


def _n(v) -> int:
    try:
        return int(float(str(v).replace(",", "") or 0))
    except Exception:
        return 0


def _held(c: KISClient) -> tuple[int, int]:
    bal = c.get_balance()
    for it in (bal.get("output1") or []):
        if it.get("pdno") == SYMBOL:
            return _n(it.get("hldg_qty")), _n(it.get("pchs_avg_pric"))
    return 0, 0


def main() -> None:
    c = KISClient()

    pr = c.get_price(SYMBOL)
    price = _n((pr.get("output") or {}).get("stck_prpr"))
    if price <= 0:
        print("!! 현재가 조회 실패 — 중단 !!")
        return
    held0, avg0 = _held(c)
    print(f"[전] {NAME} 현재가 {price:,} | 기존보유 {held0}주(평단 {avg0:,})")

    ps = c.inquire_psbl_order(SYMBOL, price, order_division="01", include_overseas="Y")
    out = ps.get("output") or {}
    if isinstance(out, list):
        out = out[0] if out else {}
    qty = _n(out.get("nrcvb_buy_qty"))          # 미수 없는(현금) 최대 수량
    amt = _n(out.get("nrcvb_buy_amt"))
    print(f"[여력] 현금 최대 {qty}주 (약 {amt:,}원) | 주문가능현금 {_n(out.get('ord_psbl_cash')):,}")
    print(f"       (신용포함 최대 {_n(out.get('max_buy_qty')):,}주 — 미수/신용은 사용 안 함)")
    if qty < 1:
        print("!! 현금 매수여력 0 — 매수 불가(D+2 미정산 등). 종료 !!")
        return

    resp = c.order_cash(SYMBOL, qty, side="buy", order_type="01")  # 시장가
    print(f"[매수주문] {qty}주 rt_cd={resp.get('rt_cd')} msg={resp.get('msg1','')}")
    if resp.get("rt_cd") != "0":
        print("!! 매수 실패 !!")
        return

    time.sleep(2.5)
    h2, a2 = _held(c)
    print(f"[체결확인] {SYMBOL} 보유 {h2}주 | 평단 {a2:,} | 평가 {a2*h2:,}")

    # 봇 청산관리 등록 (손절/트레일이 이 포지션도 관리하도록)
    try:
        from src.risk_manager import record_buy
        record_buy(SYMBOL, a2 or price, h2 or qty)
    except Exception as e:  # noqa: BLE001
        print("  record_buy 경고:", e)
    try:
        from src.tracker import log_trade
        log_trade(SYMBOL, NAME, "buy", (h2 - held0) or qty, (a2 or price) * 100, market="KR",
                  reason="사용자 지시: 인덱스 최대 매수(현금 전량, 미수/신용 미사용)")
    except Exception as e:  # noqa: BLE001
        print("  log_trade 경고:", e)
    mp = Path("logs/morning_positions.json")
    st = {}
    if mp.exists():
        try:
            st = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            st = {}
    st[SYMBOL] = {"direction": "long", "entry_price": a2 or price, "qty": h2 or qty,
                  "name": NAME, "peak": a2 or price,
                  "date": datetime.now().strftime("%Y-%m-%d")}
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    print("완료: 인덱스 최대 매수. 봇 손절/트레일 관리 등록됨.")


if __name__ == "__main__":
    main()
