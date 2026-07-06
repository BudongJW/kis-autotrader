"""실시간 KR 시장 스냅샷 — 현재 지수/대표종목 등락. 읽기 전용."""
from __future__ import annotations
from src.kis_client import KISClient

WATCH = [
    ("069500", "KODEX 200(코스피200)"),
    ("122630", "KODEX 레버리지"),
    ("114800", "KODEX 인버스"),
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("091180", "KODEX 자동차"),
]


def main() -> None:
    c = KISClient()
    print("=" * 56)
    print("실시간 KR 시장 스냅샷")
    print("=" * 56)
    for sym, name in WATCH:
        try:
            r = c.get_price(sym)
            o = r.get("output", {}) if r.get("rt_cd") == "0" else {}
            prpr = o.get("stck_prpr", "?")
            ctrt = o.get("prdy_ctrt", "?")      # 전일대비율 %
            sign = o.get("prdy_vrss_sign", "")  # 1상한2상승3보합4하한5하락
            arrow = {"1": "▲", "2": "▲", "3": "-", "4": "▼", "5": "▼"}.get(str(sign), "")
            vol = o.get("acml_vol", "?")
            print(f"  {name:<18} {int(prpr):>9,}원  {arrow}{ctrt:>6}%   (거래량 {vol})")
        except Exception as e:
            print(f"  {name}: 조회실패 {e}")


if __name__ == "__main__":
    main()
