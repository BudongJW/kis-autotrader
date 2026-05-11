"""파라미터 옵티마이저 — 최근 데이터로 전략 파라미터를 자동 최적화.

매주 실행되어:
  1. 최근 6개월 데이터로 K값·MA 조합을 그리드서치
  2. Sharpe 비율 기준 최적 파라미터 선정
  3. ETF 후보군 중 최적 종목 재선별
  4. strategy.yaml 업데이트

사용:
    python -m src.optimizer
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import yaml

from src.backtest.runner import load_history, run_backtest
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.strategies.ta_composite import DEFAULT_WEIGHTS
from src.market_regime import analyze_regime

CONFIG_PATH = Path("configs/strategy.yaml")

# 최적화 대상 ETF 후보 (유동성 충분한 ETF)
ETF_CANDIDATES = [
    ("395160", "KODEX 미국나스닥100TR"),
    ("379800", "KODEX 미국S&P500(H)"),
    ("304660", "KODEX 미국S&P500TR"),
    ("381170", "TIGER 미국테크TOP10 INDXX"),
    ("133690", "TIGER 미국나스닥100"),
    ("143850", "TIGER 미국S&P500"),
    ("091160", "KODEX 반도체"),
    ("069500", "KODEX 200"),
    ("394670", "TIGER 미국필라델피아반도체나스닥"),
    ("229200", "KODEX 코스닥150"),
]

# 그리드서치 범위
K_VALUES = [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7]
MA_VALUES = [10, 15, 20, 25, 30]


@dataclass
class OptResult:
    symbol: str
    name: str
    k: float
    ma: int
    sharpe: float
    total_return: float
    mdd: float
    win_rate: float
    num_trades: int


def optimize() -> list[OptResult]:
    """전체 ETF × 파라미터 그리드서치 실행."""
    # 최근 6개월
    end = datetime.now()
    start = end - timedelta(days=180)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    print(f"최적화 기간: {start_str} ~ {end_str}")
    print(f"ETF 후보: {len(ETF_CANDIDATES)}개, 파라미터 조합: {len(K_VALUES) * len(MA_VALUES)}개")
    print()

    all_results: list[OptResult] = []

    for sym, name in ETF_CANDIDATES:
        try:
            history = load_history(sym, start_str, end_str)
            if len(history) < 60:
                continue
        except Exception:
            continue

        best = None
        for k in K_VALUES:
            for ma in MA_VALUES:
                try:
                    strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)
                    result = run_backtest(strategy, history, initial_capital=10_000_000)

                    if result.num_trades < 5:  # 거래 너무 적으면 통계적 무의미
                        continue

                    r = OptResult(
                        symbol=sym, name=name, k=k, ma=ma,
                        sharpe=result.sharpe, total_return=result.total_return,
                        mdd=result.mdd, win_rate=result.win_rate,
                        num_trades=result.num_trades,
                    )
                    if best is None or r.sharpe > best.sharpe:
                        best = r
                except Exception:
                    continue

        if best and best.sharpe > 0:
            all_results.append(best)
            print(f"  {name:<30} K={best.k} MA={best.ma} "
                  f"Sharpe={best.sharpe:.2f} 수익={best.total_return:+.2%} "
                  f"MDD={best.mdd:.2%} 승률={best.win_rate:.1%}")

    all_results.sort(key=lambda x: x.sharpe, reverse=True)
    return all_results


def update_config(results: list[OptResult]) -> None:
    """최적화 결과를 strategy.yaml에 반영."""
    if not results:
        print("\n수익성 있는 ETF 없음. 설정 변경 안 함.")
        return

    best = results[0]

    # 시장 환경 분석 (최적 종목 기준)
    end = datetime.now()
    start = end - timedelta(days=90)
    try:
        history = load_history(best.symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        regime = analyze_regime(history)
        # 시장 환경 추천 K와 백테스트 최적 K 중 보수적인 쪽 선택
        final_k = max(best.k, regime.recommended_k)
        print(f"\n시장 환경: 추세={regime.trend}, 변동성={regime.volatility}")
        print(f"  백테스트 최적 K={best.k}, 시장환경 추천 K={regime.recommended_k} → 적용 K={final_k}")
    except Exception:
        final_k = best.k
        regime = None

    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 전략 파라미터 업데이트
    cfg["strategies"]["volatility_breakout"] = {
        "k": final_k,
        "trend_ma": best.ma,
        "optimized_at": datetime.now().strftime("%Y-%m-%d"),
        "backtest_sharpe": round(best.sharpe, 2),
        "backtest_return": round(best.total_return * 100, 2),
        "backtest_mdd": round(best.mdd * 100, 2),
    }

    # TA 가중치 (향후 최적화 대상, 현재는 기본값 저장)
    cfg["strategies"]["ta_weights"] = {k: round(v, 3) for k, v in DEFAULT_WEIGHTS.items()}

    # universe 업데이트 (Sharpe > 0 상위 3개)
    top = results[:3]
    cfg["universe"]["default"] = [
        {"symbol": r.symbol, "name": r.name}
        for r in top
    ]

    # 시장 환경 기록
    if regime:
        cfg["market_regime"] = {
            "trend": regime.trend,
            "volatility": regime.volatility,
            "trend_score": regime.trend_score,
            "vol_percentile": regime.vol_percentile,
            "analyzed_at": datetime.now().strftime("%Y-%m-%d"),
        }

    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"\n=== strategy.yaml 업데이트 완료 ===")
    print(f"  최적 종목: {best.name} ({best.symbol})")
    print(f"  K={final_k}, MA={best.ma}")
    print(f"  Sharpe={best.sharpe:.2f}, 수익률={best.total_return:+.2%}, MDD={best.mdd:.2%}")
    if len(top) > 1:
        print(f"  후보 종목: {', '.join(r.name for r in top)}")


def main() -> None:
    print("=" * 60)
    print("KIS AutoTrader — 주간 파라미터 최적화")
    print("=" * 60)

    results = optimize()

    print(f"\n총 {len(results)}개 ETF에서 수익성 확인")
    if results:
        print("\n=== 상위 5개 ===")
        for i, r in enumerate(results[:5], 1):
            print(f"  {i}. {r.name:<28} K={r.k} MA={r.ma} "
                  f"Sharpe={r.sharpe:.2f} 수익={r.total_return:+.2%}")

    update_config(results)


if __name__ == "__main__":
    main()
