"""미국 가용 달러 조회 — 해외잔고 + 매수가능금액(통합증거금 포함).

"미국 거래에 쓸 수 있는 달러가 얼마냐"를 API로 직접 답한다. 읽기 전용.
debug-once로 실행: script=scripts.debug_us_cash

핵심 구분:
  - frcr_ord_psbl_amt1: 순수 외화(USD) 주문가능금액 (실제 달러 잔고만)
  - echm_af_ord_psbl_amt: 환전이후 주문가능금액 (원화 통합증거금 환산 포함 = 실제 가용)
"""
from __future__ import annotations

from src.kis_client import KISClient

REF_SYMBOL = "SPLG"      # 매수가능 조회용 기준 종목
REF_EXCHANGE = "AMEX"


def _f(v) -> str:
    try:
        return f"{float(str(v).replace(',', '')):,.2f}"
    except Exception:
        return str(v)


def main() -> None:
    client = KISClient()

    print("=" * 60)
    print("[1] 해외 잔고 (NASD) output2 — 외화 예수금/평가")
    print("=" * 60)
    bal = client.get_overseas_balance("NASD")
    print("rt_cd:", bal.get("rt_cd"), "| msg:", bal.get("msg1", ""))
    o2 = bal.get("output2") or {}
    if isinstance(o2, list):
        o2 = o2[0] if o2 else {}
    usd_keys = {
        "frcr_dncl_amt1": "외화예수금",
        "frcr_dncl_amt_2": "외화예수금2",
        "frcr_evlu_tota": "외화평가총액",
        "frcr_pchs_amt1": "외화매입금액",
        "ord_psbl_frcr_amt": "주문가능외화금액",
        "tot_dncl_amt": "총예수금",
        "tot_evlu_pfls_amt": "총평가손익",
    }
    for k, lab in usd_keys.items():
        if k in o2:
            print(f"  {lab:<16}({k}): {_f(o2[k])}")
    print("  --- output2 전체 키 ---")
    for k in sorted(o2.keys()):
        if k not in usd_keys:
            print(f"    {k}: {o2.get(k)}")
    for it in (bal.get("output1") or []):
        if float(it.get("ovrs_cblc_qty", 0) or 0) > 0:
            print(f"  [보유] {it.get('ovrs_pdno')} {it.get('ovrs_cblc_qty')}주 "
                  f"평가 {it.get('ovrs_stck_evlu_amt')} {it.get('crcy_cd')}")

    print("\n" + "=" * 60)
    print(f"[2] 매수가능금액 (psamount, 기준 {REF_SYMBOL}) — 통합증거금 반영")
    print("=" * 60)
    price = 60.0
    try:
        pr = client.get_overseas_price(REF_SYMBOL, REF_EXCHANGE)
        po = pr.get("output") or {}
        for cand in ("last", "ovrs_now_pric1", "stck_prpr", "base"):
            if po.get(cand):
                price = float(str(po[cand]).replace(",", ""))
                break
        print(f"  {REF_SYMBOL} 현재가 조회: {price}")
    except Exception as e:
        print(f"  현재가 조회 실패({e}) — 기준가 {price} 사용")

    ps = client.get_overseas_psamount(REF_SYMBOL, price, REF_EXCHANGE)
    print("  rt_cd:", ps.get("rt_cd"), "| msg:", ps.get("msg1", ""))
    out = ps.get("output") or {}
    if isinstance(out, list):
        out = out[0] if out else {}
    key_fields = {
        "frcr_ord_psbl_amt1": "순수 USD 주문가능금액",
        "echm_af_ord_psbl_amt": "환전이후 주문가능금액(통합증거금 = 실가용)",
        "echm_af_ord_psbl_qty": "환전이후 주문가능수량",
        "ovrs_ord_psbl_amt": "해외주문가능금액",
        "ord_psbl_frcr_amt": "주문가능외화금액",
    }
    for k, lab in key_fields.items():
        if k in out:
            print(f"  {lab}: {_f(out[k])}  ({k})")
    print("  --- output 전체 키 ---")
    for k in sorted(out.keys()):
        if k not in key_fields:
            print(f"    {k}: {out.get(k)}")


if __name__ == "__main__":
    main()
