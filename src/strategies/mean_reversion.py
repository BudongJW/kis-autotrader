"""평균회귀 전략 — 변동성 돌파의 보완.

변동성 돌파는 추세장에서 강하지만 횡보장에서 매수 신호 거의 없음.
평균회귀는 가격이 단기 평균에서 일탈했을 때 회귀를 기대하고 진입.

3가지 신호 결합 (적어도 2개 충족 시 매수):
  1. RSI(7) 과매도 (< 30): 단기 과매도
  2. Bollinger Band 하단 터치: 가격이 -2σ 이탈
  3. VWAP 이탈: 가격이 VWAP 대비 -1σ 이탈

추가 필터:
  - 추세 확인: 60일 MA보다 위에 있을 때만 (하락 추세에선 매수 안 함)
  - 거래량 확인: 평균보다 활발한 종목만

신호 강도:
  - score: 0~100, 60 이상이면 매수 후보
  - 변동성 돌파가 우선이고, 미발생 시 평균회귀 신호 확인

사용:
  from src.strategies.mean_reversion import compute_mean_reversion_signal
  sig = compute_mean_reversion_signal(history)
  if sig.is_buy:
      print(f"  [평균회귀] {sig.reason}")
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


# 임계값
RSI_OVERSOLD = 30           # RSI(7) 이 미만 = 과매도
BB_LOWER_TOUCH_BP = -150    # BB 하단 -1.5σ 이탈 (basis points 단위)
VWAP_DEVIATION_BP = -150    # VWAP -1.5% 이탈
TREND_MA_PERIOD = 60        # 60일 이동평균
MIN_SCORE_FOR_BUY = 45      # 0-100 점수 (was 60 — active mode 완화)


@dataclass
class MeanReversionSignal:
    is_buy: bool
    score: float                # 0~100
    reason: str
    rsi: float
    bb_position_pct: float      # BB 내 위치 (0=하단, 100=상단)
    vwap_deviation_pct: float
    in_uptrend: bool            # 60일 MA 위
    detail: dict


def _rsi(series: pd.Series, period: int = 7) -> float:
    """단순 RSI 계산 (Wilder smoothing 없이)."""
    if len(series) < period + 1:
        return 50.0
    delta = series.diff().dropna()
    gains = delta.where(delta > 0, 0)
    losses = -delta.where(delta < 0, 0)
    avg_gain = gains.tail(period).mean()
    avg_loss = losses.tail(period).mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _bollinger_position(close: pd.Series, period: int = 20, num_std: float = 2.0):
    """Bollinger Band 내 위치 (0=하단, 100=상단)."""
    if len(close) < period:
        return 50.0
    window = close.tail(period)
    mean = window.mean()
    std = window.std()
    if std == 0:
        return 50.0
    upper = mean + num_std * std
    lower = mean - num_std * std
    current = close.iloc[-1]
    pos = (current - lower) / (upper - lower) * 100
    return float(max(0, min(100, pos)))


def _vwap(history: pd.DataFrame, period: int = 20) -> float:
    """단순 VWAP — 최근 N일 종가·거래량 가중평균."""
    if len(history) < period:
        return float(history["close"].iloc[-1])
    recent = history.tail(period)
    typical = (recent["high"] + recent["low"] + recent["close"]) / 3
    vol_sum = recent["volume"].sum()
    if vol_sum == 0:
        return float(recent["close"].iloc[-1])
    return float((typical * recent["volume"]).sum() / vol_sum)


def compute_mean_reversion_signal(history: pd.DataFrame) -> MeanReversionSignal:
    """평균회귀 신호 계산. history는 일봉 (최소 60일 필요)."""
    if history is None or len(history) < TREND_MA_PERIOD:
        return MeanReversionSignal(
            False, 0.0, f"데이터 부족 ({len(history) if history is not None else 0} < {TREND_MA_PERIOD})",
            50.0, 50.0, 0.0, False, {},
        )

    close = history["close"].astype(float)
    current = float(close.iloc[-1])

    # 1. RSI(7) 과매도
    rsi = _rsi(close, period=7)
    rsi_oversold = rsi < RSI_OVERSOLD

    # 2. Bollinger Band 위치
    bb_pos = _bollinger_position(close, period=20, num_std=2.0)
    bb_lower_touch = bb_pos < 20  # 하단 20% 이내

    # 3. VWAP 이탈
    vwap = _vwap(history, period=20)
    vwap_dev_pct = (current - vwap) / vwap * 100 if vwap > 0 else 0.0
    vwap_below = vwap_dev_pct < -1.5

    # 추세 필터: 60일 MA 위에 있어야 함 (하락장 매수 회피)
    ma60 = close.tail(TREND_MA_PERIOD).mean()
    in_uptrend = current > ma60 * 0.97  # 60일 MA의 -3% 이내

    # 신호 점수화 (0~100)
    score = 0.0
    reasons = []
    if rsi_oversold:
        # RSI가 낮을수록 점수 높음
        score += min(40, (RSI_OVERSOLD - rsi) * 2)
        reasons.append(f"RSI({rsi:.0f})↓")
    if bb_lower_touch:
        score += 30
        reasons.append(f"BB하단({bb_pos:.0f}%)")
    if vwap_below:
        score += 30
        reasons.append(f"VWAP{vwap_dev_pct:+.1f}%")

    # 하락 추세면 신호 무효화
    if not in_uptrend:
        score *= 0.3
        reasons.append("(하락추세 페널티)")

    is_buy = score >= MIN_SCORE_FOR_BUY

    return MeanReversionSignal(
        is_buy=is_buy,
        score=round(score, 1),
        reason=" + ".join(reasons) if reasons else "신호 없음",
        rsi=round(rsi, 1),
        bb_position_pct=round(bb_pos, 1),
        vwap_deviation_pct=round(vwap_dev_pct, 2),
        in_uptrend=in_uptrend,
        detail={
            "rsi_oversold": rsi_oversold,
            "bb_lower_touch": bb_lower_touch,
            "vwap_below": vwap_below,
            "in_uptrend": in_uptrend,
            "score": round(score, 1),
        },
    )
