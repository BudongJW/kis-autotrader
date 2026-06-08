"""일일 실적 기록 + 장 후 학습 — 매일 장 마감 후 실행.

기록 내용:
  - 당일 거래 결과 (매수/매도 가격, 수량, 손익)
  - 계좌 잔고 스냅샷
  - 시장 환경 분석 결과 (레짐, HMM, 터뷸런스)
  - 포지션 사이징 상태 (Kelly, 기대값)
  - 대상 ETF 시세 정보

학습:
  - TA 신호 적중률 평가 → 가중치 피드백
  - 시장 패턴 분석
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.config import settings
from src.kis_client import KISClient
from src.market_regime import analyze_regime
from src.bot.runner import fetch_recent_history
from src.bot.single_run import load_universe, get_all_holdings, get_available_cash
from src.tracker import get_summary
from src.risk_manager import get_kelly_position_size, get_strategy_expectancy

DAILY_LOG_PATH = Path("logs/daily_report.csv")
CONFIG_PATH = Path("configs/strategy.yaml")
DAILY_FIELDS = [
    "date", "cash", "holdings_value", "total_value",
    "trend", "volatility", "trend_score", "vol_percentile",
    "hmm_state", "hmm_confidence", "market_confidence",
    "kelly_f", "recommended_k", "current_k", "pnl_cumulative",
    "num_trades_today", "day_pnl",
]


def main() -> None:
    now = datetime.now()
    print(f"[{now:%Y-%m-%d %H:%M}] 일일 실적 기록")

    client = KISClient()
    universe = load_universe()

    # 계좌 상태
    cash = get_available_cash(client)
    holdings = get_all_holdings(client) or {}  # 조회 실패(None)는 빈 표시로 처리

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

    # 시장 환경 분석
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

    # strategy.yaml에서 HMM 정보 읽기
    hmm_state = "unknown"
    hmm_confidence = 0
    market_confidence = 0.5
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        mr = cfg.get("market_regime", {})
        hmm_state = mr.get("hmm_state", "unknown")
        hmm_confidence = mr.get("hmm_confidence", 0)
        market_confidence = cfg.get("market_confidence", 0.5)
        print(f"  HMM: {hmm_state} ({hmm_confidence:.0%}) | 신뢰도: {market_confidence:.0%}")
    except Exception:
        pass

    # Kelly
    kelly_f = get_kelly_position_size("combined")
    print(f"  Kelly: {kelly_f:.1%}")

    # 누적 성과
    summary = get_summary()
    print(f"  누적 PnL: {summary['pnl']:+,}원 ({summary['pnl_pct']:+.2f}%)")

    # 현재 전략 파라미터
    from src.bot.single_run import load_strategy_params
    params = load_strategy_params()
    current_k = params.get("k", 0.5)

    # 오늘 거래 수
    today_str = now.strftime("%Y-%m-%d")
    today_trades = 0
    from src.tracker import TRADE_LOG_PATH
    if TRADE_LOG_PATH.exists():
        with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("timestamp", "").startswith(today_str):
                    today_trades += 1

    # 전일 대비 PnL
    day_pnl = 0
    portfolio_path = Path("journal/_data/portfolio.json")
    if portfolio_path.exists():
        try:
            with portfolio_path.open("r", encoding="utf-8") as f:
                pf = json.load(f)
            dh = pf.get("daily_history", [])
            if dh:
                last = dh[-1]
                if last.get("date") == today_str:
                    day_pnl = last.get("day_pnl", 0)
                else:
                    day_pnl = total_value - last.get("total_value", total_value)
        except Exception:
            pass

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
            hmm_state,
            hmm_confidence,
            market_confidence,
            round(kelly_f, 4),
            regime.recommended_k if regime else "",
            current_k,
            summary["pnl"],
            today_trades,
            day_pnl,
        ])

    print(f"  기록 완료: {DAILY_LOG_PATH}")

    # 장 후 학습 실행
    print()
    try:
        from src.market_learner import post_market
        post_market(client)
    except Exception as e:
        print(f"  장 후 학습 실패: {e}")


if __name__ == "__main__":
    main()
