"""Walk-Forward 백테스트 — 롤링 윈도우 in-sample/out-of-sample 검증.

기존 단일 백테스트의 한계(과적합)를 보완. 데이터를 윈도우로 나눠서:
  1. In-sample (IS): 파라미터 최적화
  2. Out-of-sample (OOS): 최적화된 파라미터로 검증

이를 롤링하며 반복 → OOS 결과를 이어붙여 실제 성과 추정.

사용:
    python -m src.backtest.walk_forward --symbol 069500 --from 2024-01-01 --to 2026-05-01
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from src.backtest.runner import load_history, run_backtest, BacktestResult
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy


@dataclass
class WFOResult:
    """Walk-Forward 최적화 전체 결과."""
    total_return: float
    annual_return: float
    mdd: float
    sharpe: float
    win_rate: float
    num_trades: int
    num_windows: int
    oos_results: list[WindowResult] = field(default_factory=list)
    robustness_ratio: float = 0.0  # OOS Sharpe / IS Sharpe


@dataclass
class WindowResult:
    """개별 윈도우 결과."""
    window_idx: int
    is_start: str
    is_end: str
    oos_start: str
    oos_end: str
    best_k: float
    best_ma: int
    is_sharpe: float
    oos_sharpe: float
    oos_return: float
    oos_trades: int


# 파라미터 탐색 범위
K_VALUES = [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7]
MA_VALUES = [10, 15, 20, 25, 30]


def walk_forward_optimize(
    symbol: str,
    start: str,
    end: str,
    is_days: int = 120,      # In-sample 기간 (거래일)
    oos_days: int = 20,      # Out-of-sample 기간 (거래일)
    step_days: int = 20,     # 윈도우 슬라이드 단위 (거래일)
    initial_capital: float = 10_000_000,
) -> WFOResult:
    """Walk-Forward 최적화 실행.

    Args:
        symbol: 종목코드
        start, end: 전체 기간 (YYYY-MM-DD)
        is_days: In-sample 거래일 수
        oos_days: Out-of-sample 거래일 수
        step_days: 다음 윈도우까지 이동할 거래일 수
        initial_capital: 초기 자본
    """
    history = load_history(symbol, start, end)
    if len(history) < is_days + oos_days:
        raise ValueError(f"데이터 부족: {len(history)}일 < IS({is_days})+OOS({oos_days})")

    print(f"Walk-Forward: {symbol} | IS={is_days}일, OOS={oos_days}일, Step={step_days}일")
    print(f"전체 데이터: {len(history)}일 ({start} ~ {end})")

    window_results: list[WindowResult] = []
    all_oos_equity: list[pd.Series] = []
    all_is_sharpes: list[float] = []
    all_oos_sharpes: list[float] = []

    idx = 0
    window_num = 0

    while idx + is_days + oos_days <= len(history):
        is_data = history.iloc[idx: idx + is_days]
        oos_data = history.iloc[idx + is_days: idx + is_days + oos_days]

        is_start_date = str(is_data.index[0].date())
        is_end_date = str(is_data.index[-1].date())
        oos_start_date = str(oos_data.index[0].date())
        oos_end_date = str(oos_data.index[-1].date())

        # In-sample 최적화
        best_k, best_ma, best_is_sharpe = _optimize_window(
            is_data, initial_capital
        )

        # Out-of-sample 검증
        strategy = VolatilityBreakoutStrategy(k=best_k, trend_ma=best_ma)

        # OOS는 IS의 마지막 lookback일도 포함해야 지표 계산 가능
        lookback = max(best_ma, 60)
        oos_with_lookback = history.iloc[
            max(0, idx + is_days - lookback): idx + is_days + oos_days
        ]
        oos_result = run_backtest(strategy, oos_with_lookback, initial_capital=initial_capital)

        wr = WindowResult(
            window_idx=window_num,
            is_start=is_start_date,
            is_end=is_end_date,
            oos_start=oos_start_date,
            oos_end=oos_end_date,
            best_k=best_k,
            best_ma=best_ma,
            is_sharpe=best_is_sharpe,
            oos_sharpe=oos_result.sharpe,
            oos_return=oos_result.total_return,
            oos_trades=oos_result.num_trades,
        )
        window_results.append(wr)
        all_is_sharpes.append(best_is_sharpe)
        all_oos_sharpes.append(oos_result.sharpe)

        print(f"  Window {window_num}: IS {is_start_date}~{is_end_date} "
              f"→ OOS {oos_start_date}~{oos_end_date} | "
              f"K={best_k} MA={best_ma} | "
              f"IS Sharpe={best_is_sharpe:.2f} → OOS Sharpe={oos_result.sharpe:.2f} "
              f"수익={oos_result.total_return:+.2%}")

        all_oos_equity.append(oos_result.equity_curve)

        idx += step_days
        window_num += 1

    if not window_results:
        raise ValueError("유효한 윈도우 없음")

    # OOS 종합 통계
    total_oos_return = 1.0
    total_trades = 0
    for wr in window_results:
        total_oos_return *= (1 + wr.oos_return)
        total_trades += wr.oos_trades
    total_oos_return -= 1

    avg_is_sharpe = np.mean(all_is_sharpes)
    avg_oos_sharpe = np.mean(all_oos_sharpes)
    robustness = avg_oos_sharpe / avg_is_sharpe if avg_is_sharpe > 0 else 0

    # OOS Sharpe의 승률 (양수 비율)
    oos_win_rate = sum(1 for s in all_oos_sharpes if s > 0) / len(all_oos_sharpes)

    # 연환산 수익률 추정
    total_oos_days = sum(wr.oos_trades for wr in window_results)
    total_calendar_days = max(
        (pd.Timestamp(window_results[-1].oos_end) -
         pd.Timestamp(window_results[0].oos_start)).days,
        1
    )
    annual_return = (1 + total_oos_return) ** (365 / total_calendar_days) - 1

    result = WFOResult(
        total_return=float(total_oos_return),
        annual_return=float(annual_return),
        mdd=0.0,  # 개별 윈도우 MDD 합산은 의미 제한적
        sharpe=float(avg_oos_sharpe),
        win_rate=float(oos_win_rate),
        num_trades=total_trades,
        num_windows=window_num,
        oos_results=window_results,
        robustness_ratio=float(robustness),
    )

    print(f"\n=== Walk-Forward 종합 ===")
    print(f"  윈도우 수: {window_num}")
    print(f"  OOS 누적 수익: {total_oos_return:+.2%}")
    print(f"  OOS 연환산:    {annual_return:+.2%}")
    print(f"  평균 IS Sharpe: {avg_is_sharpe:.2f}")
    print(f"  평균 OOS Sharpe: {avg_oos_sharpe:.2f}")
    print(f"  강건성 비율 (OOS/IS): {robustness:.2f}")
    print(f"  OOS 양의 Sharpe 비율: {oos_win_rate:.0%}")

    if robustness < 0.3:
        print("  [경고] 강건성 낮음 — 과적합 위험. 파라미터 범위 축소 권장.")
    elif robustness > 0.7:
        print("  [양호] 강건성 우수 — OOS에서도 전략 유효.")

    return result


def _optimize_window(data: pd.DataFrame, capital: float) -> tuple[float, int, float]:
    """단일 IS 윈도우에서 최적 K, MA 탐색.

    Returns:
        (best_k, best_ma, best_sharpe)
    """
    best_k = 0.5
    best_ma = 20
    best_sharpe = float("-inf")

    for k in K_VALUES:
        for ma in MA_VALUES:
            try:
                strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)
                result = run_backtest(strategy, data, initial_capital=capital)
                if result.num_trades < 3:
                    continue
                if result.sharpe > best_sharpe:
                    best_sharpe = result.sharpe
                    best_k = k
                    best_ma = ma
            except Exception:
                continue

    return best_k, best_ma, float(best_sharpe) if best_sharpe > float("-inf") else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-Forward 백테스트")
    parser.add_argument("--symbol", required=True, help="6자리 종목코드")
    parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--is-days", type=int, default=120, help="In-sample 거래일")
    parser.add_argument("--oos-days", type=int, default=20, help="Out-of-sample 거래일")
    parser.add_argument("--capital", type=float, default=10_000_000)
    args = parser.parse_args()

    walk_forward_optimize(
        args.symbol, args.start, args.end,
        is_days=args.is_days, oos_days=args.oos_days,
        initial_capital=args.capital,
    )


if __name__ == "__main__":
    main()
