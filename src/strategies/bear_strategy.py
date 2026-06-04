"""하락장 대응 전략 — 듀얼 모멘텀 + 카나리아 경보 + 인버스 돌파.

검증된 전략 기반:
  1. 듀얼 모멘텀 (Gary Antonacci GEM): 절대 + 상대 모멘텀으로 방향 결정
  2. VAA 카나리아 경보 (Keller & Keuning): 선행 자산 음전환 시 점진적 방어
  3. 히스테리시스 레짐 전환: 비대칭 임계값으로 휩소 방지
  4. 변동성 타겟팅: 변동성 비율로 포지션 크기 자동 조절

레짐별 행동:
  - BULL: 기존 롱 전략 유지
  - CAUTION: 포지션 축소 + 방어자산 일부 편입
  - BEAR: 인버스 ETF 변동성 돌파 + 채권 전환
  - CRISIS: 현금 + 단기채권 100% (인버스도 위험)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from src.utils.logger import log

CONFIG_PATH = Path("configs/strategy.yaml")
BEAR_STATE_PATH = Path("logs/bear_state.json")


# ──────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────

@dataclass
class MarketRegimeResult:
    """시장 레짐 판단 결과."""
    regime: str                     # "BULL" / "CAUTION" / "BEAR" / "CRISIS"
    confidence: float               # 0.0 ~ 1.0
    sma_ratio: float                # 현재가 / SMA200 비율
    canary_score: int               # 카나리아 음전환 수 (0~2)
    momentum_scores: dict = field(default_factory=dict)
    detail: str = ""


@dataclass
class BearAllocation:
    """하락장 포지션 배분 결과."""
    regime: str
    inverse_pct: float              # 인버스 ETF 비중
    defensive_pct: float            # 채권 ETF 비중
    long_pct: float                 # 기존 롱 ETF 비중
    cash_pct: float                 # 현금 비중
    vol_scale: float                # 변동성 타겟팅 스케일
    detail: str = ""


@dataclass
class StrategyPerformance:
    """전략 성과 기록 (지속적 학습용)."""
    regime: str
    action: str                     # "long" / "inverse" / "defensive" / "cash"
    entry_date: str
    exit_date: str = ""
    pnl_pct: float = 0.0
    holding_days: int = 0


# ──────────────────────────────────────────────────────────
# 가중 모멘텀 (VAA 스타일)
# ──────────────────────────────────────────────────────────

def weighted_momentum(prices: pd.Series,
                      months: list[int] | None = None,
                      weights: list[int] | None = None) -> float:
    """VAA 스타일 가중 모멘텀 점수.

    12 × (1개월 수익률) + 4 × (3개월) + 2 × (6개월) + 1 × (12개월)
    최근 모멘텀에 더 큰 가중치를 부여.
    """
    if months is None:
        months = [1, 3, 6, 12]
    if weights is None:
        weights = [12, 4, 2, 1]

    if len(prices) < 5:
        return 0.0

    score = 0.0
    for m, w in zip(months, weights):
        days = m * 21  # 거래일 기준
        if len(prices) > days:
            ret = float(prices.iloc[-1] / prices.iloc[-days - 1] - 1)
            score += w * ret
        else:
            # 데이터 부족 시 가용 기간으로 계산
            ret = float(prices.iloc[-1] / prices.iloc[0] - 1)
            score += w * ret

    return score


# ──────────────────────────────────────────────────────────
# 카나리아 경보 시스템
# ──────────────────────────────────────────────────────────

def check_canary(canary_histories: dict[str, pd.DataFrame],
                 cfg: dict | None = None) -> tuple[int, dict]:
    """카나리아 유니버스 모멘텀 확인.

    Args:
        canary_histories: {심볼: OHLCV DataFrame}
        cfg: bear_strategy 설정 (momentum_months, momentum_weights)

    Returns:
        (음전환 수, {심볼: 모멘텀 점수})
    """
    if cfg is None:
        cfg = {}
    canary_cfg = cfg.get("canary_alert", {})
    months = canary_cfg.get("momentum_months", [1, 3, 6, 12])
    weights = canary_cfg.get("momentum_weights", [12, 4, 2, 1])

    bad_count = 0
    scores = {}

    for sym, hist in canary_histories.items():
        if hist is None or len(hist) < 22:
            scores[sym] = 0.0
            continue
        score = weighted_momentum(hist["close"], months, weights)
        scores[sym] = round(score, 4)
        if score <= 0:
            bad_count += 1

    return bad_count, scores


# ──────────────────────────────────────────────────────────
# 레짐 판단 (SMA200 + 카나리아 + HMM 결합)
# ──────────────────────────────────────────────────────────

# 레짐 방어 등급 (클수록 방어적). escalate-only 병합에 사용.
REGIME_RANK = {"BULL": 0, "CAUTION": 1, "BEAR": 2, "CRISIS": 3}


def more_defensive(a: str, b: str) -> str:
    """두 레짐 중 더 방어적인(등급 높은) 쪽 반환. escalate-only 병합용."""
    return a if REGIME_RANK.get(a, 0) >= REGIME_RANK.get(b, 0) else b


def detect_rapid_decline(index_history: pd.DataFrame,
                         cfg: dict | None = None) -> dict:
    """지수(KODEX 200/SPY)의 1일·3일 누적 급락률로 빠른 위험 레벨 판정.

    느린 레짐(SMA200·카나리아)이 놓치는 급락을 조기에 잡는다.
    whipsaw 방지: 인버스(BEAR)는 3일 지속 급락에서만, 1일 단발은 CAUTION까지만,
    1일 극단은 CRISIS(현금)로 — 떨어지는 칼날에 인버스로 뛰어들지 않는다.

    Returns:
        {"level": "NONE"/"CAUTION"/"BEAR"/"CRISIS",
         "ret_1d": float|None, "ret_3d": float|None, "detail": str}
    """
    cfg = cfg or {}
    rc = cfg.get("rapid_decline", {}) or {}
    if not rc.get("enabled", True):
        return {"level": "NONE", "ret_1d": None, "ret_3d": None,
                "detail": "급락 트리거 비활성"}

    # 임계값 (보수적 기본값)
    caution_1d = rc.get("caution_1d", -0.03)
    caution_3d = rc.get("caution_3d", -0.05)
    bear_3d = rc.get("bear_3d", -0.08)
    crisis_1d = rc.get("crisis_1d", -0.08)
    crisis_3d = rc.get("crisis_3d", -0.12)

    if index_history is None or len(index_history) < 2:
        return {"level": "NONE", "ret_1d": None, "ret_3d": None,
                "detail": "데이터 부족"}

    closes = index_history["close"].astype(float)
    c0 = float(closes.iloc[-1])
    ret_1d = c0 / float(closes.iloc[-2]) - 1.0 if float(closes.iloc[-2]) > 0 else 0.0
    ret_3d = None
    if len(closes) >= 4 and float(closes.iloc[-4]) > 0:
        ret_3d = c0 / float(closes.iloc[-4]) - 1.0

    # CRISIS: 1일 극단 급락 또는 3일 누적 폭락 → 현금(인버스도 회피)
    if ret_1d <= crisis_1d or (ret_3d is not None and ret_3d <= crisis_3d):
        return {"level": "CRISIS", "ret_1d": ret_1d, "ret_3d": ret_3d,
                "detail": f"급락 CRISIS (1일 {ret_1d:+.1%}"
                          f"{f', 3일 {ret_3d:+.1%}' if ret_3d is not None else ''})"}

    # BEAR: 3일 지속 급락에서만 (인버스 허용) — whipsaw 방지
    if ret_3d is not None and ret_3d <= bear_3d:
        return {"level": "BEAR", "ret_1d": ret_1d, "ret_3d": ret_3d,
                "detail": f"3일 지속 급락 BEAR (3일 {ret_3d:+.1%})"}

    # CAUTION: 1일 단발 급락 또는 3일 완만한 하락 → 방어(인버스 X)
    if ret_1d <= caution_1d or (ret_3d is not None and ret_3d <= caution_3d):
        return {"level": "CAUTION", "ret_1d": ret_1d, "ret_3d": ret_3d,
                "detail": f"급락 경고 CAUTION (1일 {ret_1d:+.1%}"
                          f"{f', 3일 {ret_3d:+.1%}' if ret_3d is not None else ''})"}

    return {"level": "NONE", "ret_1d": ret_1d, "ret_3d": ret_3d, "detail": "정상"}


def detect_market_regime(
    kospi_history: pd.DataFrame,
    canary_histories: dict[str, pd.DataFrame],
    hmm_state: str = "unknown",
    hmm_confidence: float = 0.5,
    cfg: dict | None = None,
) -> MarketRegimeResult:
    """다층 레짐 판단.

    1층: SMA200 + 히스테리시스 (추세 방향)
    2층: 카나리아 모멘텀 (조기 경보)
    3층: HMM 상태 (확인)

    레짐 결정 로직:
      - CRISIS: SMA200 -7% 이하 + 카나리아 2개 음전환
      - BEAR:   SMA200 하회 + 확인 조건 충족
      - CAUTION: 카나리아 1개 음전환 또는 SMA200 근접
      - BULL:   SMA200 상회 + 카나리아 양호
    """
    if cfg is None:
        cfg = {}
    regime_cfg = cfg.get("regime_switch", {})

    sma_period = regime_cfg.get("sma_period", 200)
    bear_entry = regime_cfg.get("bear_entry_threshold", -0.03)
    bear_exit = regime_cfg.get("bear_exit_threshold", 0.02)

    # SMA200 비율 계산
    sma_ratio = 0.0
    if kospi_history is not None and len(kospi_history) >= sma_period:
        sma = float(kospi_history["close"].rolling(sma_period).mean().iloc[-1])
        cur = float(kospi_history["close"].iloc[-1])
        sma_ratio = (cur / sma) - 1.0 if sma > 0 else 0.0
    elif kospi_history is not None and len(kospi_history) >= 20:
        # SMA200 불가 시 SMA60으로 폴백
        sma = float(kospi_history["close"].rolling(min(60, len(kospi_history))).mean().iloc[-1])
        cur = float(kospi_history["close"].iloc[-1])
        sma_ratio = (cur / sma) - 1.0 if sma > 0 else 0.0

    # 카나리아 경보
    canary_bad, canary_scores = check_canary(canary_histories, cfg)

    # 이전 상태 로드 (히스테리시스)
    prev_state = _load_bear_state()
    was_bear = prev_state.get("regime") in ("BEAR", "CRISIS")

    # 레짐 결정
    details = []

    # CRISIS: 극단적 하락
    if sma_ratio < -0.07 and canary_bad >= 2:
        regime = "CRISIS"
        confidence = min(1.0, abs(sma_ratio) * 5 + 0.5)
        details.append(f"SMA200 {sma_ratio:+.1%} (극단 하락) + 카나리아 {canary_bad}/2 음전환")

    # BEAR: 확인된 하락장
    elif sma_ratio < bear_entry and canary_bad >= 1:
        regime = "BEAR"
        confidence = min(1.0, abs(sma_ratio) * 5 + canary_bad * 0.2)
        details.append(f"SMA200 {sma_ratio:+.1%} < {bear_entry:+.1%} + 카나리아 {canary_bad}/2 음전환")

    elif was_bear and sma_ratio < bear_exit:
        # 히스테리시스: 하락장이었다면 +2% 돌파 전까지 유지
        regime = "BEAR"
        confidence = 0.6
        details.append(f"하락장 유지 (SMA200 {sma_ratio:+.1%} < 해제기준 {bear_exit:+.1%})")

    # CAUTION: 경고
    elif canary_bad >= 1 or (sma_ratio < 0 and sma_ratio >= bear_entry):
        regime = "CAUTION"
        confidence = 0.5 + canary_bad * 0.15
        reasons = []
        if canary_bad >= 1:
            reasons.append(f"카나리아 {canary_bad}/2 음전환")
        if sma_ratio < 0:
            reasons.append(f"SMA200 하회 ({sma_ratio:+.1%})")
        details.append(" + ".join(reasons))

    # BULL: 정상
    else:
        regime = "BULL"
        confidence = min(1.0, sma_ratio * 5 + 0.5)
        details.append(f"SMA200 {sma_ratio:+.1%} 상회, 카나리아 양호")

    # HMM 보정
    if hmm_state == "bear" and hmm_confidence > 0.6:
        if regime == "BULL":
            regime = "CAUTION"
            details.append(f"HMM bear ({hmm_confidence:.0%}) → CAUTION 격상")
        elif regime == "CAUTION":
            regime = "BEAR"
            details.append(f"HMM bear ({hmm_confidence:.0%}) → BEAR 격상")
    elif hmm_state == "bull" and hmm_confidence > 0.7:
        if regime == "CAUTION" and canary_bad == 0:
            regime = "BULL"
            details.append(f"HMM bull ({hmm_confidence:.0%}) + 카나리아 양호 → BULL 복귀")

    # 급락 빠른 트리거 (escalate-only): 느린 레짐(SMA200·카나리아)이 놓친 급락을
    # 조기에 더 방어적으로만 격상. 절대 덜 방어적으로 되돌리지 않는다.
    rapid = detect_rapid_decline(kospi_history, cfg)
    if rapid["level"] != "NONE":
        escalated = more_defensive(regime, rapid["level"])
        if escalated != regime:
            details.append(f"⚡급락 트리거 {rapid['level']} → {regime}에서 {escalated} 격상 "
                           f"({rapid['detail']})")
            regime = escalated
            confidence = max(confidence, 0.6)  # 급락은 확신 있는 방어 신호

    result = MarketRegimeResult(
        regime=regime,
        confidence=min(1.0, confidence),
        sma_ratio=round(sma_ratio, 4),
        canary_score=canary_bad,
        momentum_scores=canary_scores,
        detail=" | ".join(details),
    )

    # 상태 저장
    _save_bear_state({
        "regime": regime,
        "confidence": round(result.confidence, 3),
        "sma_ratio": round(sma_ratio, 4),
        "canary_bad": canary_bad,
        "canary_scores": canary_scores,
    })

    return result


# ──────────────────────────────────────────────────────────
# 포지션 배분 (레짐별)
# ──────────────────────────────────────────────────────────

def compute_bear_allocation(
    regime: MarketRegimeResult,
    current_vol: float,
    cfg: dict | None = None,
) -> BearAllocation:
    """레짐별 자산 배분 결정.

    Args:
        regime: 레짐 판단 결과
        current_vol: 현재 연간 변동성 (20일 rolling std × √252)
        cfg: bear_strategy 설정

    변동성 타겟팅:
        목표 변동성 대비 현재 변동성 비율로 전체 포지션 스케일링.
        고변동성 → 자동 축소, 저변동성 → 자동 확대.
    """
    if cfg is None:
        cfg = {}
    alloc_cfg = cfg.get("bear_allocation", {})
    target_vol = cfg.get("volatility_target", 0.12)

    # 변동성 타겟팅 스케일
    vol_scale = 1.0
    if current_vol > 0 and target_vol > 0:
        vol_scale = min(1.5, target_vol / current_vol)
        vol_scale = max(0.2, vol_scale)

    r = regime.regime

    if r == "CRISIS":
        # 위기: 현금 + 단기채 100%. 인버스도 위험할 수 있음.
        return BearAllocation(
            regime=r, inverse_pct=0.0, defensive_pct=0.30,
            long_pct=0.0, cash_pct=0.70, vol_scale=vol_scale,
            detail="CRISIS: 현금 70% + 단기채 30%. 모든 위험자산 회피.",
        )

    if r == "BEAR":
        inv_max = alloc_cfg.get("inverse_max_pct", 0.30)
        def_min = alloc_cfg.get("defensive_min_pct", 0.40)
        cash_min = alloc_cfg.get("cash_min_pct", 0.20)

        # 확신도에 비례하여 인버스 비중 조절
        inv_pct = inv_max * regime.confidence * vol_scale
        inv_pct = min(inv_max, inv_pct)
        def_pct = max(def_min, 1.0 - inv_pct - cash_min)
        cash_pct = max(cash_min, 1.0 - inv_pct - def_pct)

        return BearAllocation(
            regime=r, inverse_pct=round(inv_pct, 2),
            defensive_pct=round(def_pct, 2),
            long_pct=0.0, cash_pct=round(cash_pct, 2),
            vol_scale=round(vol_scale, 2),
            detail=f"BEAR: 인버스 {inv_pct:.0%} + 채권 {def_pct:.0%} + 현금 {cash_pct:.0%}",
        )

    if r == "CAUTION":
        # 점진적 방어: 카나리아 수에 따라 배분
        canary_factor = regime.canary_score / 2.0  # 0.0 ~ 1.0
        long_pct = max(0.3, 0.7 * (1 - canary_factor) * vol_scale)
        def_pct = min(0.5, 0.2 + canary_factor * 0.3)
        cash_pct = max(0.1, 1.0 - long_pct - def_pct)

        return BearAllocation(
            regime=r, inverse_pct=0.0,
            defensive_pct=round(def_pct, 2),
            long_pct=round(long_pct, 2),
            cash_pct=round(cash_pct, 2),
            vol_scale=round(vol_scale, 2),
            detail=f"CAUTION: 롱 {long_pct:.0%} + 채권 {def_pct:.0%} + 현금 {cash_pct:.0%}",
        )

    # BULL
    long_pct = min(1.0, 1.0 * vol_scale)
    return BearAllocation(
        regime=r, inverse_pct=0.0, defensive_pct=0.0,
        long_pct=round(long_pct, 2), cash_pct=round(1.0 - long_pct, 2),
        vol_scale=round(vol_scale, 2),
        detail=f"BULL: 롱 {long_pct:.0%} (변동성 스케일 {vol_scale:.2f})",
    )


# ──────────────────────────────────────────────────────────
# 인버스 ETF 변동성 돌파 (하락장에서 수익 추구)
# ──────────────────────────────────────────────────────────

def inverse_breakout_signal(
    inverse_history: pd.DataFrame,
    k: float = 0.5,
    trend_ma: int = 20,
) -> dict:
    """인버스 ETF 에 변동성 돌파 적용.

    인버스 ETF는 시장 하락 시 가격이 상승하므로,
    동일한 변동성 돌파 로직을 적용해도 하락장에서 매수 신호가 발생.

    Returns:
        {"breakout": bool, "price": float, "target": float, "reason": str}
    """
    if inverse_history is None or len(inverse_history) < trend_ma + 5:
        return {"breakout": False, "price": 0, "target": 0,
                "reason": "데이터 부족"}

    today = inverse_history.iloc[-1]
    yesterday = inverse_history.iloc[-2]
    cur_price = float(today["close"])

    prev_range = float(yesterday["high"]) - float(yesterday["low"])
    target_price = float(today["open"]) + prev_range * k

    ma = inverse_history["close"].rolling(trend_ma).mean()
    above_trend = cur_price > float(ma.iloc[-1])

    breakout = cur_price >= target_price

    if breakout and above_trend:
        return {
            "breakout": True,
            "price": cur_price,
            "target": target_price,
            "reason": f"인버스 돌파 (목표 {target_price:,.0f}, K={k}, MA{trend_ma} 위)",
        }

    reason = (f"미돌파 (현재 {cur_price:,.0f} < 목표 {target_price:,.0f})"
              if not breakout
              else f"추세 미통과 (MA{trend_ma} 아래)")
    return {"breakout": False, "price": cur_price, "target": target_price,
            "reason": reason}


# ──────────────────────────────────────────────────────────
# 변동성 계산
# ──────────────────────────────────────────────────────────

def compute_annualized_vol(history: pd.DataFrame, window: int = 20) -> float:
    """최근 window일 기준 연간화 변동성.

    Returns:
        연간 변동성 (0.15 = 15%). 계산 불가 시 0.20 (보수적 기본값).
    """
    if history is None or len(history) < window + 1:
        return 0.20

    try:
        returns = history["close"].pct_change().dropna()
        if len(returns) < window:
            return 0.20
        rolling_std = float(returns.tail(window).std())
        return rolling_std * math.sqrt(252)
    except Exception:
        return 0.20


# ──────────────────────────────────────────────────────────
# 전략 성과 추적 (지속적 학습)
# ──────────────────────────────────────────────────────────

PERF_LOG_PATH = Path("logs/bear_performance.json")


def log_bear_trade(regime: str, action: str, symbol: str,
                   price: float, date: str, **kwargs) -> None:
    """하락장 전략 거래 기록."""
    records = _load_performance_log()
    entry = {
        "regime": regime,
        "action": action,
        "symbol": symbol,
        "price": price,
        "date": date,
        **kwargs,
    }
    records.append(entry)
    # 최근 500건만 유지
    if len(records) > 500:
        records = records[-500:]
    _save_performance_log(records)


def get_regime_performance(regime: str, lookback_trades: int = 50) -> dict:
    """특정 레짐에서의 전략별 성과 통계.

    지속적 학습: 축적된 데이터로 레짐별 최적 행동을 자동 파악.
    """
    records = _load_performance_log()
    regime_records = [r for r in records if r.get("regime") == regime][-lookback_trades:]

    if len(regime_records) < 5:
        return {"sufficient_data": False, "trades": len(regime_records)}

    by_action = {}
    for r in regime_records:
        action = r.get("action", "unknown")
        pnl = r.get("pnl_pct", 0)
        if action not in by_action:
            by_action[action] = {"count": 0, "total_pnl": 0.0, "wins": 0}
        by_action[action]["count"] += 1
        by_action[action]["total_pnl"] += pnl
        if pnl > 0:
            by_action[action]["wins"] += 1

    stats = {}
    for action, data in by_action.items():
        n = data["count"]
        stats[action] = {
            "count": n,
            "avg_pnl": round(data["total_pnl"] / n, 4) if n > 0 else 0,
            "win_rate": round(data["wins"] / n, 3) if n > 0 else 0,
        }

    # 최적 행동 추천
    best_action = max(stats, key=lambda a: stats[a]["avg_pnl"]) if stats else "cash"

    return {
        "sufficient_data": True,
        "trades": len(regime_records),
        "stats": stats,
        "recommended_action": best_action,
    }


def get_adaptive_params(regime: str) -> dict:
    """축적된 성과 데이터로 파라미터 자동 조정.

    학습된 결과:
      - 특정 레짐에서 인버스가 효과적이면 인버스 비중 ↑
      - 특정 레짐에서 현금이 최선이면 현금 비중 ↑
      - K값, 보유기간 등도 성과 기반 조정
    """
    perf = get_regime_performance(regime)

    adjustments = {
        "inverse_scale": 1.0,
        "defensive_scale": 1.0,
        "k_adjustment": 0.0,
        "reason": "기본 파라미터",
    }

    if not perf.get("sufficient_data"):
        return adjustments

    stats = perf.get("stats", {})

    # 인버스 성과가 좋으면 비중 증가
    inv_stats = stats.get("inverse", {})
    if inv_stats.get("count", 0) >= 5:
        if inv_stats["avg_pnl"] > 0.005:  # 평균 +0.5% 이상
            adjustments["inverse_scale"] = 1.3
            adjustments["reason"] = f"인버스 학습 효과 (+{inv_stats['avg_pnl']:.1%})"
        elif inv_stats["avg_pnl"] < -0.005:  # 평균 -0.5% 이하
            adjustments["inverse_scale"] = 0.5
            adjustments["reason"] = f"인버스 손실 학습 ({inv_stats['avg_pnl']:.1%})"

    # 방어 성과 반영
    def_stats = stats.get("defensive", {})
    if def_stats.get("count", 0) >= 5:
        if def_stats["win_rate"] > 0.6:
            adjustments["defensive_scale"] = 1.2
        elif def_stats["win_rate"] < 0.3:
            adjustments["defensive_scale"] = 0.8

    return adjustments


# ──────────────────────────────────────────────────────────
# 상태 저장/로드 (히스테리시스용)
# ──────────────────────────────────────────────────────────

def _load_bear_state() -> dict:
    if BEAR_STATE_PATH.exists():
        try:
            with BEAR_STATE_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_bear_state(state: dict) -> None:
    BEAR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BEAR_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _load_performance_log() -> list:
    if PERF_LOG_PATH.exists():
        try:
            with PERF_LOG_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_performance_log(records: list) -> None:
    PERF_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PERF_LOG_PATH.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
