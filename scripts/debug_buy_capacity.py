"""인덱스(069500) 풀매수 가능 여부 검토 — 읽기 전용, 주문 안 함.

매수가능조회(TTTC8908R)로 미수없는 매수가능수량/금액을 확인하고, 잔고와 대조해
"지금 인덱스를 얼마까지 살 수 있는지"를 사실로 보고한다. debug-once: script=scripts.debug_buy_capacity
"""
from __future__ import annotations

from src.kis_client import KISClient

SYMBOL = "069500"
NAME = "KODEX 200"


def _n(v) -> int:
    try:
        return int(float(str(v).replace(",", "") or 0))
    except Exception:
        return 0


def main() -> None:
    c = KISClient()

    pr = c.get_price(SYMBOL)
    price = _n((pr.get("output") or {}).get("stck_prpr"))
    print(f"[시세] {NAME}({SYMBOL}) 현재가 {price:,}원")

    bal = c.get_balance()
    o1 = bal.get("output1") or []
    print("[보유]")
    held_val = 0
    for it in o1:
        q = _n(it.get("hldg_qty"))
        if q <= 0:
            continue
        held_val += _n(it.get("evlu_amt"))
        print(f"  {it.get('pdno')} {it.get('prdt_name')} {q}주 | 평가 {_n(it.get('evlu_amt')):,} "
              f"| 손익 {_n(it.get('evlu_pfls_amt')):+,}")
    o2 = bal.get("output2") or []
    o2 = (o2[0] if isinstance(o2, list) else o2) or {}
    cash = _n(o2.get("dnca_tot_amt"))
    d2 = _n(o2.get("prvs_rcdl_excc_amt"))   # D+2 예수금(정산 후 실제 가용)
    tot = _n(o2.get("tot_evlu_amt"))
    print(f"[계좌] 예수금 {cash:,} | D+2예수금 {d2:,} | 유가평가 {held_val:,} | 총평가 {tot:,}")

    ps = c.inquire_psbl_order(SYMBOL, price, order_division="01", include_overseas="Y")
    out = ps.get("output") or {}
    if isinstance(out, list):
        out = out[0] if out else {}
    print(f"[매수가능조회] rt_cd={ps.get('rt_cd')} msg={ps.get('msg1','')}")
    fields = {
        "ord_psbl_cash": "주문가능현금",
        "nrcvb_buy_amt": "미수없는 매수금액",
        "nrcvb_buy_qty": "미수없는 매수수량",
        "max_buy_amt": "최대 매수금액(신용포함)",
        "max_buy_qty": "최대 매수수량(신용포함)",
        "ovrs_re_use_amt_wcrc": "해외 재사용가능(원화)",
        "psbl_qty_calc_unpr": "계산단가",
    }
    for k, lab in fields.items():
        if k in out:
            print(f"  {lab:<22}({k}): {_n(out[k]):,}")

    nq = _n(out.get("nrcvb_buy_qty"))
    na = _n(out.get("nrcvb_buy_amt"))
    print("\n=== 풀매수 판정 ===")
    if price > 0 and nq > 0:
        print(f"  현금으로 살 수 있는 최대: {nq}주 = 약 {nq*price:,}원 (미수 없음)")
        print(f"  총평가 대비 비중: {(nq*price)/tot*100:.1f}%" if tot else "")
    else:
        print("  현금 매수가능 0 — 미정산(D+2) 또는 잔여현금 없음")
    print(f"  [참고] 신용 포함 최대: {_n(out.get('max_buy_qty')):,}주 (미수/신용은 사용 안 함)")


if __name__ == "__main__":
    main()
