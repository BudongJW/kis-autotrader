"""일일 익절 목표(daily_target_hit) 단위 테스트.

'차면 그날 리스크 그만' 규칙의 순수 판정부. 당일 실현+미실현이 목표 비율 도달 시 True.
"""
from src.risk_manager import daily_target_hit


def test_hit_when_realized_meets_target():
    # 자본 900,000의 1% = 9,000. 실현 10,000 → 도달
    hit, pct = daily_target_hit(10000, 0, 900000, 0.01)
    assert hit and round(pct, 4) == round(10000/900000, 4)


def test_realized_plus_unrealized_counts():
    # 실현 5,000 + 미실현 5,000 = 10,000 >= 9,000 → 도달
    hit, _ = daily_target_hit(5000, 5000, 900000, 0.01)
    assert hit


def test_below_target_not_hit():
    hit, _ = daily_target_hit(3000, 2000, 900000, 0.01)  # 5,000 < 9,000
    assert not hit


def test_unrealized_loss_offsets():
    # 실현 12,000 - 미실현손실 5,000 = 7,000 < 9,000 → 미도달(되돌림 반영)
    hit, _ = daily_target_hit(12000, -5000, 900000, 0.01)
    assert not hit


def test_disabled_when_target_zero():
    hit, pct = daily_target_hit(50000, 0, 900000, 0.0)
    assert not hit and pct == 0.0


def test_disabled_when_equity_zero():
    hit, pct = daily_target_hit(50000, 0, 0, 0.01)
    assert not hit and pct == 0.0
