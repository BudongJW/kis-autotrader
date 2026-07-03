"""국내주식 일별 체결내역 조회 — KIS TTTC8001R. 저널 백필의 진실원천.

trades.csv 누락·유령을 실제 체결과 대조해 정리하기 위한 읽기 전용 조회.
기본 오늘 하루. env EX_START/EX_END(YYYYMMDD)로 기간 지정.
debug-once: script=scripts.debug_executions
"""
from __future__ import annotations

import os
from datetime import datetime

from src.config import settings
from src.kis_auth import auth_headers
from src.kis_client import _request_with_retry

TR = "TTTC8001R"  # 국내주식 일별주문체결조회 (실전, 3개월 이내)
PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"


def _n(v) -> int:
    try:
        return int(float(str(v).replace(",", "") or 0))
    except Exception:
        return 0


def query(start: str, end: str, fk: str = "", nk: str = "") -> dict:
    url = f"{settings.base_url}{PATH}"
    params = {
        "CANO": settings.kis_account_no,
        "ACNT_PRDT_CD": settings.kis_account_prod_code,
        "INQR_STRT_DT": start,
        "INQR_END_DT": end,
        "SLL_BUY_DVSN_CD": "00",   # 전체
        "INQR_DVSN": "00",         # 역순
        "PDNO": "",
        "CCLD_DVSN": "01",         # 체결분만
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": fk,
        "CTX_AREA_NK100": nk,
    }
    resp = _request_with_retry("GET", url, headers=auth_headers(TR), params=params)
    return resp.json()


def main() -> None:
    today = datetime.now().strftime("%Y%m%d")
    start = os.environ.get("EX_START", today)
    end = os.environ.get("EX_END", today)

    print("=" * 68)
    print(f"일별 체결내역 {start} ~ {end}  (계좌 {settings.kis_account_no}-{settings.kis_account_prod_code})")
    print("=" * 68)

    data = query(start, end)
    print("rt_cd:", data.get("rt_cd"), "| msg:", data.get("msg1", ""))
    if data.get("rt_cd") != "0":
        print("raw:", {k: data.get(k) for k in ("rt_cd", "msg_cd", "msg1")})
        return

    rows = data.get("output1") or []
    print(f"\n[체결 {len(rows)}건] 시각 | 종목 | 매수/매도 | 체결수량 @ 평균가 = 금액")
    executed = []
    for r in rows:
        ccld_qty = _n(r.get("tot_ccld_qty"))
        if ccld_qty <= 0:
            continue
        side = r.get("sll_buy_dvsn_cd_name", r.get("sll_buy_dvsn_cd", ""))
        pdno = r.get("pdno", "")
        name = r.get("prdt_name", "")
        avg = _n(r.get("avg_prvs") or r.get("ccld_prvs") or r.get("ord_unpr"))
        amt = _n(r.get("tot_ccld_amt"))
        tmd = r.get("ord_tmd", "")
        tmd_fmt = f"{tmd[:2]}:{tmd[2:4]}:{tmd[4:6]}" if len(tmd) >= 6 else tmd
        executed.append((tmd_fmt, pdno, name, side, ccld_qty, avg, amt, r.get("odno", "")))
        print(f"  {tmd_fmt} | {pdno} {name:<12} | {side:<4} | {ccld_qty}주 @ {avg:,} = {amt:,}원  (주문 {r.get('odno','')})")

    # 종목별 순수량(매수-매도) — trades.csv 순포지션과 대조용
    print("\n[종목별 체결 순수량]")
    net: dict[str, int] = {}
    for _, pdno, _, side, qty, _, _, _ in executed:
        sgn = qty if ("매수" in side or side == "02") else -qty
        net[pdno] = net.get(pdno, 0) + sgn
    for k, v in sorted(net.items()):
        print(f"  {k}: {v:+d}주")

    o2 = data.get("output2") or {}
    if isinstance(o2, list):
        o2 = o2[0] if o2 else {}
    if o2:
        print("\n[합계 output2]")
        for k in ("tot_ord_qty", "tot_ccld_qty", "tot_ccld_amt", "pchs_avg_pric"):
            if k in o2:
                print(f"  {k}: {o2.get(k)}")


if __name__ == "__main__":
    main()
