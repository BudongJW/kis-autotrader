"""일일 실적 기록 — 매일 장 마감 후 실행.

기록 내용:
  - 당일 거래 결과 (매수/매도 가격, 수량, 손익)
  - 계좌 잔고 스냅샷
  - 시장 환경 분석 결과
  - 대상 ETF 시세 정보

이 데이터가 축적되면 옵티마이저의 학습 데이터가 된다.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.kis_client import KISClient
from src.market_regime import analyze_regime
from src.bot.runner import fetch_recent_history
from src.bot.single_run import load_universe, get_all_holdings, get_available_cash
from src.tracker import get_summary

DAILY_LOG_PATH = Path("logs/daily_report.csv")
DAILY_FIELDS = [
    "date", "cash", "holdings_value", "total_value",
    "trend", "volatility", "trend_score", "vol_percentile",
    "recommended_k", "current_k", "pnl_cumulative",
]


def main() -> None:
    now = datetime.now()
    print(f"[{now:%Y-%m-%d %H:%M}] 일일 실적 기록")

    client = KISClient()
    universe = load_universe()

    # 계좌 상태
    cash = get_available_cash(client)
    holdings = get_all_holdings(client)

    holdings_value = 0
    for sym, qty in holdings.items():
        try:
            resp = client.get_price(sym)
            if resp.get("rt_cd") == "0":
                price = int(resp["output"]["stck_prpr"])
                holdings_value += price * qty
        except Exception:
            pass

    total_value = cash + holdings_value
    print(f"  현금: {cash:,}원 | 보유평가: {holdings_value:,}원 | 합계: {total_value:,}원")

    # 시장 환경 분석 (첫 번째 universe 종목 기준)
    regime = None
    if universe:
        try:
            sym = universe[0]["symbol"]
            history = fetch_recent_history(client, sym, days=30)
            regime = analyze_regime(history, lookback=30)
            print(f"  시장: 추세={regime.trend}({regime.trend_score:+.3f}), "
                  f"변동성={regime.volatility}({regime.vol_percentile:.0f}%), "
                  f"추천K={regime.recommended_k}")
        except Exception as e:
            print(f"  시장 분석 실패: {e}")

    # 누적 성과
    summary = get_summary()
    print(f"  누적 PnL: {summary['pnl']:+,}원 ({summary['pnl_pct']:+.2f}%)")

    # 현재 전략 파라미터
    from src.bot.single_run import load_strategy_params
    params = load_strategy_params()
    current_k = params.get("k", 0.5)

    # CSV 기록
    DAILY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not DAILY_LOG_PATH.exists()

    with DAILY_LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(DAILY_FIELDS)
        writer.writerow([
            now.strftime("%Y-%m-%d"),
            cash, holdings_value, total_value,
            regime.trend if regime else "",
            regime.volatility if regime else "",
            regime.trend_score if regime else "",
            regime.vol_percentile if regime else "",
            regime.recommended_k if regime else "",
            current_k,
            summary["pnl"],
        ])

    print(f"  기록 완료: {DAILY_LOG_PATH}")


if __name__ == "__main__":
    main()
