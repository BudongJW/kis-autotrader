"""당일 전략 구성(build_day_plan) 테스트."""
from __future__ import annotations

from src.strategies.day_plan import build_day_plan, decide_stance


def _on(direction="neutral", nasdaq=0.0, rec="normal"):
    return {"direction": direction, "nasdaq_change": nasdaq, "recommended_action": rec}


# ── 스탠스 결정 ──────────────────────────────────────────────

def test_us_crash_is_active_inverse():
    """美 폭락(-4%↓)은 현금이 아니라 능동 인버스(DEFENSIVE) — 하락에서 수익 추구."""
    s = decide_stance("CAUTION", 0.27, _on("bearish", -5.11, "reduce_size"), "high", 82)
    assert s == "DEFENSIVE"


def test_blind_is_risk_off():
    """시장을 못 읽으면(blind) 유일하게 RISK_OFF(관망) — '못 보면 베팅 안 함'."""
    assert decide_stance("BULL", 0.7, _on("bullish", 1.0), "normal", 30, blind=True) == "RISK_OFF"
    assert decide_stance("CRISIS", 0.2, _on("bearish", -6), "high", 90, blind=True) == "RISK_OFF"


def test_crisis_is_active_inverse():
    """위기 레짐도 (데이터가 보이면) 능동 인버스 — 현금 관망이 아니라."""
    assert decide_stance("CRISIS", 0.2, _on("bearish", -6), "high", 90) == "DEFENSIVE"


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

def test_blind_risk_off_blocks_everything():
    """blind → RISK_OFF → 모든 신규매수 차단(현금 관망)."""
    p = build_day_plan("CRISIS", 0.2, _on("bearish", -6), "high", 90, blind=True)
    assert p["stance"] == "RISK_OFF"
    assert p["budget_pct"] == 0.0 and p["max_new_positions"] == 0
    assert not p["allow_long"] and not p["allow_inverse"] and not p["allow_leverage"]


def test_crisis_with_data_allows_inverse():
    """위기라도 데이터가 보이면 DEFENSIVE → 인버스 허용(롱·레버리지는 차단)."""
    p = build_day_plan("CRISIS", 0.2, _on("bearish", -6), "high", 90)
    assert p["stance"] == "DEFENSIVE"
    assert p["allow_inverse"] and not p["allow_long"] and not p["allow_leverage"]


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


# ── 매매 연동 게이트 (escalate-only) ─────────────────────────

def test_gate_blocks_buy_on_risk_off():
    """RISK_OFF 플랜(=blind 관망)은 신규 매수를 차단한다."""
    from src.bot.single_run import day_plan_blocks_buy
    p = build_day_plan("CRISIS", 0.2, _on("bearish", -6), "high", 90, blind=True)
    assert p["stance"] == "RISK_OFF"
    assert day_plan_blocks_buy(p) is True


def test_gate_blocks_buy_on_zero_budget():
    """예산 0%면(스탠스 무관) 매수 차단."""
    from src.bot.single_run import day_plan_blocks_buy
    assert day_plan_blocks_buy({"stance": "NEUTRAL", "budget_pct": 0.0}) is True


def test_gate_allows_buy_on_defensive_and_above():
    """방어 이상(예산>0) 스탠스는 매수 허용 — 사이즈 캡만 적용(escalate-only)."""
    from src.bot.single_run import day_plan_blocks_buy
    for regime, conf, on, vol, vp in [
        ("BEAR", 0.5, _on("bearish", -1), "high", 80),     # DEFENSIVE
        ("CAUTION", 0.5, _on(), "normal", 50),              # CAUTIOUS
        ("BULL", 0.75, _on("bullish", 1.0), "normal", 35),  # RISK_ON
    ]:
        p = build_day_plan(regime, conf, on, vol, vp)
        assert day_plan_blocks_buy(p) is False, p["stance"]


def test_gate_handles_none_plan():
    """플랜 산출 실패(None)면 차단하지 않는다(기존 동작 보존)."""
    from src.bot.single_run import day_plan_blocks_buy
    assert day_plan_blocks_buy(None) is False


def test_gate_size_mult_is_escalate_only():
    """모든 스탠스의 size_mult ≤ 1.0 — 게이트는 축소만(증폭 불가)."""
    from src.strategies.day_plan import _PRESETS
    assert all(0.0 <= p["size_mult"] <= 1.0 for p in _PRESETS.values())
