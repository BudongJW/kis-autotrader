"""HMM (Hidden Markov Model) 기반 시장 레짐 탐지.

GaussianHMM으로 시장 상태를 3가지로 분류:
  - Bull (상승): 높은 수익률 + 낮은 변동성
  - Bear (하락): 낮은/음의 수익률 + 높은 변동성
  - Sideways (횡보): 0 근처 수익률 + 중간 변동성

기존 선형회귀 기반 market_regime.py를 대체/보완.
HMM은 비선형 전환을 자연스럽게 포착하고, 레짐 전환 확률도 제공.

사용법:
    regime = detect_regime(daily_returns, daily_volatility)
    # regime.state = "bull" / "bear" / "sideways"
    # regime.confidence = 0.0 ~ 1.0
    # regime.transition_prob = {"bull": 0.7, "bear": 0.1, "sideways": 0.2}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from src.utils.logger import log

MODEL_CACHE_PATH = Path("logs/hmm_model.pkl")


@dataclass
class RegimeResult:
    """HMM 레짐 탐지 결과."""
    state: str                    # "bull" / "bear" / "sideways"
    confidence: float             # 현재 상태 확률 (0~1)
    transition_prob: dict[str, float] = field(default_factory=dict)
    means: dict[str, float] = field(default_factory=dict)
    detail: str = ""


def detect_regime(returns: pd.Series, volatility: pd.Series | None = None,
                  n_states: int = 3) -> RegimeResult:
    """일별 수익률(+변동성)로 HMM 레짐 탐지.

    Args:
        returns: 일별 수익률 시리즈 (최소 60일)
        volatility: 일별 실현 변동성 (5일 rolling std). None이면 자동 계산.
        n_states: HMM 상태 수 (기본 3)

    Returns:
        RegimeResult
    """
    try:
        from hmmlearn.hmm import GaussianHMM
    except ImportError:
        log.warning("hmmlearn_not_installed",
                    msg="pip install hmmlearn 필요. 폴백: 선형 분석")
        return _fallback_regime(returns)

    returns = returns.dropna()
    if len(returns) < 60:
        return RegimeResult(
            state="sideways", confidence=0.5,
            detail=f"데이터 부족 ({len(returns)} < 60)")

    # 피처 구성: [수익률, 변동성]
    if volatility is None:
        volatility = returns.rolling(5).std().fillna(returns.std())

    X = np.column_stack([returns.values, volatility.values])

    # NaN 제거
    mask = ~np.isnan(X).any(axis=1)
    X = X[mask]
    if len(X) < 60:
        return _fallback_regime(returns)

    # HMM 학습
    model = GaussianHMM(
        n_components=n_states,
        covariance_type="full",
        n_iter=200,
        random_state=42,
        tol=0.01,
    )

    try:
        model.fit(X)
    except Exception as e:
        log.warning("hmm_fit_failed", error=str(e))
        return _fallback_regime(returns)

    # 상태 디코딩
    hidden_states = model.predict(X)
    state_probs = model.predict_proba(X)

    # 각 상태의 평균 수익률로 bull/bear/sideways 매핑
    state_means = {i: float(model.means_[i, 0]) for i in range(n_states)}
    sorted_states = sorted(state_means, key=state_means.get)

    state_map = {}
    if n_states == 3:
        state_map[sorted_states[0]] = "bear"
        state_map[sorted_states[1]] = "sideways"
        state_map[sorted_states[2]] = "bull"
    elif n_states == 2:
        state_map[sorted_states[0]] = "bear"
        state_map[sorted_states[1]] = "bull"
    else:
        for i, s in enumerate(sorted_states):
            state_map[s] = f"state_{i}"

    # 현재 상태 (마지막 관측)
    current_state_idx = hidden_states[-1]
    current_state = state_map[current_state_idx]
    current_confidence = float(state_probs[-1, current_state_idx])

    # 전환 확률 (현재 상태에서 다른 상태로)
    transition_prob = {}
    for i in range(n_states):
        label = state_map[i]
        transition_prob[label] = float(model.transmat_[current_state_idx, i])

    # 상태별 평균 수익률
    means = {state_map[i]: float(model.means_[i, 0]) * 100
             for i in range(n_states)}

    detail = (f"HMM 레짐: {current_state} ({current_confidence:.0%}) | "
              f"평균수익: " +
              ", ".join(f"{k}={v:+.2f}%" for k, v in means.items()))

    return RegimeResult(
        state=current_state,
        confidence=current_confidence,
        transition_prob=transition_prob,
        means=means,
        detail=detail,
    )


def detect_regime_from_prices(prices: pd.Series, n_states: int = 3) -> RegimeResult:
    """종가 시리즈에서 직접 레짐 탐지.

    Args:
        prices: 일별 종가 시리즈 (최소 65일)
    """
    if len(prices) < 65:
        return RegimeResult(
            state="sideways", confidence=0.5,
            detail=f"가격 데이터 부족 ({len(prices)} < 65)")

    returns = prices.pct_change().dropna()
    volatility = returns.rolling(5).std()
    return detect_regime(returns, volatility, n_states)


def get_regime_action(regime: RegimeResult) -> dict:
    """레짐에 따른 매매 행동 가이드.

    Returns:
        {
            "allow_buy": bool,
            "reduce_size": bool,
            "k_adjustment": float,  # K값 조정 배수
            "reason": str,
        }
    """
    if regime.state == "bear":
        if regime.confidence > 0.7:
            return {
                "allow_buy": False,
                "reduce_size": True,
                "k_adjustment": 1.3,  # K 올려서 진입 어렵게
                "reason": f"약세장 ({regime.confidence:.0%}) — 매수 차단",
            }
        return {
            "allow_buy": True,
            "reduce_size": True,
            "k_adjustment": 1.2,
            "reason": f"약세 경향 ({regime.confidence:.0%}) — 포지션 축소",
        }

    if regime.state == "bull":
        return {
            "allow_buy": True,
            "reduce_size": False,
            "k_adjustment": 0.9,  # K 낮춰서 진입 쉽게
            "reason": f"강세장 ({regime.confidence:.0%}) — 공격적 진입",
        }

    # sideways
    return {
        "allow_buy": True,
        "reduce_size": False,
        "k_adjustment": 1.0,
        "reason": f"횡보장 ({regime.confidence:.0%}) — 기본 전략",
    }


def _fallback_regime(returns: pd.Series) -> RegimeResult:
    """HMM 실패 시 단순 통계 기반 폴백."""
    if len(returns) < 5:
        return RegimeResult(state="sideways", confidence=0.5,
                            detail="데이터 부족 (폴백)")

    avg_ret = float(returns.tail(20).mean()) if len(returns) >= 20 else float(returns.mean())
    recent_vol = float(returns.tail(5).std()) if len(returns) >= 5 else float(returns.std())
    long_vol = float(returns.tail(60).std()) if len(returns) >= 60 else recent_vol

    if avg_ret > 0.002 and recent_vol < long_vol * 1.2:
        state = "bull"
    elif avg_ret < -0.002 or recent_vol > long_vol * 1.5:
        state = "bear"
    else:
        state = "sideways"

    return RegimeResult(
        state=state,
        confidence=0.6,
        detail=f"폴백 분석: {state} (수익률 {avg_ret*100:+.2f}%, 변동성 비율 {recent_vol/long_vol:.1f}x)",
    )
