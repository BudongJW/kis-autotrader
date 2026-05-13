"""기술적 지표 복합 스코어링 — 다중 TA 지표 기반 매매 판단.

9가지 기술적 지표를 분석해 -100 ~ +100 사이의 종합 점수를 산출.
점수가 높을수록 매수 유리, 낮을수록 매도/관망 신호.

사용 지표:
  1. RSI (14) — 과매수/과매도 판단
  2. MACD (12,26,9) — 추세 방향 + 히스토그램 모멘텀
  3. Bollinger Bands (20,2) — 밴드 내 위치 + 스퀴즈
  4. Stochastic (14,3) — 단기 모멘텀
  5. ADX (14) — 추세 강도 (방향성)
  6. 이동평균 정배열 (MA5 > MA10 > MA20 > MA60)
  7. OBV (On-Balance Volume) — 거래량 기반 매수/매도 압력
  8. MFI (14) — 자금 흐름 지수 (거래량 가중 RSI)
  9. ATR 비율 — 변동성 정규화 (현재 ATR / 평균 ATR)

점수 체계:
  - 각 지표: 가중치 반영 점수
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
    obv_score: float = 0.0     # -10 ~ +10
    mfi_score: float = 0.0     # -10 ~ +10
    atr_score: float = 0.0     # -10 ~ +10
    signal: str = "HOLD"  # "BUY" / "SELL" / "HOLD"
    detail: str = ""      # 사람이 읽을 수 있는 요약

    # 기본 기준값 (레짐별 동적 조정은 get_regime_thresholds에서)
    BUY_THRESHOLD = 40
    SELL_THRESHOLD = -40


# 레짐별 TA 임계값: 상승장은 진입 완화, 하락장은 엄격
REGIME_THRESHOLDS = {
    "bull":     {"buy": 30, "sell": -30},   # 상승장: 관대한 진입
    "bear":     {"buy": 55, "sell": -50},   # 하락장: 엄격한 진입
    "sideways": {"buy": 40, "sell": -40},   # 횡보장: 기본값
    "unknown":  {"buy": 40, "sell": -40},
}

# 레짐별 TA 지표 해석 모드
# 상승장: 모멘텀 지표(MACD, MA) 비중 ↑, 역추세(RSI 과매수) 비중 ↓
# 하락장: 역추세 지표(RSI 과매도, BB 하단) 비중 ↑, confluence 요구
REGIME_WEIGHT_ADJUSTMENTS = {
    "bull": {
        "macd": 1.3,   # 모멘텀 강화
        "ma": 1.3,
        "adx": 1.2,
        "rsi": 0.7,    # RSI 과매수 패널티 완화
        "bb": 0.8,
    },
    "bear": {
        "rsi": 1.3,    # 과매도 반등 신호 강화
        "bb": 1.3,     # 하단 밴드 반등 강화
        "macd": 0.8,   # 모멘텀 추종 약화
        "ma": 0.7,
        "obv": 1.3,    # 거래량 다이버전스 중시
    },
    "sideways": {},    # 기본 가중치
}


def get_regime_thresholds() -> tuple[float, float, str]:
    """strategy.yaml에서 현재 레짐 기반 TA 임계값 반환.

    Returns:
        (buy_threshold, sell_threshold, regime_name)
    """
    try:
        import yaml
        from pathlib import Path as _P
        with _P("configs/strategy.yaml").open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        hmm_state = cfg.get("market_regime", {}).get("hmm_state", "unknown")
        thresholds = REGIME_THRESHOLDS.get(hmm_state, REGIME_THRESHOLDS["unknown"])
        return thresholds["buy"], thresholds["sell"], hmm_state
    except Exception:
        return TAScore.BUY_THRESHOLD, TAScore.SELL_THRESHOLD, "unknown"



# 기본 가중치 (합계 = 1.0). 옵티마이저가 strategy.yaml에서 덮어쓸 수 있음.
DEFAULT_WEIGHTS = {
    "rsi": 0.17,
    "macd": 0.17,
    "bb": 0.12,
    "stoch": 0.12,
    "adx": 0.12,
    "ma": 0.12,
    "obv": 0.06,
    "mfi": 0.06,
    "atr": 0.06,
}


def compute_ta_score(df: pd.DataFrame, weights: dict | None = None,
                     regime: str | None = None) -> TAScore:
    """OHLCV DataFrame에서 기술적 지표를 계산하고 종합 점수를 반환.

    Args:
        df: 컬럼 ['open','high','low','close','volume'], 최소 60행 이상.
        weights: 지표별 가중치 dict. None이면 DEFAULT_WEIGHTS 사용.
        regime: HMM 레짐 ("bull"/"bear"/"sideways"). None이면 자동 로드.

    Returns:
        TAScore — 종합 점수와 개별 지표 점수
    """
    w = dict(weights or DEFAULT_WEIGHTS)

    # 레짐 로드 및 가중치 조정
    if regime is None:
        _, _, regime = get_regime_thresholds()
    regime_adj = REGIME_WEIGHT_ADJUSTMENTS.get(regime, {})
    for k, mult in regime_adj.items():
        if k in w:
            w[k] = w[k] * mult
    # 재정규화
    w_total = sum(w.values())
    if w_total > 0:
        w = {k: v / w_total for k, v in w.items()}
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

    # ── 7. OBV (On-Balance Volume) — 가중치 10점 ──
    volume = df["volume"].astype(float)
    obv_score = _score_obv(close, volume)

    # ── 8. MFI (14) — 가중치 10점 ──
    mfi_series = ta.mfi(high, low, close, volume, length=14)
    mfi_score = 0.0
    if mfi_series is not None and not mfi_series.empty:
        mfi_val = float(mfi_series.iloc[-1])
        mfi_score = _score_mfi(mfi_val)

    # ── 9. ATR 비율 — 가중치 10점 ──
    atr_score = _score_atr_ratio(high, low, close)

    # ── 종합 (가중치 적용) ──
    # 각 raw score를 -1~+1로 정규화 후 가중합산, 다시 -100~+100 스케일
    raw_scores = {
        "rsi": rsi_score / 20.0,      # -20~+20 → -1~+1
        "macd": macd_score / 20.0,
        "bb": bb_score / 15.0,
        "stoch": stoch_score / 15.0,
        "adx": adx_score / 15.0,
        "ma": ma_score / 15.0,
        "obv": obv_score / 10.0,
        "mfi": mfi_score / 10.0,
        "atr": atr_score / 10.0,
    }
    weighted = sum(raw_scores[k] * w.get(k, 0) for k in raw_scores)
    total = weighted * 100  # -100 ~ +100
    total = max(-100, min(100, total))

    buy_th, sell_th, _ = get_regime_thresholds()
    if total >= buy_th:
        signal = "BUY"
    elif total <= sell_th:
        signal = "SELL"
    else:
        signal = "HOLD"

    detail = (
        f"TA={total:+.0f}({regime}) "
        f"[RSI({rsi:.0f})={rsi_score:+.0f} "
        f"MACD={macd_score:+.0f} "
        f"BB={bb_score:+.0f} "
        f"Stoch={stoch_score:+.0f} "
        f"ADX={adx_score:+.0f} "
        f"MA={ma_score:+.0f} "
        f"OBV={obv_score:+.0f} "
        f"MFI={mfi_score:+.0f} "
        f"ATR={atr_score:+.0f}]"
    )

    return TAScore(
        total=round(total, 1),
        rsi_score=round(rsi_score, 1),
        macd_score=round(macd_score, 1),
        bb_score=round(bb_score, 1),
        stoch_score=round(stoch_score, 1),
        adx_score=round(adx_score, 1),
        ma_score=round(ma_score, 1),
        obv_score=round(obv_score, 1),
        mfi_score=round(mfi_score, 1),
        atr_score=round(atr_score, 1),
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


def _score_obv(close: pd.Series, volume: pd.Series) -> float:
    """OBV 점수: -10 ~ +10.

    OBV 추세 (20일 선형회귀 방향)와 가격 추세의 다이버전스를 감지.
    OBV 상승 + 가격 상승: 건강한 상승 (+)
    OBV 하락 + 가격 상승: 약세 다이버전스 (-)
    OBV 상승 + 가격 하락: 강세 다이버전스 (+)
    """
    if len(close) < 20:
        return 0.0

    obv = ta.obv(close, volume)
    if obv is None or obv.empty:
        return 0.0

    # 최근 20일 OBV 추세
    obv_recent = obv.tail(20)
    obv_slope = float(np.polyfit(range(len(obv_recent)), obv_recent.values, 1)[0])

    # 최근 20일 가격 추세
    price_recent = close.tail(20)
    price_slope = float(np.polyfit(range(len(price_recent)), price_recent.values, 1)[0])

    obv_up = obv_slope > 0
    price_up = price_slope > 0

    if obv_up and price_up:
        return 7.0    # 건강한 상승
    elif obv_up and not price_up:
        return 10.0   # 강세 다이버전스 → 매수 기회
    elif not obv_up and price_up:
        return -8.0   # 약세 다이버전스 → 경고
    else:
        return -5.0   # 하락 확인


def _score_mfi(mfi: float) -> float:
    """MFI (Money Flow Index) 점수: -10 ~ +10.

    MFI < 20: 과매도 (매수 기회)
    MFI > 80: 과매수 (매도 경고)
    MFI 40~60: 중립
    """
    if mfi <= 10:
        return 10.0
    elif mfi <= 20:
        return 7.0
    elif mfi <= 30:
        return 4.0
    elif mfi <= 40:
        return 2.0
    elif mfi <= 60:
        return 0.0
    elif mfi <= 70:
        return -2.0
    elif mfi <= 80:
        return -5.0
    elif mfi <= 90:
        return -8.0
    else:
        return -10.0


def _score_atr_ratio(high: pd.Series, low: pd.Series, close: pd.Series) -> float:
    """ATR 비율 점수: -10 ~ +10.

    현재 ATR(14) / 평균 ATR(60) 비율로 변동성 상태를 판단.
    비율 < 0.8: 변동성 수축 (스퀴즈) → 돌파 기대 (+)
    비율 > 1.5: 변동성 급등 → 리스크 주의 (-)
    비율 0.8~1.2: 정상 (0)
    """
    atr_14 = ta.atr(high, low, close, length=14)
    atr_60 = ta.atr(high, low, close, length=60)

    if atr_14 is None or atr_60 is None or atr_14.empty or atr_60.empty:
        return 0.0

    current_atr = float(atr_14.iloc[-1])
    avg_atr = float(atr_60.iloc[-1])

    if avg_atr <= 0:
        return 0.0

    ratio = current_atr / avg_atr

    if ratio < 0.6:
        return 8.0    # 극도의 수축 → 큰 돌파 기대
    elif ratio < 0.8:
        return 5.0    # 수축 → 돌파 기대
    elif ratio < 1.2:
        return 0.0    # 정상 범위
    elif ratio < 1.5:
        return -3.0   # 약간의 확장
    elif ratio < 2.0:
        return -7.0   # 높은 변동성
    else:
        return -10.0  # 극도의 변동성 → 위험
