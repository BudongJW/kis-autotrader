"""장부 정합화 — KIS 실제 체결내역을 진실원천으로 trades.csv 보정안을 만든다.

배경(2026-08 감사): 장부 순수량이 114800 -449, PSQ -11, 069500 -5, QQQM -1로 음수였다.
원인은 두 가지가 섞여 있다.
  (a) 유령 매도  — 체결 확인 실패인데 '요청대로 팔렸다 치고' 기록한 것(코드는 수정됨)
  (b) 누락 매수  — Debug 워크플로로 낸 수동 주문은 저널에 커밋되지 않아 매도만 남은 것
둘은 정반대 처방이 필요하므로, 추측하지 말고 **실제 체결내역과 대조**한다.

읽기 전용. 보정안을 logs/ledger_corrections.json 으로 출력만 하고 장부는 건드리지 않는다.
debug-once: script=scripts.reconcile_ledger   (env RC_START/RC_END=YYYYMMDD)
"""
from __future__ import annotations

import json
import os
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import settings
from src.kis_auth import auth_headers
from src.kis_client import _request_with_retry

KR_TR = "TTTC8001R"      # 국내주식 일별주문체결조회 (3개월 이내)
KR_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
US_TR = "TTTS3035R"      # 해외주식 주문체결내역
US_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
CANONICAL = "https://raw.githubusercontent.com/BudongJW/kis-trading-journal/main/state/trades.csv"
OUT = Path("logs/ledger_corrections.json")


def _n(v) -> int:
    try:
        return int(float(str(v).replace(",", "") or 0))
    except Exception:
        return 0


def _fetch(path: str, tr: str, params: dict) -> list[dict]:
    """연속조회(fk/nk)까지 훑어 전체 레코드 수집."""
    url = f"{settings.base_url}{path}"
    rows, fk, nk = [], "", ""
    for _ in range(30):
        p = dict(params)
        p["CTX_AREA_FK100" if "domestic" in path else "CTX_AREA_FK200"] = fk
        p["CTX_AREA_NK100" if "domestic" in path else "CTX_AREA_NK200"] = nk
        r = _request_with_retry("GET", url, headers=auth_headers(tr), params=p)
        d = r.json()
        if d.get("rt_cd") != "0":
            print(f"  [조회실패] {tr} {d.get('msg1', '')[:60]}")
            break
        rows.extend(d.get("output") or d.get("output1") or [])
        fk = d.get("ctx_area_fk100") or d.get("ctx_area_fk200") or ""
        nk = d.get("ctx_area_nk100") or d.get("ctx_area_nk200") or ""
        if (d.get("tr_cont") or "").strip() not in ("F", "M") or not nk.strip():
            break
    return rows


def kr_fills(start: str, end: str) -> list[dict]:
    """국내 실체결. 3개월 제한이 있어 구간을 나눠 조회."""
    out = []
    s = datetime.strptime(start, "%Y%m%d").date()
    e = datetime.strptime(end, "%Y%m%d").date()
    while s <= e:
        chunk_end = min(e, s + timedelta(days=80))
        raw = _fetch(KR_PATH, KR_TR, {
            "CANO": settings.kis_account_no,
            "ACNT_PRDT_CD": settings.kis_account_prod_code,
            "INQR_STRT_DT": s.strftime("%Y%m%d"),
            "INQR_END_DT": chunk_end.strftime("%Y%m%d"),
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "",
            "CCLD_DVSN": "01",        # 체결분만
            "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "", "EXCG_ID_DVSN_CD": "KRX",
        })
        for it in raw:
            q = _n(it.get("tot_ccld_qty"))
            if q <= 0:
                continue
            out.append({
                "date": str(it.get("ord_dt") or ""),
                "symbol": str(it.get("pdno") or ""),
                "side": "buy" if str(it.get("sll_buy_dvsn_cd")) == "02" else "sell",
                "qty": q,
                "price": _n(it.get("avg_prvs")) or _n(it.get("ord_unpr")),
                "market": "KR",
            })
        s = chunk_end + timedelta(days=1)
    return out


def us_fills(start: str, end: str) -> list[dict]:
    out = []
    for exch in ("NASD", "AMEX"):
        raw = _fetch(US_PATH, US_TR, {
            "CANO": settings.kis_account_no,
            "ACNT_PRDT_CD": settings.kis_account_prod_code,
            "PDNO": "%", "ORD_STRT_DT": start, "ORD_END_DT": end,
            "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "01",   # 체결
            "OVRS_EXCG_CD": exch, "SORT_SQN": "DS",
            "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
        })
        for it in raw:
            q = _n(it.get("ft_ccld_qty"))
            if q <= 0:
                continue
            out.append({
                "date": str(it.get("ord_dt") or ""),
                "symbol": str(it.get("pdno") or "").strip(),
                "side": "buy" if str(it.get("sll_buy_dvsn_cd")) == "02" else "sell",
                "qty": q,
                "price": float(str(it.get("ft_ccld_unpr3") or 0) or 0),
                "market": "US",
            })
    return out


def journal_rows() -> list[dict]:
    p = Path("logs/trades.csv")
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(CANONICAL, p)
    import csv
    rows = []
    with p.open(encoding="utf-8") as f:
        for r in csv.reader(f):
            if len(r) < 6 or r[3] not in ("buy", "sell"):
                continue
            rows.append({"ts": r[0], "date": r[0][:10].replace("-", ""),
                         "symbol": r[1], "side": r[3], "qty": int(float(r[4])),
                         "price": float(r[5]), "reason": r[8] if len(r) > 8 else ""})
    return rows


def main() -> None:
    end = os.environ.get("RC_END") or date.today().strftime("%Y%m%d")
    start = os.environ.get("RC_START") or "20260601"
    print(f"정합화 대상 기간 {start} ~ {end}")

    jr = journal_rows()
    print(f"장부 {len(jr)}건")
    real = kr_fills(start, end) + us_fills(start, end)
    print(f"실체결 {len(real)}건")

    # 날짜·종목·방향 단위로 수량 집계 후 차이를 낸다(시각까지 맞추긴 어렵다)
    def agg(rows):
        d = defaultdict(int)
        for r in rows:
            d[(r["date"], r["symbol"], r["side"])] += r["qty"]
        return d

    ja, ra = agg(jr), agg(real)
    keys = sorted(set(ja) | set(ra))
    missing, phantom = [], []
    for k in keys:
        diff = ra.get(k, 0) - ja.get(k, 0)
        if diff > 0:
            missing.append({"date": k[0], "symbol": k[1], "side": k[2], "qty": diff,
                            "note": "실체결에 있으나 장부에 없음(누락)"})
        elif diff < 0:
            phantom.append({"date": k[0], "symbol": k[1], "side": k[2], "qty": -diff,
                            "note": "장부에만 있음(유령)"})

    print(f"\n=== 누락(장부에 추가해야) {len(missing)}건 ===")
    for m in missing:
        px = next((r["price"] for r in real
                   if (r["date"], r["symbol"], r["side"]) == (m["date"], m["symbol"], m["side"])), 0)
        m["price"] = px
        print(f"  {m['date']} {m['symbol']} {m['side']} {m['qty']}주 @ {px}")
    print(f"\n=== 유령(장부에서 빼야) {len(phantom)}건 ===")
    for p in phantom:
        print(f"  {p['date']} {p['symbol']} {p['side']} {p['qty']}주")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"start": start, "end": end,
                               "missing": missing, "phantom": phantom},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n보정안 저장: {OUT}")
    print("※ 이 스크립트는 장부를 수정하지 않는다. 보정 적용은 별도 단계.")


if __name__ == "__main__":
    main()
