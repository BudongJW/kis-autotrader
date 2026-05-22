"""inquire-psamount endpoint 응답 전체를 dump해서 cash_usd=0 원인 진단.

사용법:
  python -m scripts.debug_psamount

출력:
  - QQQ 현재가 조회 결과
  - inquire-psamount 전체 응답 (모든 field)
  - 통합증거금 관련 field들의 실제 값
"""

from __future__ import annotations

import json

from src.kis_client import KISClient
from src.bot.us_session import get_us_price


def main() -> None:
    client = KISClient()

    print("=" * 60)
    print("[1] QQQ 현재가 조회")
    print("=" * 60)
    try:
        qqq_price = get_us_price(client, "QQQ", "NASD")
        print(f"  QQQ 가격: ${qqq_price}")
    except Exception as e:
        print(f"  ❌ 실패: {e}")
        qqq_price = 500.0  # fallback for testing
        print(f"  fallback 가격 사용: ${qqq_price}")

    if qqq_price <= 0:
        print("\n  ⚠️ QQQ 가격 0 — psamount 호출 못 함. 강제 가격 $500으로 시도.")
        qqq_price = 500.0

    print()
    print("=" * 60)
    print(f"[2] inquire-psamount 호출 (QQQ @ ${qqq_price})")
    print("=" * 60)
    try:
        resp = client.get_overseas_psamount("QQQ", qqq_price, exchange="NASD")
        print(f"\n  rt_cd: {resp.get('rt_cd')}")
        print(f"  msg_cd: {resp.get('msg_cd')}")
        print(f"  msg1: {resp.get('msg1', '')[:200]}")
        print()
        print("  === output 전체 ===")
        output = resp.get("output", {})
        if isinstance(output, list):
            for i, o in enumerate(output):
                print(f"\n  --- output[{i}] ---")
                for k, v in o.items():
                    print(f"    {k}: {v}")
        elif isinstance(output, dict):
            for k, v in output.items():
                print(f"    {k}: {v}")
        else:
            print(f"    {output}")

        print()
        print("  === 핵심 USD 잔고 field들 ===")
        if isinstance(output, list) and output:
            output = output[0]
        if isinstance(output, dict):
            for field in [
                "frcr_ord_psbl_amt1",        # 외화 단독 주문가능
                "echm_af_ord_psbl_amt",       # 환전이후 주문가능
                "echm_af_ord_psbl_qty",       # 환전이후 주문가능 수량
                "max_ord_psbl_qty",
                "ord_psbl_frcr_amt",
                "ord_psbl_amt",
                "tr_crcy_cd",
                "nrcvb_buy_amt",
                "nrcvb_buy_qty",
            ]:
                value = output.get(field)
                print(f"    {field}: {value}")
    except Exception as e:
        print(f"  ❌ psamount 호출 실패: {e}")
        import traceback
        traceback.print_exc()

    print()
    print("=" * 60)
    print("[3] inquire-balance 호출 (참고용 — 외화 잔고)")
    print("=" * 60)
    try:
        resp = client.get_overseas_balance(exchange="NASD")
        print(f"  rt_cd: {resp.get('rt_cd')}")
        print(f"  msg1: {resp.get('msg1', '')[:200]}")
        print()
        output2 = resp.get("output2", {})
        if isinstance(output2, list) and output2:
            output2 = output2[0]
        print("  === output2 (외화 잔고 요약) ===")
        if isinstance(output2, dict):
            for k, v in output2.items():
                print(f"    {k}: {v}")
    except Exception as e:
        print(f"  ❌ balance 호출 실패: {e}")


if __name__ == "__main__":
    main()
