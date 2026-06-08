"""당일 전략 구성 — 장전 신호를 종합해 '오늘의 스탠스'를 산출.

매일 장 전(또는 장 시작 시) 시장 상태를 한 덩어리로 평가해, 그날의 매매 자세를
명시적으로 결정한다. 흩어진 신호(레짐·신뢰도·美 오버나이트·변동성·급락)를 모아
RISK_OFF ~ RISK_ON 5단계 스탠스 + 예산·포지션·허용전략·브리핑으로 변환.

설계 원칙: 방어 우선(하나라도 위험 신호면 보수적으로). 스탠스는 표시·로깅용
1차 산출이며, 실제 매매 연동은 검증 후 단계적으로(escalate-only).
"""
from __future__ import annotations

# 스탠스별 프리셋 (보수 → 공격)
_PRESETS = {
    "RISK_OFF":  {"budget_pct": 0.0,  "max_new_positions": 0, "allow_long": False,
                  "allow_inverse": False, "allow_leverage": False, "k_adjust": 0.10, "size_mult": 0.0},
    "DEFENSIVE": {"budget_pct": 0.25, "max_new_positions": 1, "allow_long": False,
                  "allow_inverse": True,  "allow_leverage": False, "k_adjust": 0.10, "size_mult": 0.4},
    "CAUTIOUS":  {"budget_pct": 0.40, "max_new_positions": 2, "allow_long": True,
                  "allow_inverse": True,  "allow_leverage": False, "k_adjust": 0.05, "size_mult": 0.6},
    "NEUTRAL":   {"budget_pct": 0.70, "max_new_positions": 2, "allow_long": True,
                  "allow_inverse": False, "allow_leverage": False, "k_adjust": 0.0,  "size_mult": 0.85},
    "RISK_ON":   {"budget_pct": 1.0,  "max_new_positions": 3, "allow_long": True,
                  "allow_inverse": False, "allow_leverage": True,  "k_adjust": 0.0,  "size_mult": 1.0},
}

_STANCE_KR = {
    "RISK_OFF": "위험회피(현금)", "DEFENSIVE": "방어", "CAUTIOUS": "신중",
    "NEUTRAL": "중립", "RISK_ON": "공격",
}


def decide_stance(regime: str, confidence: float, overnight: dict,
                  volatility: str, vol_percentile: float,
                  rapid_level: str = "NONE", blind: bool = False) -> str:
    """장전 신호 → 당일 스탠스. 철학: **보이면 추세를 타고, 못 보면 관망.**

    상승 추세면 롱(고확신 시 레버리지·dry-run), 하락 추세면 능동적으로 인버스로
    수익을 노린다. 시장을 못 읽는 경우(blind)에만 현금 관망(RISK_OFF)한다.
    """
    og = overnight or {}
    direction = og.get("direction", "neutral")
    nasdaq = og.get("nasdaq_change") or 0
    rec = og.get("recommended_action", "normal")
    conf = confidence if confidence is not None else 0.5
    high_vol = (volatility == "high") or ((vol_percentile or 0) >= 75)

    # 0) 블라인드: 시장 데이터를 못 읽으면 관망(유일한 RISK_OFF 트리거).
    #    "못 보면 베팅 안 한다" — 폭락 때 API 과부하로 데이터가 빠지는 상황 방어.
    if blind:
        return "RISK_OFF"
    # 1) 하락 추세 → 능동 인버스(DEFENSIVE): 위기/하락장/지속급락/美 폭락(-4%↓)/
    #    (고변동+약세). 현금이 아니라 인버스로 하락에서 수익을 노린다.
    if (regime in ("CRISIS", "BEAR") or rapid_level in ("CRISIS", "BEAR")
            or nasdaq <= -4 or (high_vol and direction == "bearish")):
        return "DEFENSIVE"
    # 2) 신중: 경고 레짐 / 낮은 신뢰도 / reduce_size / 단발 급락경고
    if regime == "CAUTION" or conf < 0.4 or rec == "reduce_size" or rapid_level == "CAUTION":
        return "CAUTIOUS"
    # 3) 공격: 강세 + 높은 신뢰도 + 약세 아님 + 정상 변동성 (롱·레버리지 허용)
    if regime == "BULL" and conf >= 0.6 and direction != "bearish" and not high_vol:
        return "RISK_ON"
    # 4) 그 외 중립
    return "NEUTRAL"


def build_day_plan(regime: str, confidence: float, overnight: dict,
                   volatility: str, vol_percentile: float,
                   rapid_level: str = "NONE", base_k: float = 0.5,
                   force_stance: str | None = None, blind: bool = False) -> dict:
    """장전 신호 종합 → 당일 전략 플랜(스탠스·예산·허용전략·브리핑).

    기본은 **자율** — decide_stance가 신호를 보고 스탠스를 정한다.
    force_stance: 비상 수동 브레이크(운영자 개입). 평상시엔 None(자율).
    단 escalate-only 원칙 유지 — 강제 스탠스도 프리셋의 보수적 캡을 그대로 따른다.
    """
    auto_stance = decide_stance(regime, confidence, overnight, volatility,
                                vol_percentile, rapid_level, blind=blind)
    if force_stance and force_stance in _PRESETS:
        stance = force_stance
        forced = (force_stance != auto_stance)
    else:
        stance = auto_stance
        forced = False
    p = dict(_PRESETS[stance])
    og = overnight or {}
    nasdaq = og.get("nasdaq_change")
    direction = og.get("direction", "neutral")
    conf = confidence if confidence is not None else 0.5
    high_vol = (volatility == "high") or ((vol_percentile or 0) >= 75)

    # 허용 전략 요약
    allow = []
    if p["allow_long"]:
        allow.append("롱")
    if p["allow_inverse"]:
        allow.append("인버스")
    if p["allow_leverage"]:
        allow.append("레버리지")
    if not allow:
        allow.append("현금/방어자산만")

    # 한글 브리핑
    _tag = "수동강제 " if forced else ""
    parts = [f"오늘 스탠스 {_tag}{_STANCE_KR[stance]}({stance})"]
    parts.append(f"레짐 {regime}·신뢰도 {round(conf * 100)}%")
    if nasdaq is not None:
        parts.append(f"美 오버나이트 {direction}({nasdaq:+.1f}%)")
    if high_vol:
        parts.append(f"고변동({vol_percentile}%ile)")
    parts.append(f"예산 {round(p['budget_pct'] * 100)}%·최대 {p['max_new_positions']}종목·허용 {'/'.join(allow)}")
    briefing = " · ".join(parts)

    return {
        "stance": stance,
        "forced": forced,
        "auto_stance": auto_stance,
        "stance_kr": _STANCE_KR[stance],
        "budget_pct": p["budget_pct"],
        "max_new_positions": p["max_new_positions"],
        "allow_long": p["allow_long"],
        "allow_inverse": p["allow_inverse"],
        "allow_leverage": p["allow_leverage"],
        "size_mult": p["size_mult"],
        "k_effective": round(base_k + p["k_adjust"], 3),
        "allow_summary": "/".join(allow),
        "briefing": briefing,
        "signals": {
            "regime": regime, "confidence": conf, "direction": direction,
            "nasdaq_change": nasdaq, "high_vol": high_vol,
            "vol_percentile": vol_percentile, "rapid_level": rapid_level,
        },
    }
