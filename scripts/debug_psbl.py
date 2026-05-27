"""국내 매수가능금액 진단 — '주문가능 0원' 원인 추적.

3가지 비교:
  1. inquire-balance의 output2 전체 (ord_psbl_cash 외 다른 필드도)
  2. inquire-psbl-order with OVRS_ICLD_YN=N (국내 전용)
  3. inquire-psbl-order with OVRS_ICLD_YN=Y (해외 포함)

차이를 보면:
  - 2번과 3번이 같으면 통합증거금 영향 없음
  - 2번 << 3번이면 통합증거금이 KRW를 USD 담보로 잡고 있음
  - 둘 다 0이면 정말 매수 가능 자금 없음 (D+2 결제 대기 등)
"""

from __future__ import annotations

import json

from src.kis_client import KISClient


def _print_resp(label: str, resp: dict, focus_fields: list[str] = None):
    print(f"\n=== {label} ===")
    print(f"rt_cd: {resp.get('rt_cd')}, msg: {resp.get('msg1', '')[:100]}")
    output = resp.get("output", {})
    if isinstance(output, list) and output:
        output = output[0]
    if focus_fields and isinstance(output, dict):
        for f in focus_fields:
            v = output.get(f)
            if v is not None:
                print(f"  {f}: {v}")
    else:
        if isinstance(output, dict):
            for k, v in list(output.items())[:20]:
                print(f"  {k}: {v}")


def main() -> None:
    client = KISClient()

    print("=" * 60)
    print("[1] inquire-balance — output2 (계좌 요약) 전체")
    print("=" * 60)
    resp = client.get_balance()
    print(f"rt_cd: {resp.get('rt_cd')}, msg: {resp.get('msg1', '')[:80]}")
    output2 = resp.get("output2", [])
    if isinstance(output2, list) and output2:
        output2 = output2[0]
    if isinstance(output2, dict):
        for k, v in sorted(output2.items()):
            print(f"  {k:30}: {v}")

    print()
    print("=" * 60)
    print("[2] inquire-psbl-order — KODEX 반도체 (091160) @ 162,000원")
    print("    OVRS_ICLD_YN = N (국내 전용)")
    print("=" * 60)
    try:
        resp = client.inquire_psbl_order("091160", 162000, "00", "N")
        _print_resp("국내 전용", resp)
    except Exception as e:
        print(f"❌ {e}")

    print()
    print("=" * 60)
    print("[3] inquire-psbl-order — KODEX 반도체 @ 162,000원")
    print("    OVRS_ICLD_YN = Y (해외 통합증거금 포함)")
    print("=" * 60)
    try:
        resp = client.inquire_psbl_order("091160", 162000, "00", "Y")
        _print_resp("해외 포함", resp)
    except Exception as e:
        print(f"❌ {e}")

    print()
    print("=" * 60)
    print("[4] inquire-psbl-order — 저가 ETF KODEX 철강 (117680) @ 8,600원")
    print("=" * 60)
    try:
        resp = client.inquire_psbl_order("117680", 8600, "00", "Y")
        _print_resp("KODEX 철강 (저가)", resp)
    except Exception as e:
        print(f"❌ {e}")

    print()
    print("=" * 60)
    print("[5] inquire-psbl-order — 시장가 주문 시도 (ORD_DVSN=01)")
    print("=" * 60)
    try:
        resp = client.inquire_psbl_order("117680", 0, "01", "Y")
        _print_resp("시장가", resp)
    except Exception as e:
        print(f"❌ {e}")

    print()
    print("=" * 60)
    print("진단 가이드:")
    print("  - [2]와 [3]이 같음 → 통합증거금 영향 없음, 다른 원인 (D+2 등)")
    print("  - [2] << [3] → 통합증거금이 KRW를 USD 담보로 잡음")
    print("  - 모두 0 → 정말 매수 가능 자금 없음")
    print("=" * 60)


if __name__ == "__main__":
    main()
