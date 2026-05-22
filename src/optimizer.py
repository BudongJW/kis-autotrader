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
    sharpe: float                          # 학습 구간 Sharpe
    total_return: float
    mdd: float
    win_rate: float
    num_trades: int
    # 학습/검증 분리 — quant-platform 방식 (과적합 1차 필터)
    val_sharpe: float = 0.0                # 검증 구간 Sharpe
    val_return: float = 0.0
    val_mdd: float = 0.0
    val_num_trades: int = 0
    val_passed: bool = False               # 검증 통과 여부 (val_sharpe >= train_sharpe * 0.5)


# 학습/검증 split 비율 (전체 180일 → 학습 126일, 검증 54일)
TRAIN_RATIO = 0.7
# 검증 합격 기준: 검증 Sharpe가 학습 Sharpe의 최소 50% 이상
VALIDATION_THRESHOLD = 0.5
# 검증 구간 최소 거래 횟수
MIN_VAL_TRADES = 3


def _split_history(history, train_ratio: float = TRAIN_RATIO):
    """history를 학습/검증으로 시간순 분할."""
    split_idx = int(len(history) * train_ratio)
    train = history.iloc[:split_idx]
    val = history.iloc[split_idx:]
    return train, val


def _evaluate_on_validation(k: float, ma: int, val_history) -> dict:
    """검증 구간에서 동일 파라미터로 backtest. 결과 dict 반환."""
    if len(val_history) < ma + 5:
        return {"sharpe": 0.0, "total_return": 0.0, "mdd": 0.0,
                "num_trades": 0, "passed": False}
    try:
        strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)
        r = run_backtest(strategy, val_history, initial_capital=10_000_000)
        return {
            "sharpe": float(r.sharpe),
            "total_return": float(r.total_return),
            "mdd": float(r.mdd),
            "num_trades": int(r.num_trades),
            "passed": False,  # 호출자가 train_sharpe와 비교 후 결정
        }
    except Exception:
        return {"sharpe": 0.0, "total_return": 0.0, "mdd": 0.0,
                "num_trades": 0, "passed": False}


def _optuna_objective(trial, history, sym, name):
    """Optuna objective: 학습 구간 Sharpe 최대화 (MDD 페널티 포함).

    history는 학습 구간만 (검증은 study 완료 후 별도 평가).
    """
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
    """전체 ETF × 학습/검증 분리 + Bayesian 최적화 (Optuna TPE).

    1. 180일 데이터를 학습 70%(126일) / 검증 30%(54일)로 시간순 분할
    2. 학습 구간에서 best params 탐색
    3. 검증 구간에서 동일 params로 backtest → val_sharpe 측정
    4. val_sharpe >= train_sharpe * 0.5 인 종목만 val_passed=True 표시
    5. update_config는 val_passed=True인 결과만 strategy.yaml에 반영
    """
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
    print(f"학습/검증 분리: {int(TRAIN_RATIO*100)}% / {int((1-TRAIN_RATIO)*100)}%"
          f" (검증 합격 기준: val_sharpe ≥ train × {VALIDATION_THRESHOLD:.0%})")
    print(f"ETF 후보: {len(ETF_CANDIDATES)}개 | 방식: {method}")
    print()

    all_results: list[OptResult] = []
    passed_count = 0
    rejected_count = 0

    for sym, name in ETF_CANDIDATES:
        try:
            history = load_history(sym, start_str, end_str)
            if len(history) < 90:
                continue
        except Exception:
            continue

        train_hist, val_hist = _split_history(history)

        # 학습 구간에서 best params 탐색
        if use_optuna:
            best = _optimize_optuna(train_hist, sym, name, optuna)
        else:
            best = _optimize_grid(train_hist, sym, name)
        if not best or best.sharpe <= 0:
            continue

        # 검증 구간에서 cross-check
        val = _evaluate_on_validation(best.k, best.ma, val_hist)
        best.val_sharpe = val["sharpe"]
        best.val_return = val["total_return"]
        best.val_mdd = val["mdd"]
        best.val_num_trades = val["num_trades"]
        # 합격 기준: 검증 Sharpe가 학습의 50% 이상 + 최소 거래 횟수
        best.val_passed = (
            val["sharpe"] >= best.sharpe * VALIDATION_THRESHOLD
            and val["num_trades"] >= MIN_VAL_TRADES
        )

        all_results.append(best)
        if best.val_passed:
            passed_count += 1
            tag = "✓"
        else:
            rejected_count += 1
            tag = "✗"

        print(f"  [{tag}] {name:<30} K={best.k} MA={best.ma} | "
              f"학습 Sharpe={best.sharpe:.2f}({best.num_trades}건) "
              f"수익={best.total_return:+.1%} | "
              f"검증 Sharpe={best.val_sharpe:.2f}({best.val_num_trades}건) "
              f"수익={best.val_return:+.1%}")

    print(f"\n검증 통과: {passed_count}개 | 거부: {rejected_count}개")
    all_results.sort(key=lambda x: (x.val_passed, x.sharpe), reverse=True)
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
    """최적화 결과를 strategy.yaml에 반영. 검증 통과(val_passed=True) 결과만 채택."""
    if not results:
        print("\n수익성 있는 ETF 없음. 설정 변경 안 함.")
        return

    # 검증 통과한 결과만 필터링 (과적합 1차 차단)
    validated = [r for r in results if r.val_passed]
    if not validated:
        print("\n⚠️  검증 통과한 ETF 없음 (전부 과적합 의심). 설정 변경 안 함.")
        print("    학습 구간에서만 잘 작동하고 검증 구간에선 무너지는 파라미터들.")
        return

    best = validated[0]
    print(f"\n=== 검증 통과 최우수: {best.name} ===")
    print(f"    학습 Sharpe={best.sharpe:.2f} → 검증 Sharpe={best.val_sharpe:.2f}")

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

    # 전략 파라미터 업데이트 (검증 메트릭도 함께 저장)
    cfg["strategies"]["volatility_breakout"] = {
        "k": final_k,
        "trend_ma": best.ma,
        "optimized_at": datetime.now().strftime("%Y-%m-%d"),
        "train_sharpe": round(best.sharpe, 2),
        "train_return": round(best.total_return * 100, 2),
        "train_mdd": round(best.mdd * 100, 2),
        "val_sharpe": round(best.val_sharpe, 2),
        "val_return": round(best.val_return * 100, 2),
        "val_mdd": round(best.val_mdd * 100, 2),
        # 기존 키도 backwards 호환으로 유지
        "backtest_sharpe": round(best.sharpe, 2),
        "backtest_return": round(best.total_return * 100, 2),
        "backtest_mdd": round(best.mdd * 100, 2),
    }

    # TA 가중치 (향후 최적화 대상, 현재는 기본값 저장)
    cfg["strategies"]["ta_weights"] = {k: round(v, 3) for k, v in DEFAULT_WEIGHTS.items()}

    # universe 업데이트 — 검증 통과 상위 3개만
    top = validated[:3]
    cfg["universe"]["default"] = [
        {"symbol": r.symbol, "name": r.name}
        for r in top
    ]
    print(f"\n검증 통과 상위 3개로 universe 업데이트:")
    for r in top:
        print(f"  {r.name} (학습 Sharpe={r.sharpe:.2f}, 검증 Sharpe={r.val_sharpe:.2f})")

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
