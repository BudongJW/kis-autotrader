"""신호 확률적 융합 — 다중 신호를 가중 결합하여 최종 매수 확률 산출.

기존 방식: TA → LGBM → 순차 거부 (약한 신호 여러 개 합쳐도 무시됨)
신규 방식: 모든 신호를 확률로 변환 → 가중 결합 → 최종 확률

시그모이드 기반 결합:
  final_prob = sigmoid(w1*ta_norm + w2*lgbm_logit + w3*breakout + w4*gap + w5*regime - bias)

가중치는 과거 거래 결과로 학습됨 (logs/fusion_weights.json).
학습 데이터 부족 시 휴리스틱 기본값 사용.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from src.utils.logger import log

FUSION_WEIGHTS_PATH = Path("logs/fusion_weights.json")


@dataclass
class FusionResult:
    """신호 융합 결과."""
    final_prob: float          # 최종 매수 확률 (0.0 ~ 1.0)
    signal: str                # "BUY" / "SKIP" / "STRONG_BUY"
    confidence: float          # 확신도 (0.0 ~ 1.0)
    components: dict           # 각 신호의 기여도
    detail: str


# 기본 가중치 (학습 전)
DEFAULT_WEIGHTS = {
    "ta_score": 0.25,       # TA 복합 점수 기여
    "lgbm_prob": 0.30,      # LGBM 예측 확률 기여
    "breakout": 0.20,       # 변동성 돌파 신호 기여
    "overnight_gap": 0.10,  # 미국장 갭 기여
    "regime": 0.15,         # 시장 레짐 기여
    "bias": -0.1,           # 보수적 바이어스
}

# 매수 임계값
BUY_THRESHOLD = 0.55
STRONG_BUY_THRESHOLD = 0.70


def _sigmoid(x: float) -> float:
    """안전한 시그모이드."""
    x = max(-10, min(10, x))
    return 1.0 / (1.0 + math.exp(-x))


def _load_weights() -> dict:
    """학습된 가중치 로드."""
    if FUSION_WEIGHTS_PATH.exists():
        try:
            with FUSION_WEIGHTS_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("trained") and data.get("weights"):
                return data["weights"]
        except Exception:
            pass
    return dict(DEFAULT_WEIGHTS)


def _save_weights(weights: dict, metrics: dict) -> None:
    """학습된 가중치 저장."""
    FUSION_WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FUSION_WEIGHTS_PATH.open("w", encoding="utf-8") as f:
        json.dump({
            "trained": True,
            "weights": weights,
            "metrics": metrics,
        }, f, ensure_ascii=False, indent=2)


def fuse_signals(
    ta_score: float,           # -100 ~ +100
    lgbm_prob: float,          # 0.0 ~ 1.0 (모델 없으면 0.5)
    breakout_signal: bool,     # True = 돌파 발생
    overnight_gap: dict | None = None,    # {"direction": str, "strength": float}
    regime: str = "unknown",   # "bull" / "bear" / "sideways"
    regime_confidence: float = 0.5,
    market_confidence: float = 0.5,
) -> FusionResult:
    """모든 신호를 가중 결합하여 최종 매수 확률 산출.

    각 신호를 -1 ~ +1 범위로 정규화한 후, 가중합 → 시그모이드.
    """
    w = _load_weights()

    # ── 신호 정규화 (-1 ~ +1) ──

    # TA: -100~+100 → -1~+1
    ta_norm = max(-1, min(1, ta_score / 100.0))

    # LGBM: 0~1 → logit scale → -2~+2
    lgbm_logit = 0.0
    if lgbm_prob != 0.5:
        # 0.5를 중립으로, 편향을 로짓으로 변환 (더 극단적인 값에 더 큰 가중)
        lgbm_clamped = max(0.05, min(0.95, lgbm_prob))
        lgbm_logit = math.log(lgbm_clamped / (1 - lgbm_clamped))
        lgbm_logit = max(-2, min(2, lgbm_logit))
    lgbm_norm = lgbm_logit / 2.0  # → -1~+1

    # 돌파 신호: bool → 0.6 (있으면) / -0.3 (없으면)
    breakout_norm = 0.6 if breakout_signal else -0.3

    # 오버나이트 갭: direction + strength → -1~+1
    gap_norm = 0.0
    if overnight_gap:
        direction = overnight_gap.get("direction", "neutral")
        strength = overnight_gap.get("strength", 0)
        if direction == "bullish":
            gap_norm = min(1.0, strength)
        elif direction == "bearish":
            gap_norm = -min(1.0, strength)

    # 레짐: bull=+0.8, sideways=0, bear=-0.8 (확신도 반영)
    regime_map = {"bull": 0.8, "sideways": 0.0, "bear": -0.8}
    regime_norm = regime_map.get(regime, 0.0) * regime_confidence

    # ── 가중 결합 ──
    logit = (
        w.get("ta_score", 0.25) * ta_norm * 3.0 +
        w.get("lgbm_prob", 0.30) * lgbm_norm * 3.0 +
        w.get("breakout", 0.20) * breakout_norm * 3.0 +
        w.get("overnight_gap", 0.10) * gap_norm * 3.0 +
        w.get("regime", 0.15) * regime_norm * 3.0 +
        w.get("bias", -0.1)
    )

    final_prob = _sigmoid(logit)
    confidence = abs(final_prob - 0.5) * 2.0

    if final_prob >= STRONG_BUY_THRESHOLD:
        signal = "STRONG_BUY"
    elif final_prob >= BUY_THRESHOLD:
        signal = "BUY"
    else:
        signal = "SKIP"

    components = {
        "ta": round(ta_norm, 3),
        "lgbm": round(lgbm_norm, 3),
        "breakout": round(breakout_norm, 3),
        "gap": round(gap_norm, 3),
        "regime": round(regime_norm, 3),
    }

    detail = (f"융합={final_prob:.0%} [{signal}] "
              f"(TA={ta_norm:+.2f} LGBM={lgbm_norm:+.2f} "
              f"돌파={'O' if breakout_signal else 'X'} "
              f"갭={gap_norm:+.2f} 레짐={regime_norm:+.2f})")

    return FusionResult(
        final_prob=round(final_prob, 3),
        signal=signal,
        confidence=round(confidence, 3),
        components=components,
        detail=detail,
    )


def learn_fusion_weights() -> dict | None:
    """경험 버퍼에서 과거 거래 결과로 융합 가중치를 학습.

    Brier Score 최소화 (예측 확률 vs 실제 결과)로 가중치 최적화.
    단순 그리드 서치 방식 (파라미터 5개, 각 3단계 = 243개 조합).

    Returns:
        학습된 가중치 dict 또는 None (데이터 부족)
    """
    try:
        from src.experience import _load_experience
    except ImportError:
        return None

    records = _load_experience()

    # 평가 완료된 매수/스킵 결정만 사용
    evaluated = [r for r in records
                 if r.get("evaluated") and r.get("pnl_pct") is not None]

    if len(evaluated) < 20:
        return None

    # 학습 데이터 구성
    X = []  # [ta_norm, lgbm_norm, breakout, gap, regime]
    y = []  # 1=실제 수익, 0=실제 손실

    for r in evaluated:
        ta_scores = r.get("ta_scores", {})
        ta_total = ta_scores.get("total", 0)
        lgbm = r.get("lgbm_prob", 0.5)
        action = r.get("action", "skip")
        pnl = r.get("pnl_pct", 0)

        ta_norm = max(-1, min(1, ta_total / 100.0))
        lgbm_norm = 0.0
        if lgbm and lgbm != 0.5:
            lgbm_clamped = max(0.05, min(0.95, lgbm))
            lgbm_norm = math.log(lgbm_clamped / (1 - lgbm_clamped)) / 2.0

        had_breakout = r.get("breakout_signal", action == "buy")
        breakout_norm = 0.6 if had_breakout else -0.3

        X.append([ta_norm, lgbm_norm, breakout_norm, 0, 0])  # gap/regime 없으면 0
        y.append(1 if pnl > 0 else 0)

    X = np.array(X)
    y = np.array(y)

    # 그리드 서치: 각 가중치를 0.1, 0.2, 0.3에서 탐색
    best_brier = 999
    best_weights = dict(DEFAULT_WEIGHTS)

    weight_options = [0.10, 0.20, 0.35]
    param_names = ["ta_score", "lgbm_prob", "breakout", "overnight_gap", "regime"]

    # 간소화: 주요 3개 파라미터만 그리드 서치 (ta, lgbm, breakout)
    for w_ta in weight_options:
        for w_lgbm in weight_options:
            for w_brk in weight_options:
                w_gap = 0.10
                w_regime = max(0.05, 1.0 - w_ta - w_lgbm - w_brk - w_gap)
                if w_regime > 0.4:
                    continue

                weights = [w_ta, w_lgbm, w_brk, w_gap, w_regime]

                # Brier Score 계산
                logits = X @ (np.array(weights) * 3.0) - 0.1
                probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -10, 10)))
                brier = float(np.mean((probs - y) ** 2))

                if brier < best_brier:
                    best_brier = brier
                    best_weights = {
                        "ta_score": w_ta,
                        "lgbm_prob": w_lgbm,
                        "breakout": w_brk,
                        "overnight_gap": w_gap,
                        "regime": w_regime,
                        "bias": -0.1,
                    }

    _save_weights(best_weights, {
        "brier_score": round(best_brier, 4),
        "n_samples": len(y),
        "win_rate": round(float(np.mean(y)), 3),
    })

    log.info("fusion_weights_learned",
             brier=f"{best_brier:.4f}",
             n_samples=len(y),
             weights=best_weights)

    return best_weights
