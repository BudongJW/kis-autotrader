"""수급 → 다음날 방향 예측력 빠른 검증 (30일, 소표본).

외국인+기관 순매수 부호가 (1) 같은날 등락과 관련있나, (2) 다음날 등락을 맞히나.
같은 endpoint(inquire-investor)가 flow와 prdy_vrss_sign(그날 등락)을 함께 줘서
별도 시세 없이 검증 가능. 30일이라 소표본 — 첫 신호 확인용.
debug-once: script=scripts.debug_flow_predict
"""
from __future__ import annotations

from src.config import settings
from src.kis_auth import auth_headers
from src.kis_client import _request_with_retry

TR = "FHKST01010900"
PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"


def _n(v) -> int:
    try:
        return int(float(str(v).replace(",", "") or 0))
    except Exception:
        return 0


def main() -> None:
    url = f"{settings.base_url}{PATH}"
    resp = _request_with_retry("GET", url, headers=auth_headers(TR),
                               params={"FID_COND_MRKT_DIV_CODE": "J",
                                       "FID_INPUT_ISCD": "069500"}).json()
    rows = resp.get("output") or resp.get("output2") or []
    print("=" * 60)
    print(f"수급 -> 다음날 방향 검증 (069500, {len(rows)}일)")
    print("=" * 60)

    # 날짜 오름차순 정렬, 등락 부호 유효한 것만
    recs = []
    for r in rows:
        d = r.get("stck_bsop_date", "")
        sign = str(r.get("prdy_vrss_sign", ""))
        up = 1 if sign in ("1", "2") else (-1 if sign in ("4", "5") else 0)
        flow = _n(r.get("frgn_ntby_qty")) + _n(r.get("orgn_ntby_qty"))  # 외국인+기관
        frgn = _n(r.get("frgn_ntby_qty"))
        if up != 0:
            recs.append((d, flow, frgn, up))
    recs.sort(key=lambda x: x[0])
    print(f"유효일: {len(recs)}")

    # (1) 같은날: flow 부호 == 그날 등락?
    same = [1 for _, f, _, u in recs if (f > 0) == (u > 0)]
    # (2) 다음날: flow(t) 부호가 등락(t+1) 맞히나?
    nxt_all = nxt_frgn = 0
    hit_all = hit_frgn = 0
    for i in range(len(recs) - 1):
        _, f, fr, _ = recs[i]
        _, _, _, u1 = recs[i + 1]
        if f != 0:
            nxt_all += 1
            hit_all += int((f > 0) == (u1 > 0))
        if fr != 0:
            nxt_frgn += 1
            hit_frgn += int((fr > 0) == (u1 > 0))

    up_days = sum(1 for *_, u in recs if u > 0)
    print(f"\n기준선(상승일 비율): {up_days}/{len(recs)} = {up_days/len(recs):.1%}")
    print(f"\n(1) 같은날 flow 부호 == 등락: {sum(same)}/{len(recs)} = {sum(same)/len(recs):.1%}")
    print(f"    (수급이 가격과 관련있는지 = 재료 유효성)")
    print(f"\n(2) 다음날 예측:")
    if nxt_all:
        print(f"    외국인+기관 부호 -> 다음날: {hit_all}/{nxt_all} = {hit_all/nxt_all:.1%}")
    if nxt_frgn:
        print(f"    외국인만 부호     -> 다음날: {hit_frgn}/{nxt_frgn} = {hit_frgn/nxt_frgn:.1%}")
    print("\n주의: 30일 소표본이라 노이즈 큼. 55%+ 나오면 더 긴 데이터로 본격 검증 가치.")


if __name__ == "__main__":
    main()
