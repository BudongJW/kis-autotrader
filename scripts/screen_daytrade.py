"""단타 프로파일 개별주 스크리너 (KRX 실데이터, pykrx).

저가 + 고유동성(거래대금) + 고변동(일중 변동폭) 종목을 정량 추출한다.
추측이 아니라 최근 거래일 실데이터 기준. 우선주·스팩·초저가·초소형(작전주)은 제외.

사용: python scripts/screen_daytrade.py [기준일YYYYMMDD] [N]
출력: 상위 N개 후보 (코드·이름·종가·거래대금·평균변동폭%·시총)
"""
from __future__ import annotations
import sys
from pykrx import stock

PRICE_MIN, PRICE_MAX = 2000, 30000      # 저가대(페니/초고가 제외)
MCAP_MIN = 3000e8                         # 시총 3000억 하한(작전주·관리종목 회피)
TURNOVER_MIN = 300e8                      # 일 거래대금 300억 하한(유동성=단타 가능)
VOL_DAYS = 15                             # 변동폭 평균 윈도
SHORTLIST = 40                            # 1차 통과 후 변동성 계산 대상


def latest_trading_day(seed: str) -> str:
    import datetime
    y, m, d = int(seed[:4]), int(seed[4:6]), int(seed[6:8])
    base = datetime.date(y, m, d)
    for back in range(0, 8):
        ds = (base - datetime.timedelta(days=back)).strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv(ds, market="KOSPI")
            if df is not None and len(df) > 100 and df["종가"].sum() > 0:
                return ds
        except Exception:
            pass
    return seed


def screen(date: str, topn: int):
    rows = []
    for mkt in ("KOSPI", "KOSDAQ"):
        ohlcv = stock.get_market_ohlcv(date, market=mkt)
        cap = stock.get_market_cap(date, market=mkt)
        for tkr in ohlcv.index:
            try:
                close = float(ohlcv.loc[tkr, "종가"])
                value = float(ohlcv.loc[tkr, "거래대금"])
                hi = float(ohlcv.loc[tkr, "고가"]); lo = float(ohlcv.loc[tkr, "저가"])
                mcap = float(cap.loc[tkr, "시가총액"]) if tkr in cap.index else 0.0
            except Exception:
                continue
            if not tkr.endswith("0"):       # 우선주 등 제외(보통주만)
                continue
            if not (PRICE_MIN <= close <= PRICE_MAX):
                continue
            if value < TURNOVER_MIN or mcap < MCAP_MIN:
                continue
            name = stock.get_market_ticker_name(tkr)
            if any(x in name for x in ("스팩", "리츠", "우B", "전환")):
                continue
            day_range = (hi - lo) / close * 100 if close else 0
            rows.append({"tkr": tkr, "name": name, "mkt": mkt, "close": close,
                         "value": value, "mcap": mcap, "day_range": day_range})
    # 1차: 거래대금 상위 SHORTLIST (유동성 우선)
    rows.sort(key=lambda r: r["value"], reverse=True)
    short = rows[:SHORTLIST]
    # 2차: 최근 VOL_DAYS 평균 일중 변동폭% 계산
    import datetime
    end = date
    start = (datetime.datetime.strptime(date, "%Y%m%d")
             - datetime.timedelta(days=VOL_DAYS * 2)).strftime("%Y%m%d")
    for r in short:
        try:
            h = stock.get_market_ohlcv(start, end, r["tkr"])
            h = h.tail(VOL_DAYS)
            rng = ((h["고가"] - h["저가"]) / h["종가"] * 100).mean()
            r["avg_range"] = float(rng)
        except Exception:
            r["avg_range"] = r["day_range"]
    # 최종: 변동성 순 (단타 핵심)
    short.sort(key=lambda r: r["avg_range"], reverse=True)
    return short[:topn]


def main():
    seed = sys.argv[1] if len(sys.argv) > 1 else "20260619"
    topn = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    date = latest_trading_day(seed)
    res = screen(date, topn)
    print(f"\n[단타 스크리너] 기준일 {date} — 저가·고유동성·고변동 상위 {topn}")
    print(f"{'코드':<8}{'이름':<14}{'시장':<7}{'종가':>9}{'거래대금(억)':>12}{'평균변동%':>10}{'시총(억)':>10}")
    for r in res:
        print(f"{r['tkr']:<8}{r['name']:<14}{r['mkt']:<7}{r['close']:>9,.0f}"
              f"{r['value']/1e8:>12,.0f}{r.get('avg_range',0):>10.1f}{r['mcap']/1e8:>10,.0f}")


if __name__ == "__main__":
    main()
