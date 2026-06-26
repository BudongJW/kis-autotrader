"""conviction_position_cap_krw 단위 테스트.

약신호 과집중 방지: 강신호만 큰 비중, 중·약신호는 상한이 작아 (호출부에서
min_position_krw 미만이면) 진입 스킵. 069500(약신호 16%) 손절 사례 회귀방지.
"""
from src.risk_manager import conviction_position_cap_krw

CFG = {"max_position_weight": 0.35, "conviction_strong_weight": 0.35,
       "conviction_mid_weight": 0.22, "conviction_weak_weight": 0.12}
EQ = 888_000
MIN = 300_000


def test_strong_signal_full_cap():
    # 돌파 + 융합 75% → 강신호, 0.35 비중 = 310,800 ≥ 최소 → 진입 가능
    cap = conviction_position_cap_krw(EQ, 0.75, True, CFG)
    assert cap == int(EQ * 0.35)
    assert cap >= MIN


def test_weak_signal_below_min_skipped():
    # 069500 케이스: 돌파X + 융합 64% → 약신호, 0.12 = 106,560 < 최소 → 스킵
    cap = conviction_position_cap_krw(EQ, 0.64, False, CFG)
    assert cap == int(EQ * 0.12)
    assert cap < MIN          # 호출부가 진입 스킵


def test_mid_signal_below_min_skipped():
    # 융합 72% 무돌파 → 중신호 0.22 = 195,360 < 최소 300k → 스킵
    cap = conviction_position_cap_krw(EQ, 0.72, False, CFG)
    assert cap == int(EQ * 0.22)
    assert cap < MIN


def test_breakout_mid_band():
    # 돌파 + 융합 64%(<0.70) → 중신호(돌파+≥0.62) 0.22
    cap = conviction_position_cap_krw(EQ, 0.64, True, CFG)
    assert cap == int(EQ * 0.22)


def test_hard_cap_not_exceeded():
    # conviction_strong_weight가 base보다 커도 base로 제한
    cfg = {"max_position_weight": 0.30, "conviction_strong_weight": 0.50}
    cap = conviction_position_cap_krw(EQ, 0.8, True, cfg)
    assert cap == int(EQ * 0.30)


def test_069500_one_share_exceeds_weak_cap():
    # 069500 1주(146,520) > 약신호 상한(106,560) → 1주도 못 사 스킵
    cap = conviction_position_cap_krw(EQ, 0.64, False, CFG)
    assert 146_520 > cap


def test_theme_boost_mid_to_strong():
    # 반도체 주도주 중신호: 부스트 없으면 mid(195k<최소→스킵), 부스트면 strong(311k≥최소→진입)
    no_boost = conviction_position_cap_krw(EQ, 0.72, False, CFG, theme_boost=False)
    boosted = conviction_position_cap_krw(EQ, 0.72, False, CFG, theme_boost=True)
    assert no_boost == int(EQ * 0.22)
    assert boosted == int(EQ * 0.35)   # mid→strong
    assert no_boost < MIN <= boosted   # 부스트로 진입 가능해짐


def test_theme_boost_weak_to_mid():
    boosted = conviction_position_cap_krw(EQ, 0.55, False, CFG, theme_boost=True)
    assert boosted == int(EQ * 0.22)   # weak→mid


def test_theme_boost_strong_unchanged_and_capped():
    # 이미 강신호면 그대로(strong), 하드상한 초과 안 함
    boosted = conviction_position_cap_krw(EQ, 0.75, True, CFG, theme_boost=True)
    assert boosted == int(EQ * 0.35)   # strong 유지, base 0.35 초과 안 함
