"""eod_us_hold_decision 단위 테스트.

매일 전량청산 churn(왕복 0.5% 수수료)을 막기 위해, 마감 시 수익+추세 winner만
오버나이트 보유하고 손실·약세는 청산하는지 검증.
"""
from src.bot.us_session import eod_us_hold_decision

_CFG = {"eod_hold_winners": True, "strategy": {"trailing_activate_pct": 0.02}}


def test_keep_winner_above_activate():
    # +3% (≥ +2% activate) → 추세 winner 보유
    keep, why = eod_us_hold_decision(100.0, 103.0, _CFG)
    assert keep is True
    assert "보유" in why


def test_close_flat_below_activate():
    # +0.5% (< +2%) → 청산(churn 회피)
    keep, why = eod_us_hold_decision(100.0, 100.5, _CFG)
    assert keep is False
    assert "청산" in why


def test_close_loss():
    keep, why = eod_us_hold_decision(100.0, 98.0, _CFG)
    assert keep is False


def test_disabled_always_closes():
    cfg = {"eod_hold_winners": False, "strategy": {"trailing_activate_pct": 0.02}}
    keep, why = eod_us_hold_decision(100.0, 110.0, cfg)  # +10% winner라도
    assert keep is False
    assert "전량청산" in why


def test_invalid_price_closes():
    keep, why = eod_us_hold_decision(0.0, 103.0, _CFG)
    assert keep is False


def test_boundary_exactly_at_activate_keeps():
    # 정확히 +2% → 보유(≥)
    keep, why = eod_us_hold_decision(100.0, 102.0, _CFG)
    assert keep is True


def test_default_activate_when_missing():
    # strategy 없으면 기본 2% 적용
    keep, _ = eod_us_hold_decision(100.0, 103.0, {"eod_hold_winners": True})
    assert keep is True
