"""당일 전략 구성(build_day_plan) 테스트."""
from __future__ import annotations

from src.strategies.day_plan import build_day_plan, decide_stance


def _on(direction="neutral", nasdaq=0.0, rec="normal"):
    return {"direction": direction, "nasdaq_change": nasdaq, "recommended_action": rec}


# ── 스탠스 결정 ──────────────────────────────────────────────

def test_us_crash_is_risk_off():
    """美 폭락(-4%↓)은 레짐 무관 RISK_OFF (검은 월요일)."""
    s = decide_stance("CAUTION", 0.27, _on("bearish", -5.11, "reduce_size"), "high", 82)
    assert s == "RISK_OFF"


def test_bear_regime_defensive():
    s = decide_stance("BEAR", 0.5, _on("neutral", -1), "normal", 50)
    assert s == "DEFENSIVE"


def test_high_vol_bearish_defensive():
    s = decide_stance("CAUTION", 0.5, _on("bearish", -2), "high", 80)
    assert s == "DEFENSIVE"


def test_caution_or_low_conf_cautious():
    assert decide_stance("CAUTION", 0.5, _on(), "normal", 50) == "CAUTIOUS"
    assert decide_stance("BULL", 0.3, _on(), "normal", 50) == "CAUTIOUS"  # 낮은 신뢰도
    assert decide_stance("BULL", 0.7, _on(rec="reduce_size"), "normal", 50) == "CAUTIOUS"


def test_strong_bull_risk_on():
    s = decide_stance("BULL", 0.7, _on("bullish", 1.2), "normal", 40)
    assert s == "RISK_ON"


def test_bull_but_high_vol_not_risk_on():
    s = decide_stance("BULL", 0.7, _on("neutral", 0.5), "high", 80)
    assert s == "NEUTRAL"


# ── 플랜 프리셋 ──────────────────────────────────────────────

def test_risk_off_blocks_everything():
    p = build_day_plan("CRISIS", 0.2, _on("bearish", -6), "high", 90)
    assert p["stance"] == "RISK_OFF"
    assert p["budget_pct"] == 0.0 and p["max_new_positions"] == 0
    assert not p["allow_long"] and not p["allow_inverse"] and not p["allow_leverage"]


def test_defensive_allows_inverse_not_long():
    p = build_day_plan("BEAR", 0.5, _on("bearish", -1), "high", 80)
    assert p["stance"] == "DEFENSIVE"
    assert p["allow_inverse"] and not p["allow_long"] and not p["allow_leverage"]


def test_risk_on_allows_leverage():
    p = build_day_plan("BULL", 0.75, _on("bullish", 1.0), "normal", 35, base_k=0.5)
    assert p["stance"] == "RISK_ON"
    assert p["allow_leverage"] and p["allow_long"]
    assert p["budget_pct"] == 1.0 and p["max_new_positions"] == 3


def test_k_raised_in_high_vol():
    """고변동 방어 스탠스는 K를 높여 돌파 문턱↑."""
    p = build_day_plan("BEAR", 0.4, _on("bearish", -2), "high", 85, base_k=0.5)
    assert p["k_effective"] > 0.5


def test_briefing_present():
    p = build_day_plan("CAUTION", 0.27, _on("bearish", -5.11, "reduce_size"), "high", 82)
    assert "스탠스" in p["briefing"] and p["stance"] in p["briefing"]
