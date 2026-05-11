"""시장 환경 분석 — 추세/변동성 상태를 판별.

봇과 옵티마이저가 시장 상태에 따라 전략 파라미터를 조정할 수 있도록
현재 시장이 '추세장'인지 '횡보장'인지, 변동성이 높은지 낮은지를 판별한다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class MarketRegime:
    trend: str          # "up", "down", "sideways"
    volatility: str     # "high", "normal", "low"
    trend_score: float  # -1.0(강한하락) ~ +1.0(강한상승)
    vol_percentile: float  # 0~100, 최근 변동성의 과거 대비 위치
    recommended_k: float   # 시장 환경에 맞는 K값 추천


def analyze_regime(history: pd.DataFrame, lookback: int = 60) -> MarketRegime:
    """최근 lookback일 기준으로 시장 환경을 분석.

    Args:
        history: OHLCV DataFrame (close 필수)
        lookback: 분석 기간 (영업일)
    """
    close = history["close"].tail(lookback).astype(float)

    # ── 추세 판별 ──
    # 선형 회귀 기울기로 추세 강도 측정
    x = np.arange(len(close))
    slope = np.polyfit(x, close.values, 1)[0]
    # 기울기를 평균 가격 대비 % 변화율로 정규화
    trend_score = float(slope / close.mean() * len(close))
    trend_score = max(-1.0, min(1.0, trend_score))  # clamp

    if trend_score > 0.15:
        trend = "up"
    elif trend_score < -0.15:
        trend = "down"
    else:
        trend = "sideways"

    # ── 변동성 판별 ──
    daily_returns = close.pct_change().dropna()
    current_vol = float(daily_returns.tail(10).std())

    # 전체 기간 변동성 분포에서 현재 위치
    rolling_vol = daily_returns.rolling(10).std().dropna()
    if len(rolling_vol) > 0:
        vol_percentile = float((rolling_vol < current_vol).sum() / len(rolling_vol) * 100)
    else:
        vol_percentile = 50.0

    if vol_percentile > 75:
        volatility = "high"
    elif vol_percentile < 25:
        volatility = "low"
    else:
        volatility = "normal"

    # ── K값 추천 ──
    # 추세장 + 저변동성 → 낮은 K (진입 적극적)
    # 횡보장 + 고변동성 → 높은 K (진입 보수적)
    base_k = 0.5
    if trend in ("up",):
        base_k -= 0.1
    elif trend in ("down", "sideways"):
        base_k += 0.05
    if volatility == "high":
        base_k += 0.1
    elif volatility == "low":
        base_k -= 0.05
    recommended_k = max(0.3, min(0.7, round(base_k, 2)))

    return MarketRegime(
        trend=trend,
        volatility=volatility,
        trend_score=round(trend_score, 3),
        vol_percentile=round(vol_percentile, 1),
        recommended_k=recommended_k,
    )
