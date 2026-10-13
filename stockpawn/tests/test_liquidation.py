"""처분 엔진 (스펙 2.4): 부분/전량 처분 수량 계산, 잉여금 정산."""

import math
from decimal import Decimal

import pytest

from app import config
from app.engines import liquidation as liq
from app.engines.liquidation import LiquidationMode as M
from app.engines.liquidation import Position
from app.engines.risk import LiquidationReason as R

C = config.SELL_COST_RATE
T = config.PARTIAL_LIQUIDATION_TARGET_RATIO


def ratio_after(v: Decimal, d: Decimal, p: Decimal, q: int) -> Decimal:
    net = liq.expected_fill_price(p) * (1 - C) * q
    return (v - p * q) / (d - net)


def test_expected_fill_price_is_97_percent_of_prev_close():
    assert liq.expected_fill_price(Decimal("100000")) == Decimal("97000.00")


def test_payoff_quantity_matches_spec_formula():
    """스펙: ceil((원금 + 미수이자 + 예상수수료) / 예상체결가). 수수료는 수량에 비례하므로
    q는 'q·F − q·F·c ≥ D'를 만족하는 최소 정수여야 한다."""
    debt, ref = Decimal("10164383"), Decimal("50000")
    q = liq.payoff_quantity(debt, ref, holding=10_000)
    f = liq.expected_fill_price(ref)
    assert q * f - q * f * C >= debt  # 수수료 떼고도 원리금 회수
    assert (q - 1) * f - (q - 1) * f * C < debt  # 최소 수량
    assert q == math.ceil((debt + q * f * C) / f)  # 스펙 식과 일치


def test_payoff_quantity_capped_by_holding():
    assert liq.payoff_quantity(Decimal("10000000"), Decimal("1000"), holding=50) == 50
    assert liq.payoff_quantity(Decimal("0"), Decimal("1000"), holding=50) == 0


def test_partial_quantity_restores_150_percent_minimally():
    ref, holding = Decimal("130000"), 1000
    v, d = ref * holding, Decimal("100000000")  # 담보비율 130%
    q = liq.partial_quantity(v, d, ref, holding)
    assert 0 < q < holding
    assert ratio_after(v, d, ref, q) >= T
    assert ratio_after(v, d, ref, q - 1) < T


def test_partial_quantity_zero_when_already_at_target():
    assert liq.partial_quantity(Decimal("150"), Decimal("100"), Decimal("1"), 150) == 0


def test_partial_when_selling_cannot_help_sells_everything():
    # 비율 102%: 0.97·(1−c)로 팔면 팔수록 비율이 떨어지는 구간 → 보유 전량
    ref, holding = Decimal("102"), 1000
    assert liq.partial_quantity(ref * holding, Decimal("100000"), ref, holding) == holding


@pytest.mark.parametrize(
    "reasons,ratio,mode",
    [
        ([R.RATIO_BREACH], Decimal("1.15"), M.PARTIAL),
        ([R.MARGIN_CALL_EXPIRED], Decimal("1.30"), M.PARTIAL),
        ([R.MATURITY_EXPIRED], Decimal("2.00"), M.PAYOFF),
        ([R.INELIGIBLE_COLLATERAL], Decimal("2.00"), M.PAYOFF),
        ([R.MANUAL], Decimal("1.60"), M.PAYOFF),
        ([R.RATIO_BREACH], Decimal("0.99"), M.FULL),  # 전량 처분은 100% 미만일 때만
        ([R.MATURITY_EXPIRED], Decimal("0.80"), M.FULL),
        ([R.RATIO_BREACH], Decimal("1.00"), M.PARTIAL),
    ],
)
def test_choose_mode(reasons, ratio, mode):
    assert liq.choose_mode(reasons, ratio) == mode


def test_plan_partial_single_stock():
    pos = [Position("005930", 1000, Decimal("130000"), "A")]
    plan = liq.plan_liquidation(pos, Decimal("100000000"), M.PARTIAL)
    assert plan.mode == M.PARTIAL and len(plan.orders) == 1
    assert plan.orders[0].qty == liq.partial_quantity(Decimal("130000000"), Decimal("100000000"),
                                                      Decimal("130000"), 1000)
    assert plan.ratio_before == Decimal("1.3000")
    assert plan.expected_ratio_after >= T


def test_plan_partial_sells_riskier_collateral_first():
    pos = [Position("A00001", 1000, Decimal("100000"), "A", Decimal("0.10")),
           Position("C00001", 100, Decimal("100000"), "C", Decimal("0.40"))]
    # V = 1.1억, D = 0.85억 → 129%
    plan = liq.plan_liquidation(pos, Decimal("85000000"), M.PARTIAL)
    assert plan.orders[0].code == "C00001" and plan.orders[0].qty == 100  # C 전량 먼저
    assert plan.orders[1].code == "A00001"
    assert plan.expected_ratio_after >= T


def test_plan_payoff_covers_debt_and_releases_rest():
    pos = [Position("005930", 1000, Decimal("50000"), "A")]
    debt = Decimal("20000000")
    plan = liq.plan_liquidation(pos, debt, M.PAYOFF)
    assert plan.expected_proceeds >= debt
    assert plan.orders[0].qty == liq.payoff_quantity(debt, Decimal("50000"), 1000) < 1000
    assert plan.expected_ratio_after is None  # 채무 정리 예상


def test_plan_full_sells_everything_tradable_and_reports_shortfall():
    pos = [Position("A00001", 100, Decimal("50000"), "A"),
           Position("H00001", 100, Decimal("50000"), "B", tradable=False)]
    plan = liq.plan_liquidation(pos, Decimal("12000000"), M.FULL)  # V 1,000만 / D 1,200만 → 83%
    assert [(o.code, o.qty) for o in plan.orders] == [("A00001", 100)]
    assert any("거래정지" in n for n in plan.notes)


def test_plan_full_with_no_tradable_collateral_has_no_orders():
    pos = [Position("H00001", 100, Decimal("50000"), "X", tradable=False)]
    plan = liq.plan_liquidation(pos, Decimal("12000000"), M.FULL)
    assert plan.orders == () and plan.total_qty == 0


def test_plan_all_sold_still_short_is_noted():
    pos = [Position("A00001", 100, Decimal("50000"), "A")]
    plan = liq.plan_liquidation(pos, Decimal("6000000"), M.FULL)
    assert any("부족분" in n for n in plan.notes)


# ── 정산 ──

def test_settle_interest_then_principal_then_surplus():
    s = liq.settle(Decimal("10500000"), Decimal("164383"), Decimal("10000000"))
    assert (s.to_interest, s.to_principal, s.surplus) == (Decimal("164383"), Decimal("10000000"), Decimal("335617"))
    assert s.debt_cleared and s.remaining_debt == 0


def test_settle_partial_proceeds():
    s = liq.settle(Decimal("3000000"), Decimal("164383"), Decimal("10000000"))
    assert (s.to_interest, s.to_principal, s.surplus) == (Decimal("164383"), Decimal("2835617"), Decimal("0"))
    assert s.remaining_principal == Decimal("7164383") and not s.debt_cleared


def test_settle_floors_fractional_proceeds():
    s = liq.settle(Decimal("100.9"), Decimal("50"), Decimal("1000"))
    assert s.net_proceeds == Decimal("100") and s.to_principal == Decimal("50")
