"""pykrx로 외국인 수급 장기 히스토리 확보 가능성 확인.

KIS inquire-investor는 30일뿐. pykrx는 KRX에서 몇 년치 투자자별 매매를 준다.
어떤 함수/컬럼이 외국인 순매수(일별)를 주는지 탐색. 읽기 전용.
debug-once: script=scripts.debug_pykrx_flow
"""
from __future__ import annotations


def _try(label, fn):
    try:
        df = fn()
        if df is None or len(df) == 0:
            print(f"\n[{label}] 빈 결과")
            return None
        print(f"\n[{label}] OK — {len(df)}행")
        print(f"  컬럼: {list(df.columns)}")
        print(f"  인덱스 범위: {df.index[0]} ~ {df.index[-1]}")
        print("  마지막 3행:")
        print(df.tail(3).to_string())
        return df
    except Exception as e:  # noqa: BLE001
        print(f"\n[{label}] 실패: {type(e).__name__}: {e}")
        return None


def main() -> None:
    print("=" * 64)
    print("pykrx 외국인 수급 장기 히스토리 프로브")
    print("=" * 64)
    from pykrx import stock

    frm, to = "20240701", "20260709"

    # 1) KOSPI 지수(1001) 일별 투자자별 거래대금 — 시장 전체 수급(최선)
    _try("KOSPI지수 1001 일별 투자자거래대금",
         lambda: stock.get_market_trading_value_by_date(frm, to, "1001"))

    # 2) 069500 ETF 일별 투자자별 거래대금
    _try("069500 일별 투자자거래대금",
         lambda: stock.get_market_trading_value_by_date(frm, to, "069500"))

    # 3) 시장 전체(KOSPI) 순매수 — 다른 시그니처 폴백
    _try("KOSPI 투자자별 거래대금(집계)",
         lambda: stock.get_market_trading_value_by_investor(frm, to, "1001"))


if __name__ == "__main__":
    main()
