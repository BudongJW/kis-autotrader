"""position_cap_weight 단위 테스트 — 성과 검증 단계별 포지션 상한 램프.

검증 전 base, 거래수·승률 충족 시 단계 상향, 절대 hard_max(전액 금지) 초과 안 함.
"""
from src.risk_manager import position_cap_weight

CFG = {"sizing_ramp": {
    "enabled": True, "base_weight": 0.35, "hard_max": 0.65,
    "tiers": [
        {"trades": 20, "win_rate": 0.50, "weight": 0.45},
        {"trades": 40, "win_rate": 0.55, "weight": 0.55},
        {"trades": 60, "win_rate": 0.58, "weight": 0.65},
    ]}}


def test_unproven_stays_base():
    # 거래 8건 (검증 전) → base 0.35 유지 (현재 상태)
    assert position_cap_weight(8, 0.5, CFG) == 0.35


def test_tier1_after_20_trades():
    assert position_cap_weight(20, 0.50, CFG) == 0.45


def test_tier2_needs_both_trades_and_winrate():
    # 거래 40이지만 승률 0.52 < 0.55 → tier2 미달, tier1(0.45) 적용
    assert position_cap_weight(40, 0.52, CFG) == 0.45
    # 승률도 충족 → tier2 0.55
    assert position_cap_weight(40, 0.55, CFG) == 0.55


def test_tier3_top():
    assert position_cap_weight(60, 0.58, CFG) == 0.65


def test_never_exceeds_hard_max():
    # 초강 성과여도 hard_max(0.65) 초과 금지 (전액 방지)
    cfg = {"sizing_ramp": {"enabled": True, "base_weight": 0.35, "hard_max": 0.65,
                           "tiers": [{"trades": 1, "win_rate": 0.0, "weight": 0.99}]}}
    assert position_cap_weight(100, 0.9, cfg) == 0.65


def test_disabled_returns_base():
    cfg = {"sizing_ramp": {"enabled": False, "base_weight": 0.35, "hard_max": 0.65,
                           "tiers": [{"trades": 1, "win_rate": 0.0, "weight": 0.6}]}}
    assert position_cap_weight(100, 0.9, cfg) == 0.35


def test_low_winrate_no_promotion():
    # 거래 많아도 승률 낮으면 승격 안 됨 (검증 실패 → 안 키움)
    assert position_cap_weight(100, 0.40, CFG) == 0.35
