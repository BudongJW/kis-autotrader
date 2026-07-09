"""외국인·기관 수급 데이터 프로브 — '진짜 나침반' 핵심 재료 확보 가능성 확인.

코스피 방향의 최강 신호는 외국인/기관 순매수. KIS 투자자별 매매동향
(inquire-investor, FHKST01010900)이 뽑히는지, 어떤 필드가 오는지 확인. 읽기 전용.
debug-once: script=scripts.debug_investor_flow
"""
from __future__ import annotations

from src.config import settings
from src.kis_auth import auth_headers
from src.kis_client import _request_with_retry

TR = "FHKST01010900"  # 종목별 투자자매매동향(일별)
PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"

# 069500 = KODEX 200 (코스피200 대표 ETF, 시장 수급 프록시)
TARGETS = [("J", "069500", "KODEX 200"), ("U", "0001", "코스피지수")]


def _n(v) -> int:
    try:
        return int(float(str(v).replace(",", "") or 0))
    except Exception:
        return 0


def probe(mrkt: str, code: str, name: str) -> None:
    url = f"{settings.base_url}{PATH}"
    params = {"FID_COND_MRKT_DIV_CODE": mrkt, "FID_INPUT_ISCD": code}
    try:
        resp = _request_with_retry("GET", url, headers=auth_headers(TR), params=params).json()
    except Exception as e:
        print(f"\n[{name} {code}] 예외: {e}")
        return
    print(f"\n[{name} {code}] rt_cd={resp.get('rt_cd')} {resp.get('msg1','')}")
    rows = resp.get("output") or resp.get("output2") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        print(f"  데이터 없음. 응답 키: {list(resp.keys())}")
        return
    print(f"  {len(rows)}일 반환. 첫 행 키: {sorted(rows[0].keys())[:20]}")
    print("  최근 5일 (날짜 | 외국인순매수 | 기관순매수 | 개인순매수):")
    for r in rows[:5]:
        d = r.get("stck_bsop_date", "?")
        frgn = _n(r.get("frgn_ntby_qty"))
        orgn = _n(r.get("orgn_ntby_qty"))
        prsn = _n(r.get("prsn_ntby_qty"))
        print(f"    {d} | 외국인 {frgn:+,} | 기관 {orgn:+,} | 개인 {prsn:+,}")


def main() -> None:
    print("=" * 60)
    print("외국인·기관 수급 데이터 프로브")
    print("=" * 60)
    for mrkt, code, name in TARGETS:
        probe(mrkt, code, name)


if __name__ == "__main__":
    main()
