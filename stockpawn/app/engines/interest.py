"""이자 엔진 (스펙 2.2) — 순수 함수.

**적수(積數) 방식**: 적수 = Σ(일별 원금잔액), 누적이자 = floor(적수 × 연이율 / 365).
매일 적립하는 이자는 누적이자의 차분이라, 매일 원 단위 정수로 적립되면서도
  - 1,000만 원 × 30일  → floor(164,383.56) = 164,383원
  - 1,000만 원 × 365일 → 정확히 2,000,000원
이 보장된다. (매일 반올림/절사하면 오차가 쌓여 둘 다 틀린다.)

규칙
  - **단리 전용**: 적수에는 원금 잔액만 들어간다. 미수이자는 절대 이자 계산 기준이 아니다.
    복리는 실효 이자율이 20%를 넘을 수 있어 금지.
  - 연체 가산이자 = 0, 중도상환수수료 = 0, 선이자 공제 금지 (config 참고).
  - 일수 계산은 한편넣기: 대출일 포함, 상환일 제외. 상환일 당일분 이자는 없다.
  - 누적이자는 언제나 floor(적수 × 법정최고금리 / 365) 이하임을 검사한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app import config
from app.money import ZERO, floor_won, is_whole_won


class LegalRateExceeded(ValueError):
    """법정 최고금리 초과 — 어떤 경로로도 발생하면 안 된다."""


def check_rate(annual_rate: Decimal) -> None:
    if annual_rate <= 0:
        raise ValueError(f"연이율은 양수여야 합니다: {annual_rate}")
    if annual_rate > config.LEGAL_MAX_RATE:
        raise LegalRateExceeded(f"연이율 {annual_rate} > 법정 최고 {config.LEGAL_MAX_RATE}")


def daily_interest(principal: Decimal, annual_rate: Decimal = config.ANNUAL_RATE) -> Decimal:
    """1일 이자 정밀값(원 미만 포함) = 원금 × 연이율 / 365. 표시·설명용 — 적립은 accrue_day()."""
    check_rate(annual_rate)
    return principal * annual_rate / config.DAY_COUNT


def interest_for_balance_days(balance_days: Decimal, annual_rate: Decimal = config.ANNUAL_RATE) -> Decimal:
    """적수 → 누적이자 (원 미만 절사). 적수 × 연이율은 유한소수라 정확하고, 나눗셈은 한 번뿐이다."""
    check_rate(annual_rate)
    if balance_days < 0:
        raise ValueError("적수는 음수일 수 없습니다")
    return (balance_days * annual_rate) // config.DAY_COUNT


def simple_interest(principal: Decimal, days: int, annual_rate: Decimal = config.ANNUAL_RATE) -> Decimal:
    """원금 고정 단리 이자 = floor(원금 × 연이율 × 일수 / 365)."""
    return interest_for_balance_days(principal * days, annual_rate)


def legal_max_interest(balance_days: Decimal) -> Decimal:
    """해당 적수에 대해 법적으로 받을 수 있는 최대 이자."""
    return interest_for_balance_days(balance_days, config.LEGAL_MAX_RATE)


def assert_within_legal_cap(total_interest: Decimal, balance_days: Decimal) -> None:
    """누적 수취(예정) 이자가 법정 상한 이내인지. 위반 시 LegalRateExceeded."""
    cap = legal_max_interest(balance_days)
    if total_interest > cap:
        raise LegalRateExceeded(f"누적이자 {total_interest}원 > 법정 상한 {cap}원 (적수 {balance_days})")


def effective_annual_rate(total_interest: Decimal, balance_days: Decimal) -> Decimal:
    """실효 연이율 = 이자 × 365 / 적수."""
    if balance_days <= 0:
        return ZERO
    return total_interest * config.DAY_COUNT / balance_days


@dataclass(frozen=True)
class AccrualPosting:
    date: date
    principal_base: Decimal  # 그날 이자 계산 기준 원금 잔액 (미수이자 미포함)
    daily_interest: Decimal  # 원 단위 정수
    balance_days_after: Decimal  # 적립 후 누적 적수


def accrue_day(
    balance_days: Decimal, principal_base: Decimal, annual_rate: Decimal = config.ANNUAL_RATE
) -> tuple[Decimal, Decimal]:
    """하루치 적립. (그날 이자, 갱신된 적수)를 돌려준다.

    principal_base는 **원금 잔액**이어야 한다. 원금 + 미수이자를 넣으면 복리가 되며,
    그 경우 누적이자가 법정 상한을 넘게 되어 상위 검사(assert_within_legal_cap)에 걸린다.
    """
    if principal_base < 0 or not is_whole_won(principal_base):
        raise ValueError(f"원금 잔액은 0 이상의 원 단위 정수여야 합니다: {principal_base}")
    before = interest_for_balance_days(balance_days, annual_rate)
    new_balance_days = balance_days + principal_base
    after = interest_for_balance_days(new_balance_days, annual_rate)
    return after - before, new_balance_days


def accrue_days(
    balance_days: Decimal,
    principal_base: Decimal,
    days: Iterable[date],
    annual_rate: Decimal = config.ANNUAL_RATE,
) -> list[AccrualPosting]:
    """원금이 일정한 구간을 여러 날 적립."""
    postings = []
    for d in days:
        interest, balance_days = accrue_day(balance_days, principal_base, annual_rate)
        postings.append(AccrualPosting(d, principal_base, interest, balance_days))
    return postings


@dataclass(frozen=True)
class PaymentAllocation:
    to_interest: Decimal
    to_principal: Decimal
    remaining_interest: Decimal
    remaining_principal: Decimal

    @property
    def fully_repaid(self) -> bool:
        return self.remaining_interest == 0 and self.remaining_principal == 0


def allocate_payment(amount: Decimal, accrued_interest: Decimal, outstanding_principal: Decimal) -> PaymentAllocation:
    """상환금 충당: 이자 → 원금 순서 (민법 제479조). 수수료 항목은 없다(0).

    잔액을 넘는 상환은 ValueError — 초과분 처리(잉여금 반환)는 호출측 책임.
    """
    if amount <= 0 or not is_whole_won(amount):
        raise ValueError(f"상환액은 양의 원 단위 정수여야 합니다: {amount}")
    total = accrued_interest + outstanding_principal
    if amount > total:
        raise ValueError(f"상환액 {amount}원이 상환할 잔액 {total}원을 초과합니다")
    to_interest = min(amount, accrued_interest)
    to_principal = amount - to_interest
    return PaymentAllocation(
        to_interest=to_interest,
        to_principal=to_principal,
        remaining_interest=accrued_interest - to_interest,
        remaining_principal=outstanding_principal - to_principal,
    )


def payoff_amount(accrued_interest: Decimal, outstanding_principal: Decimal) -> Decimal:
    """완제 금액 = 원금 잔액 + 미수이자. 수수료 없음."""
    return floor_won(accrued_interest + outstanding_principal)
