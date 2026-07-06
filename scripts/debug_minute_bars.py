"""국내 분봉 조회 프로브 (FHKST03010200) — 진입 시야(VWAP/세션구조) 토대 검증.

봇이 지금 진입에 쓰는 데이터는 전일종가/시가/현재가 3개뿐이라 '꼭지냐 눌림이냐'를
못 본다. 분봉이 실제로 오는지, VWAP/세션고저/range내위치를 계산 가능한지 확인.
읽기 전용. debug-once: script=scripts.debug_minute_bars
"""
from __future__ import annotations

from datetime import datetime

from src.config import settings
from src.kis_auth import auth_headers
from src.kis_client import _request_with_retry

TR = "FHKST03010200"  # 주식당일분봉조회 (공통)
PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
SYMBOL = "069500"  # KODEX 200 (조간 벤치마크)


def _n(v) -> float:
    try:
        return float(str(v).replace(",", "") or 0)
    except Exception:
        return 0.0


def main() -> None:
    now = datetime.now().strftime("%H%M%S")
    url = f"{settings.base_url}{PATH}"
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": SYMBOL,
        "FID_INPUT_HOUR_1": now,
        "FID_PW_DATA_INCU_YN": "N",
    }
    resp = _request_with_retry("GET", url, headers=auth_headers(TR), params=params).json()
    print("=" * 60)
    print(f"분봉 조회 {SYMBOL} @ {now}  rt_cd={resp.get('rt_cd')} {resp.get('msg1','')}")
    print("=" * 60)
    if resp.get("rt_cd") != "0":
        print("raw:", {k: resp.get(k) for k in ("rt_cd", "msg_cd", "msg1")})
        return

    bars = resp.get("output2") or []
    print(f"[분봉 {len(bars)}개] (시각 | 시가 고가 저가 종가 | 거래량)")
    # KIS는 최신→과거 순으로 줌. 시간순 정렬.
    bars = sorted(bars, key=lambda b: b.get("stck_cntg_hour", ""))
    pv = tv = 0.0
    hi = lo = None
    for b in bars[-12:]:
        t = b.get("stck_cntg_hour", "")
        tf = f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else t
        o, h, l, c = _n(b.get("stck_oprc")), _n(b.get("stck_hgpr")), _n(b.get("stck_lwpr")), _n(b.get("stck_prpr"))
        vol = _n(b.get("cntg_vol"))
        print(f"  {tf} | {o:,.0f} {h:,.0f} {l:,.0f} {c:,.0f} | {vol:,.0f}")
    # 전체 반환분으로 VWAP/세션 고저 계산
    for b in bars:
        h, l, c = _n(b.get("stck_hgpr")), _n(b.get("stck_lwpr")), _n(b.get("stck_prpr"))
        vol = _n(b.get("cntg_vol"))
        typ = (h + l + c) / 3
        pv += typ * vol
        tv += vol
        hi = h if hi is None else max(hi, h)
        lo = l if lo is None else min(lo, l)
    cur = _n(bars[-1].get("stck_prpr")) if bars else 0
    vwap = pv / tv if tv else 0
    print("\n[세션 구조 (반환된 분봉 범위 기준)]")
    print(f"  현재가 {cur:,.0f} | 고가 {hi:,.0f} | 저가 {lo:,.0f} | VWAP {vwap:,.0f}")
    if hi and lo and hi > lo:
        pos = (cur - lo) / (hi - lo)  # 0=저점 1=고점
        pb = (hi - cur) / hi * 100     # 고점 대비 되돌림 %
        print(f"  range내 위치 {pos*100:.0f}% (0=저점,100=고점) | 고점대비 되돌림 -{pb:.2f}%")
        print(f"  VWAP 대비 {'위(+' if cur>=vwap else '아래('}{(cur-vwap)/vwap*100:+.2f}%)")
        print("\n  [진입시야 예시] 상승추세인데 range 80%+ = 꼭지추격(오늘 실패 패턴)")
        print("                  상승추세인데 VWAP 부근+range 40~60% = 눌림목(좋은 진입)")


if __name__ == "__main__":
    main()
