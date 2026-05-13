"""파라미터 옵티마이저 — Optuna Bayesian 최적화 + 그리드서치 폴백.

매주 실행되어:
  1. 최근 6개월 데이터로 K값·MA·손절% 등을 Bayesian 최적화
  2. Sharpe 비율 기준 최적 파라미터 선정 (TPE sampler)
  3. ETF 후보군 중 최적 종목 재선별
  4. TA 가중치도 함께 최적화
  5. strategy.yaml 업데이트

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


def _optuna_objective(trial, history, sym, name):
    """Optuna objective: Sharpe ratio 최대화."""
    k = trial.suggest_float("k", 0.3, 0.7, step=0.05)
    ma = trial.suggest_int("trend_ma", 10, 30, step=5)

    strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)
    result = run_backtest(strategy, history, initial_capital=10_000_000)

    if result.num_trades < 5:
        return float("-inf")

    # 목적함수: Sharpe (MDD 패널티 포함)
    # MDD가 -15% 이상이면 페널티
    mdd_penalty = max(0, (-result.mdd - 0.15)) * 5
    score = result.sharpe - mdd_penalty

    trial.set_user_attr("total_return", result.total_return)
    trial.set_user_attr("mdd", result.mdd)
    trial.set_user_attr("win_rate", result.win_rate)
    trial.set_user_attr("num_trades", result.num_trades)

    return score


def optimize() -> list[OptResult]:
    """전체 ETF × Bayesian 최적화 (Optuna TPE)."""
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        use_optuna = True
    except ImportError:
        print("optuna 미설치 — 그리드서치 폴백")
        use_optuna = False

    end = datetime.now()
    start = end - timedelta(days=180)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    method = "Bayesian (Optuna TPE)" if use_optuna else "Grid Search"
    print(f"최적화 기간: {start_str} ~ {end_str}")
    print(f"ETF 후보: {len(ETF_CANDIDATES)}개 | 방식: {method}")
    print()

    all_results: list[OptResult] = []

    for sym, name in ETF_CANDIDATES:
        try:
            history = load_history(sym, start_str, end_str)
            if len(history) < 60:
                continue
        except Exception:
            continue

        if use_optuna:
            best = _optimize_optuna(history, sym, name, optuna)
        else:
            best = _optimize_grid(history, sym, name)

        if best and best.sharpe > 0:
            all_results.append(best)
            print(f"  {name:<30} K={best.k} MA={best.ma} "
                  f"Sharpe={best.sharpe:.2f} 수익={best.total_return:+.2%} "
                  f"MDD={best.mdd:.2%} 승률={best.win_rate:.1%}")

    all_results.sort(key=lambda x: x.sharpe, reverse=True)
    return all_results


def _optimize_optuna(history, sym, name, optuna) -> OptResult | None:
    """Optuna TPE로 최적 파라미터 탐색."""
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(
        lambda trial: _optuna_objective(trial, history, sym, name),
        n_trials=50,
        show_progress_bar=False,
    )

    if study.best_trial.value == float("-inf"):
        return None

    t = study.best_trial
    return OptResult(
        symbol=sym,
        name=name,
        k=t.params["k"],
        ma=t.params["trend_ma"],
        sharpe=round(t.value, 2),
        total_return=t.user_attrs["total_return"],
        mdd=t.user_attrs["mdd"],
        win_rate=t.user_attrs["win_rate"],
        num_trades=t.user_attrs["num_trades"],
    )


def _optimize_grid(history, sym, name) -> OptResult | None:
    """그리드서치 폴백 (Optuna 미설치 시)."""
    best = None
    for k in K_VALUES:
        for ma in MA_VALUES:
            try:
                strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)
                result = run_backtest(strategy, history, initial_capital=10_000_000)
                if result.num_trades < 5:
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
    return best


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


def train_lgbm_model() -> None:
    """LightGBM 예측 모델 재학습 (상위 3개 ETF 데이터 사용)."""
    try:
        from src.strategies.lgbm_predictor import LGBMPredictor
    except ImportError:
        print("\n[LGBM] lightgbm 미설치 — 스킵")
        return

    print("\n=== LightGBM 모델 학습 ===")
    end = datetime.now()
    start = end - timedelta(days=365)

    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    universe = cfg.get("universe", {}).get("default", [])
    if not universe:
        universe = [{"symbol": "069500", "name": "KODEX 200"}]

    # 유니버스 종목들의 데이터를 합쳐서 학습
    all_data = []
    for stock in universe[:3]:
        try:
            hist = load_history(stock["symbol"],
                                start.strftime("%Y-%m-%d"),
                                end.strftime("%Y-%m-%d"))
            if len(hist) >= 120:
                all_data.append(hist)
                print(f"  {stock['name']}: {len(hist)}일 로드")
        except Exception as e:
            print(f"  {stock['name']}: 로드 실패 ({e})")

    if not all_data:
        print("  학습 데이터 없음")
        return

    # 가장 긴 데이터로 학습 (대표 종목)
    best_data = max(all_data, key=len)
    predictor = LGBMPredictor()
    result = predictor.train(best_data)
    print(f"  정확도: {result['accuracy']:.1%}, AUC: {result['auc']:.3f}")


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
    train_lgbm_model()


if __name__ == "__main__":
    main()
