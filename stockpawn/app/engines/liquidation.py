"""처분(반대매매) 엔진 (스펙 2.4) — 순수 함수.

수량 규칙
  - 원리금 회수 수량(PAYOFF) = ceil((원금 + 미수이자 + 예상수수료) / 예상체결가), 보유수량 상한
      예상체결가 F = 전일 종가 × 0.97, 예상수수료 = q × F × c 이므로
      q·F ≥ D + q·F·c  ⇔  q ≥ D / (F·(1 − c))  — 같은 식을 닫힌 형태로 푼다.
  - 부분 처분(PARTIAL) 우선: 담보비율이 150%로 회복되는 최소 수량만.
      q주 매도 후 V' = V − q·P, D' = D − q·F·(1 − c)
      V'/D' ≥ T  ⇔  q ≥ (T·D − V) / (T·F·(1 − c) − P)
  - 전량 처분(FULL)은 ratio < 100%일 때만.

모드 선택
  ratio < 100%                                 → FULL
  만기 경과·담보 부적격(tier X)·운영자 수동      → PAYOFF (채무 자체를 정리해야 하는 사유)
  담보비율 < 120%·마진콜 유예 만료              → PARTIAL

여러 종목이 담보면 위험한 담보부터 판다: tier X → C → B → A, 같은 등급은 헤어컷 큰 순.
거래정지 종목은 주문이 체결될 수 없으므로 계획에서 제외(시가 합계에는 포함).

정산: 순매도대금은 이자 → 원금 순으로 충당, 남으면 잉여금(차주 반환).
담보가 남지 않았는데 채무가 남으면 부족분은 미수채권으로 기록(서비스 계층).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app import config
from app.engines.risk import LiquidationReason, collateral_ratio, display_ratio
from app.money import ONE, ZERO, ceil_int, floor_won


class LiquidationMode(StrEnum):
    PARTIAL = "PARTIAL"  # 담보비율 150% 회복분만
    PAYOFF = "PAYOFF"  # 원리금 전액 회수분
    FULL = "FULL"  # 담보 전량


@dataclass(frozen=True)
class Position:
    code: str
    qty: int  # 처분 가능(질권 유지 중) 수량
    ref_price: Decimal  # 기준가 = 전일 종가
    tier: str = "C"
    haircut: Decimal = ZERO
    tradable: bool = True  # 거래정지면 False

    @property
    def market_value(self) -> Decimal:
        return self.ref_price * self.qty


@dataclass(frozen=True)
class PlannedOrder:
    code: str
    qty: int
    expected_price: Decimal
    expected_net: Decimal  # qty × 예상체결가 × (1 − 매도비용률)


@dataclass(frozen=True)
class LiquidationPlan:
    mode: LiquidationMode
    orders: tuple[PlannedOrder, ...]
    debt: Decimal
    market_value: Decimal
    ratio_before: Decimal | None
    expected_proceeds: Decimal
    expected_ratio_after: Decimal | None  # 채무가 모두 정리될 것으로 예상되면 None
    notes: tuple[str, ...] = ()

    @property
    def total_qty(self) -> int:
        return sum(o.qty for o in self.orders)


def expected_fill_price(ref_price: Decimal) -> Decimal:
    return ref_price * config.EXPECTED_FILL_DISCOUNT


def _net_per_share(ref_price: Decimal, cost_rate: Decimal) -> Decimal:
    return expected_fill_price(ref_price) * (ONE - cost_rate)


def payoff_quantity(
    debt: Decimal, ref_price: Decimal, holding: int, cost_rate: Decimal = config.SELL_COST_RATE
) -> int:
    """원리금 + 예상수수료를 회수하는 최소 수량 (보유수량 상한)."""
    if debt <= 0 or holding <= 0:
        return 0
    return min(ceil_int(debt / _net_per_share(ref_price, cost_rate)), holding)


def partial_quantity(
    market_value: Decimal,
    debt: Decimal,
    ref_price: Decimal,
    holding: int,
    target: Decimal = config.PARTIAL_LIQUIDATION_TARGET_RATIO,
    cost_rate: Decimal = config.SELL_COST_RATE,
) -> int:
    """이 종목을 팔아 담보비율을 target으로 회복시키는 최소 수량 (보유수량 상한).

    현재 비율이 1/(0.97·(1−c)) ≈ 103% 이하면 매도할수록 비율이 오히려 떨어지므로
    식의 해가 보유수량을 넘고 → 보유수량 전부가 된다.
    """
    if debt <= 0 or holding <= 0 or market_value >= target * debt:
        return 0
    denom = target * _net_per_share(ref_price, cost_rate) - ref_price
    if denom <= 0:  # 설정상 불가능하지만(1.5×0.97×0.9975 > 1) 방어: 매도로 회복 불가
        return holding
    return min(ceil_int((target * debt - market_value) / denom), holding)


def choose_mode(reasons: Iterable[LiquidationReason], ratio: Decimal | None) -> LiquidationMode:
    if ratio is not None and ratio < config.FULL_LIQUIDATION_RATIO:
        return LiquidationMode.FULL
    payoff_reasons = {
        LiquidationReason.MATURITY_EXPIRED,
        LiquidationReason.INELIGIBLE_COLLATERAL,
        LiquidationReason.MANUAL,
    }
    if payoff_reasons.intersection(reasons):
        return LiquidationMode.PAYOFF
    return LiquidationMode.PARTIAL


def _priority(p: Position) -> tuple:
    return (-config.TIER_ORDER.index(p.tier), -p.haircut, p.code)


def plan_liquidation(
    positions: Sequence[Position],
    debt: Decimal,
    mode: LiquidationMode,
    target: Decimal = config.PARTIAL_LIQUIDATION_TARGET_RATIO,
    cost_rate: Decimal = config.SELL_COST_RATE,
) -> LiquidationPlan:
    market_value = sum((p.market_value for p in positions), ZERO)
    ratio_before = collateral_ratio(market_value, debt)
    sellable = sorted((p for p in positions if p.tradable and p.qty > 0), key=_priority)
    notes = [f"거래정지로 처분 제외: {p.code}" for p in positions if not p.tradable and p.qty > 0]

    quantities: list[tuple[Position, int]] = []
    v, d = market_value, debt
    for p in sellable:
        if mode == LiquidationMode.FULL:
            q = p.qty
        elif d <= 0:
            break
        elif mode == LiquidationMode.PAYOFF:
            q = payoff_quantity(d, p.ref_price, p.qty, cost_rate)
        else:
            q = min(
                partial_quantity(v, d, p.ref_price, p.qty, target, cost_rate),
                payoff_quantity(d, p.ref_price, p.qty, cost_rate),
            )
            if q == 0:
                break  # 이미 목표 비율 회복
        if q > 0:
            quantities.append((p, q))
            v -= p.ref_price * q
            d -= _net_per_share(p.ref_price, cost_rate) * q

    orders = tuple(
        PlannedOrder(
            code=p.code,
            qty=q,
            expected_price=expected_fill_price(p.ref_price),
            expected_net=floor_won(_net_per_share(p.ref_price, cost_rate) * q),
        )
        for p, q in quantities
    )
    proceeds = sum((o.expected_net for o in orders), ZERO)
    remaining_debt = debt - proceeds
    ratio_after = display_ratio(collateral_ratio(v, remaining_debt)) if remaining_debt > 0 else None
    if remaining_debt > 0 and v <= 0:
        notes.append(f"전량 처분 후에도 예상 부족분 {floor_won(remaining_debt):,}원")
    return LiquidationPlan(
        mode=mode,
        orders=orders,
        debt=debt,
        market_value=market_value,
        ratio_before=display_ratio(ratio_before),
        expected_proceeds=proceeds,
        expected_ratio_after=ratio_after,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class Settlement:
    net_proceeds: Decimal
    to_interest: Decimal
    to_principal: Decimal
    surplus: Decimal  # 잉여금 — 차주에게 반환
    remaining_interest: Decimal
    remaining_principal: Decimal

    @property
    def debt_cleared(self) -> bool:
        return self.remaining_interest == 0 and self.remaining_principal == 0

    @property
    def remaining_debt(self) -> Decimal:
        return self.remaining_interest + self.remaining_principal


def settle(net_proceeds: Decimal, accrued_interest: Decimal, outstanding_principal: Decimal) -> Settlement:
    """체결 순대금 상계: 이자 → 원금 → 잉여금."""
    net = floor_won(max(net_proceeds, ZERO))
    to_interest = min(net, accrued_interest)
    rest = net - to_interest
    to_principal = min(rest, outstanding_principal)
    surplus = rest - to_principal
    return Settlement(
        net_proceeds=net,
        to_interest=to_interest,
        to_principal=to_principal,
        surplus=surplus,
        remaining_interest=accrued_interest - to_interest,
        remaining_principal=outstanding_principal - to_principal,
    )
