"""이자 엔진 (스펙 2.2): 일할계산, 20% 상한 강제, 복리 금지."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.engines import interest
from app.engines.interest import LegalRateExceeded

TEN_MILLION = Decimal("10000000")
RATE = Decimal("0.20")


def _accrue_n_days(principal: Decimal, n: int) -> list[interest.AccrualPosting]:
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    return interest.accrue_days(Decimal(0), principal, days, RATE)


# ── (a) 원금 1,000만원 30일 이자 = 164,383원 ──

def test_a_ten_million_30_days_is_164383():
    assert interest.simple_interest(TEN_MILLION, 30) == Decimal("164383")
    postings = _accrue_n_days(TEN_MILLION, 30)
    assert sum(p.daily_interest for p in postings) == Decimal("164383")


def test_daily_postings_are_whole_won_and_differ_by_at_most_one():
    postings = _accrue_n_days(TEN_MILLION, 30)
    amounts = {p.daily_interest for p in postings}
    assert amounts <= {Decimal("5479"), Decimal("5480")}  # 정밀값 5,479.45…
    assert all(a == a.to_integral_value() for a in amounts)


def test_daily_interest_formula():
    assert interest.daily_interest(TEN_MILLION, RATE) == TEN_MILLION * RATE / 365


# ── (b) 365일 적립 합계 = 정확히 원금 × 0.20 ──

def test_b_365_days_sum_is_exactly_twenty_percent():
    postings = _accrue_n_days(TEN_MILLION, 365)
    total = sum(p.daily_interest for p in postings)
    assert total == TEN_MILLION * RATE == Decimal("2000000")
    assert interest.effective_annual_rate(total, postings[-1].balance_days_after) == RATE


@pytest.mark.parametrize("principal", ["1", "999", "1234567", "33333333", "987654321"])
@pytest.mark.parametrize("days", [1, 7, 31, 90, 365, 400])
def test_daily_postings_telescope_to_simple_interest(principal, days):
    p = Decimal(principal)
    total = sum(x.daily_interest for x in _accrue_n_days(p, days))
    assert total == interest.simple_interest(p, days)
    assert total <= p * RATE * days / 365  # 절사 — 정밀값을 넘지 않음


# ── (c) 복리 금지 ──

def test_c_unpaid_interest_never_enters_the_base():
    """미수이자를 한 푼도 안 갚아도 1년 이자는 정확히 20%. 복리로 바꾸면 이 테스트가 깨진다."""
    balance_days, accrued = Decimal(0), Decimal(0)
    outstanding_principal = TEN_MILLION
    for _ in range(365):
        daily, balance_days = interest.accrue_day(balance_days, outstanding_principal, RATE)
        accrued += daily
    assert accrued == Decimal("2000000")
    assert balance_days == TEN_MILLION * 365


def test_c_compounding_would_exceed_legal_cap():
    """비교용: 원금+미수이자를 기준으로 삼으면(복리) 1년에 20%를 넘는다 → 상한 검사에 걸린다."""
    balance_days, accrued = Decimal(0), Decimal(0)
    for _ in range(365):
        daily, balance_days = interest.accrue_day(balance_days, TEN_MILLION + accrued, RATE)
        accrued += daily
    assert accrued > Decimal("2210000")  # ≈ 22.1%
    with pytest.raises(LegalRateExceeded):
        interest.assert_within_legal_cap(accrued, TEN_MILLION * 365)


def test_principal_change_splits_the_base():
    # 1,000만원 10일 + (500만원 상환 후) 500만원 20일 → 적수 2억
    postings = _accrue_n_days(TEN_MILLION, 10)
    bd = postings[-1].balance_days_after
    more = interest.accrue_days(bd, Decimal("5000000"), [date(2026, 2, 1) + timedelta(days=i) for i in range(20)])
    total = sum(p.daily_interest for p in postings + more)
    assert total == Decimal("109589")  # floor(200,000,000 × 0.2 / 365 = 109,589.04)


def test_accrue_day_rejects_fractional_or_negative_base():
    with pytest.raises(ValueError):
        interest.accrue_day(Decimal(0), Decimal("100.5"), RATE)
    with pytest.raises(ValueError):
        interest.accrue_day(Decimal(0), Decimal("-1"), RATE)


# ── 20% 상한 강제 ──

def test_rate_above_legal_max_is_rejected_everywhere():
    bad = Decimal("0.2001")
    with pytest.raises(LegalRateExceeded):
        interest.daily_interest(TEN_MILLION, bad)
    with pytest.raises(LegalRateExceeded):
        interest.simple_interest(TEN_MILLION, 30, bad)
    with pytest.raises(LegalRateExceeded):
        interest.accrue_day(Decimal(0), TEN_MILLION, bad)


def test_legal_cap_assertion():
    bd = TEN_MILLION * 30
    interest.assert_within_legal_cap(Decimal("164383"), bd)
    with pytest.raises(LegalRateExceeded):
        interest.assert_within_legal_cap(Decimal("164384"), bd)


# ── 상환 충당 ──

def test_payment_goes_to_interest_first():
    a = interest.allocate_payment(Decimal("200000"), Decimal("164383"), TEN_MILLION)
    assert (a.to_interest, a.to_principal) == (Decimal("164383"), Decimal("35617"))
    assert a.remaining_interest == 0 and a.remaining_principal == Decimal("9964383")
    assert not a.fully_repaid


def test_payment_smaller_than_interest():
    a = interest.allocate_payment(Decimal("100"), Decimal("164383"), TEN_MILLION)
    assert (a.to_interest, a.to_principal) == (Decimal("100"), Decimal("0"))


def test_full_payoff_has_no_fee():
    payoff = interest.payoff_amount(Decimal("164383"), TEN_MILLION)
    assert payoff == Decimal("10164383")  # 원금 + 이자, 수수료 0
    assert interest.allocate_payment(payoff, Decimal("164383"), TEN_MILLION).fully_repaid


@pytest.mark.parametrize("amount", ["0", "-1", "100.5", "10164384"])
def test_invalid_payments(amount):
    with pytest.raises(ValueError):
        interest.allocate_payment(Decimal(amount), Decimal("164383"), TEN_MILLION)
