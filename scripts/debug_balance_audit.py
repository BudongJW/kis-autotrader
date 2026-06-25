"""잔고 감사 — KIS 원시 잔고 필드 전체 덤프 + trades.csv 대조.

888k(6/22)→577k(6/25) 31만원 급감의 행방 규명. 출금 없음 확인됨.
inquire-balance output2 전 필드 + 해외잔고 + KR 거래 현금흐름 재구성.

사용: debug-once.yml에서 script=scripts.debug_balance_audit
"""
from __future__ import annotations

import json

from src.kis_client import KISClient
from src.merge_trades import _read


def _f(d: dict, k: str) -> str:
    v = d.get(k, "")
    return f"{int(v):,}" if str(v).lstrip("-").isdigit() else str(v)


def main() -> None:
    client = KISClient()
    print("=" * 64)
    print("[1] 국내 잔고 (inquire-balance) output2 — 계좌 요약 전 필드")
    print("=" * 64)
    bal = client.get_balance()
    print("  rt_cd:", bal.get("rt_cd"), "| msg:", bal.get("msg1", ""))
    o2 = (bal.get("output2") or [{}])
    o2 = o2[0] if isinstance(o2, list) and o2 else (o2 if isinstance(o2, dict) else {})
    # 핵심 필드 라벨
    labels = {
        "dnca_tot_amt": "예수금총액",
        "prvs_rcdl_excc_amt": "가수도정산금액(미정산)",
        "thdt_buyqty": "금일매수수량",
        "nxdy_excc_amt": "익일정산금액",
        "prvs_rcdl_excc_amt2": "전일정산",
        "tot_evlu_amt": "총평가금액",
        "nass_amt": "순자산금액",
        "scts_evlu_amt": "유가증권평가",
        "pchs_amt_smtl_amt": "매입금액합계",
        "evlu_amt_smtl_amt": "평가금액합계",
        "evlu_pfls_smtl_amt": "평가손익합계",
        "tot_stln_slng_chgs": "총대주매도대금",
        "d2_auto_rdpt_amt": "D+2자동상환",
        "cma_evlu_amt": "CMA평가",
    }
    for k, lab in labels.items():
        if k in o2:
            print(f"  {lab:<22}({k}): {_f(o2, k)}")
    print("\n  --- output2 전체 키 (위에 없는 것) ---")
    for k in sorted(o2.keys()):
        if k not in labels:
            print(f"    {k}: {o2.get(k)}")

    print("\n  --- output1 (국내 보유종목) ---")
    for it in (bal.get("output1") or []):
        if int(float(it.get("hldg_qty", 0) or 0)) > 0:
            print(f"    {it.get('pdno')} {it.get('prdt_name')} {it.get('hldg_qty')}주 "
                  f"평가 {_f(it,'evlu_amt')} 손익 {_f(it,'evlu_pfls_amt')}")

    print("\n" + "=" * 64)
    print("[2] 해외 잔고 (get_overseas_balance)")
    print("=" * 64)
    try:
        ov = client.get_overseas_balance("NASD")
        print("  rt_cd:", ov.get("rt_cd"), "| msg:", ov.get("msg1", ""))
        oo2 = ov.get("output2") or {}
        if isinstance(oo2, list):
            oo2 = oo2[0] if oo2 else {}
        for k in sorted(oo2.keys()):
            print(f"    {k}: {oo2.get(k)}")
        for it in (ov.get("output1") or []):
            if float(it.get("ovrs_cblc_qty", 0) or 0) > 0:
                print(f"    [보유] {it.get('ovrs_pdno')} {it.get('ovrs_cblc_qty')}주 "
                      f"평가 {it.get('ovrs_stck_evlu_amt')} {it.get('crcy_cd')}")
    except Exception as e:
        print("  해외잔고 실패:", e)

    print("\n" + "=" * 64)
    print("[3] trades.csv 현금흐름 재구성 (KR=KRW, US=USD센트 분리)")
    print("=" * 64)
    rows = _read("logs/trades.csv")
    kr_in = kr_out = 0
    us_in_c = us_out_c = 0
    for r in rows:
        sym = str(r.get("symbol", ""))
        try:
            amt = int(float(r.get("amount", 0) or 0))
        except Exception:
            continue
        is_us = sym.startswith("US_") or sym in ("XLF", "SCHG", "SPLG", "PSQ", "SH", "TLT", "SHY", "SPY")
        side = r.get("side")
        if is_us:
            if side == "buy": us_out_c += amt
            else: us_in_c += amt
        else:
            if side == "buy": kr_out += amt
            else: kr_in += amt
    print(f"  KR 매수합 {kr_out:,} / 매도합 {kr_in:,} → 순현금 {kr_in - kr_out:+,}원")
    print(f"  US 매수합 {us_out_c/100:,.2f} / 매도합 {us_in_c/100:,.2f} → 순 {((us_in_c-us_out_c)/100):+,.2f}$ (센트단위 기록)")
    print(f"  거래건수: {len(rows)}")
    print(f"\n  ▶ initial 500,000 + KR순현금({kr_in-kr_out:+,}) = 예상 KR잔고 {500_000 + (kr_in-kr_out):,}원")
    print(f"    실제 예수금(dnca_tot_amt) = {_f(o2,'dnca_tot_amt')}원")


if __name__ == "__main__":
    main()
