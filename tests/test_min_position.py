"""apply_min_position 단위 테스트.

단타: 소액 1주 회피 → 최소 투입금액 floor. 단 현금·비중상한으로 캡, 절대 줄이지 않음.
"""
from src.risk_manager import apply_min_position


def test_bumps_small_to_min():
    # 1주(13,040) → 최소 30만원 = 22주 (13040*22=286,880 ≤ 300k//13040=23주? 300000//13040=23)
    q = apply_min_position(1, 13040, avail_cash=900_000, min_krw=300_000)
    assert q == 300_000 // 13040  # 23주
    assert q > 1


def test_does_not_shrink_when_already_big():
    # 이미 30주(>30만)면 그대로
    q = apply_min_position(30, 13040, avail_cash=900_000, min_krw=300_000)
    assert q == 30


def test_capped_by_cash():
    # 현금 50,000뿐이면 95%=47,500 // 13040 = 3주로 캡(30만 못 채움)
    q = apply_min_position(1, 13040, avail_cash=50_000, min_krw=300_000)
    assert q == int(50_000 * 0.95 // 13040)


def test_capped_by_weight():
    # 비중상한 0.45 * equity 400,000 = 180,000 // 13040 = 13주로 캡
    q = apply_min_position(1, 13040, avail_cash=900_000, min_krw=300_000,
                           max_weight=0.45, equity=400_000)
    assert q == int(180_000 // 13040)


def test_disabled_when_min_zero():
    q = apply_min_position(1, 13040, avail_cash=900_000, min_krw=0)
    assert q == 1


def test_zero_price_safe():
    q = apply_min_position(1, 0, avail_cash=900_000, min_krw=300_000)
    assert q == 1
