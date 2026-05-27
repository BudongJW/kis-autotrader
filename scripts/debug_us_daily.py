"""해외 일봉 endpoint 응답 dump — 어제 'us_daily_empty' 에러 원인 진단.

QQQ에 대해 3개 endpoint 시도:
  1. get_overseas_price (현재가 — HHDFS00000300)
  2. get_overseas_daily_price (일봉 — HHDFS76240000)  ← 어제 에러난 곳
  3. 다른 거래소 코드 시도 (NAS vs NASD)

전체 응답을 raw로 출력해서 권한 vs endpoint 문제 판단.
"""

from __future__ import annotations

import json

from src.kis_client import KISClient


def _dump_resp(label: str, resp: dict) -> None:
    print(f"\n  === {label} ===")
    print(f"  rt_cd: {resp.get('rt_cd')}")
    print(f"  msg_cd: {resp.get('msg_cd')}")
    print(f"  msg1: {resp.get('msg1', '')[:200]}")
    output = resp.get("output", resp.get("output1", {}))
    output2 = resp.get("output2", [])
    print(f"  output type: {type(output).__name__}")
    if isinstance(output, dict):
        print(f"  output keys: {list(output.keys())[:10]}")
        for k, v in list(output.items())[:8]:
            v_str = str(v)[:80]
            print(f"    {k}: {v_str}")
    elif isinstance(output, list):
        print(f"  output length: {len(output)}")
        if output:
            print(f"  output[0] keys: {list(output[0].keys())[:10] if isinstance(output[0], dict) else '?'}")

    print(f"  output2 type: {type(output2).__name__}")
    if isinstance(output2, list):
        print(f"  output2 length: {len(output2)}")
        if output2 and isinstance(output2[0], dict):
            print(f"  output2[0] sample keys: {list(output2[0].keys())[:10]}")
            for k, v in list(output2[0].items())[:5]:
                print(f"    {k}: {str(v)[:60]}")


def main() -> None:
    client = KISClient()

    print("=" * 60)
    print("[1] QQQ 현재가 (HHDFS00000300)")
    print("=" * 60)
    try:
        resp = client.get_overseas_price("QQQ", "NASD")
        _dump_resp("get_overseas_price(QQQ, NASD)", resp)
    except Exception as e:
        print(f"  ❌ 실패: {e}")
        import traceback; traceback.print_exc()

    print()
    print("=" * 60)
    print("[2] QQQ 일봉 (HHDFS76240000) — 어제 에러난 endpoint")
    print("=" * 60)
    try:
        resp = client.get_overseas_daily_price("QQQ", "NASD")
        _dump_resp("get_overseas_daily_price(QQQ, NASD)", resp)
        # 응답 전체 첫 600자
        print(f"\n  === raw 응답 (앞 600자) ===")
        print(json.dumps(resp, ensure_ascii=False, indent=2)[:600])
    except Exception as e:
        print(f"  ❌ 실패: {e}")
        import traceback; traceback.print_exc()

    print()
    print("=" * 60)
    print("[3] AAPL 일봉 — 다른 종목으로 비교")
    print("=" * 60)
    try:
        resp = client.get_overseas_daily_price("AAPL", "NASD")
        _dump_resp("get_overseas_daily_price(AAPL, NASD)", resp)
    except Exception as e:
        print(f"  ❌ 실패: {e}")

    print()
    print("=" * 60)
    print("[4] 거래소 코드 비교 — NAS vs NASD")
    print("=" * 60)
    for excd in ["NASD", "NAS"]:
        try:
            resp = client.get_overseas_daily_price("QQQ", excd)
            output2 = resp.get("output2", [])
            n_rows = len(output2) if isinstance(output2, list) else 0
            n_rows_valid = sum(1 for r in output2 if isinstance(r, dict) and r.get("xymd"))
            print(f"  {excd:6}: rt_cd={resp.get('rt_cd')}, output2 행={n_rows}, 유효={n_rows_valid}")
        except Exception as e:
            print(f"  {excd:6}: ❌ {e}")


if __name__ == "__main__":
    main()
