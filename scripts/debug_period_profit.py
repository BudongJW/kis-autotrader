"""기간별 매매손익현황 조회 (국내주식) — KIS TTTC8715R.

"어제/오늘 얼마 벌었냐"를 저널(회계 버그) 대신 KIS API 실현손익으로 직접 답한다.
읽기 전용. debug-once로 실행: script=scripts.debug_period_profit

기본 조회기간은 최근 2영업일(어제~오늘). 필요 시 env PP_START/PP_END(YYYYMMDD)로 지정.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from src.config import settings
from src.kis_auth import auth_headers
from src.kis_client import _request_with_retry

TR_PERIOD_PROFIT = "TTTC8715R"  # 국내주식 기간별매매손익현황조회 (실전)
PATH = "/uapi/domestic-stock/v1/trading/inquire-period-trade-profit"


def _n(v) -> int:
    try:
        return int(float(str(v).replace(",", "") or 0))
    except Exception:
        return 0


def query(start: str, end: str) -> dict:
    url = f"{settings.base_url}{PATH}"
    params = {
        "CANO": settings.kis_account_no,
        "ACNT_PRDT_CD": settings.kis_account_prod_code,
        "SORT_DVSN": "00",
        "INQR_STRT_DT": start,
        "INQR_END_DT": end,
        "PDNO": "",
        "CBLC_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    resp = _request_with_retry("GET", url, headers=auth_headers(TR_PERIOD_PROFIT), params=params)
    return resp.json()


def main() -> None:
    today = datetime.now()
    start = os.environ.get("PP_START", (today - timedelta(days=1)).strftime("%Y%m%d"))
    end = os.environ.get("PP_END", today.strftime("%Y%m%d"))

    print("=" * 64)
    print(f"기간별 매매손익 조회 {start} ~ {end}  (계좌 {settings.kis_account_no}-{settings.kis_account_prod_code})")
    print("=" * 64)

    data = query(start, end)
    print("rt_cd:", data.get("rt_cd"), "| msg:", data.get("msg1", ""))
    if data.get("rt_cd") != "0":
        print("조회 실패 — TR/파라미터 확인 필요")
        print("raw:", data)
        return

    # ── output1: 개별 매도(청산) 실현손익 행 ──
    rows = data.get("output1") or []
    print(f"\n[개별 청산 {len(rows)}건] (매매일 | 종목 | 실현손익 | 수익률)")
    per_day: dict[str, int] = {}
    for r in rows:
        d = r.get("trad_dt", "") or r.get("evlu_dt", "")
        name = r.get("prdt_name", r.get("pdno", ""))
        pfls = _n(r.get("rlzt_pfls") or r.get("evlu_pfls_amt"))
        rate = r.get("pfls_rt", r.get("evlu_pfls_rt", ""))
        per_day[d] = per_day.get(d, 0) + pfls
        print(f"  {d} | {name:<16} | {pfls:+,}원 | {rate}%")

    if per_day:
        print("\n[일자별 실현손익 합]")
        for d in sorted(per_day):
            print(f"  {d}: {per_day[d]:+,}원")

    # ── output2: 기간 합계 ──
    o2 = data.get("output2") or {}
    if isinstance(o2, list):
        o2 = o2[0] if o2 else {}
    print("\n[기간 합계 output2 — 전 필드]")
    for k in sorted(o2.keys()):
        print(f"  {k}: {o2.get(k)}")

    # 총 실현손익 후보 필드
    for key in ("tot_rlzt_pfls", "rlzt_pfls", "tot_trad_pfls"):
        if key in o2:
            print(f"\n  ▶ 총 실현손익({key}) = {_n(o2[key]):+,}원")
            break


if __name__ == "__main__":
    main()
