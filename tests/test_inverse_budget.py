"""size_inverse_budget 단위 테스트 — 인버스가 throttle로 먼지사이즈가 돼도
신호 확인 시 의미있는 사이즈로 진입하는지(역베팅 상한 포함) 검증.

회귀 방지: 2026-06-30 CAUTION에서 114800 인버스 TA 29.9로 신호는 떴으나
inv_budget(=bear_budget×0.15)이 ~8천원으로 10000 게이트에 걸려 스킵된 버그.
"""
from src.risk_manager import size_inverse_budget


def test_tiny_budget_floored_to_min():
    # 원시 8,000원(throttle 결과) → floor 250,000으로 끌어올림
    out = size_inverse_budget(8000, avail_cash=577424, min_krw=250000, max_krw=350000)
    assert out == 250000


def test_capped_at_max_for_counter_trend():
    # 큰 원시예산도 역베팅 상한(350,000)으로 캡
    out = size_inverse_budget(900000, avail_cash=2000000, min_krw=250000, max_krw=350000)
    assert out == 350000


def test_within_band_passthrough():
    out = size_inverse_budget(300000, avail_cash=2000000, min_krw=250000, max_krw=350000)
    assert out == 300000


def test_never_exceeds_available_cash():
    # 현금이 floor보다 적으면 가용현금 95%로 내려감 (과주문 방지)
    out = size_inverse_budget(8000, avail_cash=100000, min_krw=250000, max_krw=350000)
    assert out == 95000


def test_clears_10000_gate():
    # floor 적용 후엔 항상 10000 게이트 통과 (스킵 버그 회귀 방지)
    out = size_inverse_budget(3420, avail_cash=577424, min_krw=250000, max_krw=350000)
    assert out >= 10000


def test_floor_disabled_when_min_zero():
    # min_krw=0이면 floor 비활성 — 원시예산 유지(상한·현금캡만)
    out = size_inverse_budget(8000, avail_cash=577424, min_krw=0, max_krw=350000)
    assert out == 8000


def test_zero_cash_returns_zero():
    out = size_inverse_budget(8000, avail_cash=0, min_krw=250000, max_krw=350000)
    assert out == 0
