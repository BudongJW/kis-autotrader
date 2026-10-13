"""금액 헬퍼. 모든 금액은 Decimal, 원 단위 정수로 확정할 땐 절사(대주에게 유리하지 않은 방향)."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


def D(value: int | str | Decimal) -> Decimal:
    """int/str/Decimal → Decimal. float는 거부 (이진 부동소수 오차 유입 차단)."""
    if isinstance(value, float):
        raise TypeError("금액 계산에 float 금지 — str 또는 Decimal을 쓰세요")
    return value if isinstance(value, Decimal) else Decimal(value)


def floor_won(x: Decimal) -> Decimal:
    """원 미만 절사."""
    return x.to_integral_value(rounding=ROUND_FLOOR)


def ceil_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_CEILING))


def is_whole_won(x: Decimal) -> bool:
    return x == x.to_integral_value()
