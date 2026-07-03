"""US ETF 시세/체결가 프로브 — 방향성 모멘텀 종목 실현가능성 확인.

QQQ(비쌈) vs QQQM(저가판) vs PSQ(나스닥 인버스) 주당 가격을 확인해
$555 가용 예산으로 몇 주 살 수 있는지 판정. get_overseas_price 원시 output도
덤프해 정확한 가격 필드를 찾는다. 읽기 전용.
"""
from __future__ import annotations

from src.kis_client import KISClient

# (심볼, 거래소)
SYMS = [
    ("QQQ", "NASD"),
    ("QQQM", "NASD"),
    ("PSQ", "AMEX"),
    ("SPLG", "AMEX"),
]
AVAIL_USD = 554.95  # 현재 frcr_ord_psbl_amt1


def _price_from(out: dict) -> float:
    for k in ("last", "ovrs_now_pric1", "stck_prpr", "base", "ovrs_prpr",
              "prpr", "clos", "prdy_clpr"):
        v = out.get(k)
        try:
            if v not in (None, "", "0") and float(str(v).replace(",", "")) > 0:
                return float(str(v).replace(",", ""))
        except Exception:
            continue
    return 0.0


def main() -> None:
    client = KISClient()
    print("=" * 60)
    print(f"US ETF 가격 프로브 (가용 ${AVAIL_USD})")
    print("=" * 60)
    for sym, exch in SYMS:
        try:
            resp = client.get_overseas_price(sym, exch)
            out = resp.get("output") or {}
            if isinstance(out, list):
                out = out[0] if out else {}
            px = _price_from(out)
            if px > 0:
                shares = int(AVAIL_USD // px)
                print(f"\n{sym} ({exch}): ${px:,.2f}  → 가용예산으로 {shares}주 "
                      f"(1주 비용 ${px:,.2f})")
            else:
                print(f"\n{sym} ({exch}): 가격 미검출 rt_cd={resp.get('rt_cd')} "
                      f"msg={resp.get('msg1','')}")
                print(f"  output 키: {sorted(out.keys())[:12]}")
                for k in ("last", "ovrs_now_pric1", "prdy_clpr", "base"):
                    if k in out:
                        print(f"    {k}={out.get(k)}")
        except Exception as e:
            print(f"\n{sym} ({exch}): 예외 {e}")


if __name__ == "__main__":
    main()
