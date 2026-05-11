"""백테스트 러너 — 과거 데이터로 전략 검증.

사용:
    python -m src.backtest.runner --strategy golden_cross --symbol 005930 \
        --from 2023-01-01 --to 2024-12-31

산출:
    - 누적 수익률, 연환산, MDD, Sharpe, 승률, 거래 횟수
    - HTML 리포트 (선택: reports/<symbol>_<strategy>_<timestamp>.html)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from src.strategies.base import BaseStrategy, SignalType
from src.strategies.golden_cross import GoldenCrossStrategy

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "golden_cross": GoldenCrossStrategy,
}


@dataclass
class BacktestResult:
    total_return: float
    annual_return: float
    mdd: float
    sharpe: float
    win_rate: float
    num_trades: int
    equity_curve: pd.Series


def load_history(symbol: str, start: str, end: str) -> pd.DataFrame:
    """과거 시세 로드.

    TODO: 집에서 실제 KIS API 또는 pykrx 등으로 채울 것.
    현재는 더미 데이터를 반환하므로 백테스트 실행은 가능하지만 결과는 무의미.
    """
    dates = pd.date_range(start=start, end=end, freq="B")
    rng = np.random.default_rng(seed=42)
    # 단순 랜덤워크 (실제로는 KIS API의 inquire-daily-itemchartprice 사용)
    returns = rng.normal(loc=0.0003, scale=0.015, size=len(dates))
    close = 70000 * np.cumprod(1 + returns)
    df = pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.003, len(dates))),
            "high": close * (1 + np.abs(rng.normal(0, 0.005, len(dates)))),
            "low": close * (1 - np.abs(rng.normal(0, 0.005, len(dates)))),
            "close": close,
            "volume": rng.integers(1_000_000, 10_000_000, len(dates)),
        },
        index=dates,
    )
    return df


def run_backtest(
    strategy: BaseStrategy,
    history: pd.DataFrame,
    *,
    initial_capital: float = 1_000_000,
    fee_rate: float = 0.00015,  # 한투 일반 수수료 (가정)
    tax_rate: float = 0.0023,   # 매도 시 증권거래세
) -> BacktestResult:
    capital = initial_capital
    position_qty = 0
    position_avg_price = 0.0
    equity_curve = []
    trades: list[float] = []  # 거래별 손익 (%)

    for i in range(strategy.required_lookback, len(history)):
        window = history.iloc[: i + 1]
        signal = strategy.generate_signal("BACKTEST", window)
        price = float(window["close"].iloc[-1])

        if signal.type == SignalType.BUY and position_qty == 0:
            qty = int(capital * 0.95 // price)  # 95% 투입, 수수료 여유
            if qty > 0:
                cost = qty * price * (1 + fee_rate)
                capital -= cost
                position_qty = qty
                position_avg_price = price
        elif signal.type == SignalType.SELL and position_qty > 0:
            proceeds = position_qty * price * (1 - fee_rate - tax_rate)
            pnl_pct = (price - position_avg_price) / position_avg_price
            trades.append(pnl_pct)
            capital += proceeds
            position_qty = 0
            position_avg_price = 0.0

        equity = capital + position_qty * price
        equity_curve.append(equity)

    equity_series = pd.Series(equity_curve, index=history.index[strategy.required_lookback :])
    total_return = equity_series.iloc[-1] / initial_capital - 1
    days = (equity_series.index[-1] - equity_series.index[0]).days
    annual_return = (1 + total_return) ** (365 / max(days, 1)) - 1 if days > 0 else 0.0

    daily_returns = equity_series.pct_change().dropna()
    sharpe = (
        np.sqrt(252) * daily_returns.mean() / daily_returns.std()
        if daily_returns.std() > 0
        else 0.0
    )

    rolling_max = equity_series.cummax()
    drawdown = (equity_series - rolling_max) / rolling_max
    mdd = float(drawdown.min())

    win_rate = sum(1 for t in trades if t > 0) / len(trades) if trades else 0.0

    return BacktestResult(
        total_return=float(total_return),
        annual_return=float(annual_return),
        mdd=mdd,
        sharpe=float(sharpe),
        win_rate=float(win_rate),
        num_trades=len(trades),
        equity_curve=equity_series,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 백테스트 러너")
    parser.add_argument("--strategy", required=True, choices=list(STRATEGY_REGISTRY.keys()))
    parser.add_argument("--symbol", required=True, help="6자리 종목코드")
    parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=1_000_000)
    args = parser.parse_args()

    strategy = STRATEGY_REGISTRY[args.strategy]()
    history = load_history(args.symbol, args.start, args.end)
    result = run_backtest(strategy, history, initial_capital=args.capital)

    print(f"\n=== 백테스트 결과: {args.strategy} on {args.symbol} ===")
    print(f"기간:         {args.start} ~ {args.end}")
    print(f"초기 자본:    {args.capital:,.0f}원")
    print(f"누적 수익률:  {result.total_return:+.2%}")
    print(f"연환산:       {result.annual_return:+.2%}")
    print(f"MDD:          {result.mdd:.2%}")
    print(f"Sharpe:       {result.sharpe:.2f}")
    print(f"승률:         {result.win_rate:.2%}")
    print(f"거래 횟수:    {result.num_trades}")
    print(
        f"\n⚠️ 현재 load_history()는 더미 데이터. 실제 운영 전 KIS API 또는 pykrx로 교체 필요."
    )


if __name__ == "__main__":
    main()
