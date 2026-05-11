"""기술적 지표 복합 스코어링 — 다중 TA 지표 기반 매매 판단.

6가지 기술적 지표를 분석해 -100 ~ +100 사이의 종합 점수를 산출.
점수가 높을수록 매수 유리, 낮을수록 매도/관망 신호.

사용 지표:
  1. RSI (14) — 과매수/과매도 판단
  2. MACD (12,26,9) — 추세 방향 + 히스토그램 모멘텀
  3. Bollinger Bands (20,2) — 밴드 내 위치 + 스퀴즈
  4. Stochastic (14,3) — 단기 모멘텀
  5. ADX (14) — 추세 강도 (방향성)
  6. 이동평균 정배열 (MA5 > MA10 > MA20 > MA60)

점수 체계:
  - 각 지표: -20 ~ +20점 (가중치 반영)
  - 합산: -100 ~ +100
  - BUY: +40 이상 | SELL: -40 이하 | HOLD: 나머지
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta as ta


@dataclass
class TAScore:
    """기술적 분석 종합 점수."""
    total: float          # -100 ~ +100
    rsi_score: float      # -20 ~ +20
    macd_score: float     # -20 ~ +20
    bb_score: float       # -15 ~ +15
    stoch_score: float    # -15 ~ +15
    adx_score: float      # -15 ~ +15
    ma_score: float       # -15 ~ +15
    signal: str           # "BUY" / "SELL" / "HOLD"
    detail: str           # 사람이 읽을 수 있는 요약

    # 기준값
    BUY_THRESHOLD = 40
    SELL_THRESHOLD = -40


def compute_ta_score(df: pd.DataFrame) -> TAScore:
    """OHLCV DataFrame에서 기술적 지표를 계산하고 종합 점수를 반환.

    Args:
        df: 컬럼 ['open','high','low','close','volume'], 최소 60행 이상.
            최신 행이 마지막.

    Returns:
        TAScore — 종합 점수와 개별 지표 점수
    """
    if len(df) < 60:
        return TAScore(
            total=0, rsi_score=0, macd_score=0, bb_score=0,
            stoch_score=0, adx_score=0, ma_score=0,
            signal="HOLD", detail=f"데이터 부족 ({len(df)} < 60)",
        )

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    # ── 1. RSI (14) — 가중치 20점 ──
    rsi_series = ta.rsi(close, length=14)
    rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else 50.0
    rsi_score = _score_rsi(rsi)

    # ── 2. MACD (12,26,9) — 가중치 20점 ──
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    macd_score = 0.0
    if macd_df is not None and not macd_df.empty:
        macd_line = float(macd_df.iloc[-1, 0])  # MACD
        signal_line = float(macd_df.iloc[-1, 2])  # Signal
        histogram = float(macd_df.iloc[-1, 1])  # Histogram
        macd_score = _score_macd(macd_line, signal_line, histogram)

    # ── 3. Bollinger Bands (20,2) — 가중치 15점 ──
    bb_df = ta.bbands(close, length=20, std=2)
    bb_score = 0.0
    if bb_df is not None and not bb_df.empty:
        bb_lower = float(bb_df.iloc[-1, 0])  # BBL
        bb_mid = float(bb_df.iloc[-1, 1])    # BBM
        bb_upper = float(bb_df.iloc[-1, 2])  # BBU
        cur_price = float(close.iloc[-1])
        bb_score = _score_bollinger(cur_price, bb_lower, bb_mid, bb_upper)

    # ── 4. Stochastic (14,3) — 가중치 15점 ──
    stoch_df = ta.stoch(high, low, close, k=14, d=3)
    stoch_score = 0.0
    if stoch_df is not None and not stoch_df.empty:
        stoch_k = float(stoch_df.iloc[-1, 0])  # %K
        stoch_d = float(stoch_df.iloc[-1, 1])  # %D
        stoch_score = _score_stochastic(stoch_k, stoch_d)

    # ── 5. ADX (14) — 가중치 15점 ──
    adx_df = ta.adx(high, low, close, length=14)
    adx_score = 0.0
    if adx_df is not None and not adx_df.empty:
        adx_val = float(adx_df.iloc[-1, 0])   # ADX
        dmp = float(adx_df.iloc[-1, 1])        # +DI
        dmn = float(adx_df.iloc[-1, 2])        # -DI
        adx_score = _score_adx(adx_val, dmp, dmn)

    # ── 6. 이동평균 정배열 — 가중치 15점 ──
    ma5 = float(ta.sma(close, length=5).iloc[-1]) if len(close) >= 5 else 0
    ma10 = float(ta.sma(close, length=10).iloc[-1]) if len(close) >= 10 else 0
    ma20 = float(ta.sma(close, length=20).iloc[-1]) if len(close) >= 20 else 0
    ma60 = float(ta.sma(close, length=60).iloc[-1]) if len(close) >= 60 else 0
    cur_price = float(close.iloc[-1])
    ma_score = _score_ma_alignment(cur_price, ma5, ma10, ma20, ma60)

    # ── 종합 ──
    total = rsi_score + macd_score + bb_score + stoch_score + adx_score + ma_score
    total = max(-100, min(100, total))

    if total >= TAScore.BUY_THRESHOLD:
        signal = "BUY"
    elif total <= TAScore.SELL_THRESHOLD:
        signal = "SELL"
    else:
        signal = "HOLD"

    detail = (
        f"TA={total:+.0f} "
        f"[RSI({rsi:.0f})={rsi_score:+.0f} "
        f"MACD={macd_score:+.0f} "
        f"BB={bb_score:+.0f} "
        f"Stoch={stoch_score:+.0f} "
        f"ADX={adx_score:+.0f} "
        f"MA={ma_score:+.0f}]"
    )

    return TAScore(
        total=round(total, 1),
        rsi_score=round(rsi_score, 1),
        macd_score=round(macd_score, 1),
        bb_score=round(bb_score, 1),
        stoch_score=round(stoch_score, 1),
        adx_score=round(adx_score, 1),
        ma_score=round(ma_score, 1),
        signal=signal,
        detail=detail,
    )


# ──────────────────────────────────────────────────────────
# 개별 지표 점수 산출 함수
# ──────────────────────────────────────────────────────────

def _score_rsi(rsi: float) -> float:
    """RSI 점수: -20 ~ +20.

    30 이하: 과매도 → 매수 기회 (+10~+20)
    70 이상: 과매수 → 매도 경고 (-10~-20)
    40~60: 중립 (0)
    """
    if rsi <= 20:
        return 20.0
    elif rsi <= 30:
        return 10.0 + (30 - rsi)  # 10~20
    elif rsi <= 40:
        return (40 - rsi)  # 0~10
    elif rsi <= 60:
        return 0.0
    elif rsi <= 70:
        return -(rsi - 60)  # 0~-10
    elif rsi <= 80:
        return -10.0 - (rsi - 70)  # -10~-20
    else:
        return -20.0


def _score_macd(macd: float, signal: float, histogram: float) -> float:
    """MACD 점수: -20 ~ +20.

    MACD > Signal (골든크로스 방향): +
    히스토그램 증가: 추가 가산
    """
    score = 0.0

    # MACD vs Signal 관계
    if macd > signal:
        score += 10.0
    else:
        score -= 10.0

    # 히스토그램 방향 (양수이면 상승 모멘텀)
    if histogram > 0:
        score += min(histogram * 2, 10.0)
    else:
        score += max(histogram * 2, -10.0)

    return max(-20, min(20, score))


def _score_bollinger(price: float, lower: float, mid: float, upper: float) -> float:
    """볼린저 밴드 점수: -15 ~ +15.

    하단 밴드 근처: 매수 기회 (+)
    상단 밴드 근처: 과열 경고 (-)
    중간선 위 상승: 약한 매수 (+)
    """
    if upper == lower:
        return 0.0

    # 밴드 내 위치 (0=하단, 1=상단)
    position = (price - lower) / (upper - lower)

    if position <= 0.1:
        return 15.0   # 하단 밴드 이탈 → 강한 반등 기대
    elif position <= 0.3:
        return 10.0   # 하단 근처
    elif position <= 0.5:
        return 5.0    # 중간선 아래
    elif position <= 0.7:
        return 0.0    # 중간선 위 (중립)
    elif position <= 0.9:
        return -7.0   # 상단 근처
    else:
        return -15.0  # 상단 밴드 이탈 → 과열


def _score_stochastic(k: float, d: float) -> float:
    """스토캐스틱 점수: -15 ~ +15.

    K < 20 + K > D: 과매도 반등 시작 → 매수
    K > 80 + K < D: 과매수 하락 시작 → 매도
    """
    score = 0.0

    # 과매도/과매수 영역
    if k < 20:
        score += 8.0
    elif k < 30:
        score += 4.0
    elif k > 80:
        score -= 8.0
    elif k > 70:
        score -= 4.0

    # K와 D의 교차
    if k > d:
        score += 7.0  # 상승 교차
    else:
        score -= 7.0  # 하락 교차

    return max(-15, min(15, score))


def _score_adx(adx: float, dmp: float, dmn: float) -> float:
    """ADX 점수: -15 ~ +15.

    ADX > 25: 추세 존재
      +DI > -DI: 상승 추세 → 매수
      -DI > +DI: 하락 추세 → 매도
    ADX < 20: 추세 없음 → 중립
    """
    if adx < 20:
        return 0.0  # 추세 없음 → 중립

    # 추세 강도에 따른 가중
    strength = min((adx - 20) / 30, 1.0)  # 20~50 → 0~1

    if dmp > dmn:
        return 15.0 * strength  # 상승 추세
    else:
        return -15.0 * strength  # 하락 추세


def _score_ma_alignment(price: float, ma5: float, ma10: float,
                        ma20: float, ma60: float) -> float:
    """이동평균 정배열 점수: -15 ~ +15.

    정배열 (가격>MA5>MA10>MA20>MA60): +15
    역배열 (가격<MA5<MA10<MA20<MA60): -15
    부분 정배열/역배열: 중간 점수
    """
    if ma5 == 0 or ma60 == 0:
        return 0.0

    score = 0.0

    # 각 관계 체크 (정배열 방향이면 +, 역배열이면 -)
    pairs = [
        (price, ma5),
        (ma5, ma10),
        (ma10, ma20),
        (ma20, ma60),
    ]

    for upper, lower in pairs:
        if upper > lower:
            score += 3.75  # 4개 × 3.75 = 15
        else:
            score -= 3.75

    return max(-15, min(15, score))
