"""리스크 모니터 (스펙 2.3): 상태 전이 전 경로 + 영업일 기준 마진콜 유예."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.business_days import KrxCalendar, WeekdayCalendar
from app.engines import risk
from app.engines.risk import LiquidationReason as R
from app.engines.risk import LoanStatus as S
from factories import kst

CAL = WeekdayCalendar()
DEBT = Decimal("10000000")
MATURITY = date(2026, 6, 1)
THU = kst(2026, 3, 12)  # 목요일 10:00


def ratio_input(ratio: str, status=S.NORMAL, deadline=None, maturity=MATURITY, ineligible=False):
    return risk.RiskInput(status=status, market_value=DEBT * Decimal(ratio), debt=DEBT, maturity_date=maturity,
                          margin_call_deadline=deadline, has_ineligible_collateral=ineligible)


# ── 영업일 캘린더 ──

def test_add_business_days_skips_weekend():
    assert CAL.add_business_days(date(2026, 3, 12), 2) == date(2026, 3, 16)  # 목 → 월
    assert CAL.add_business_days(date(2026, 3, 13), 2) == date(2026, 3, 17)  # 금 → 화
    assert CAL.add_business_days(date(2026, 3, 14), 2) == date(2026, 3, 17)  # 토 → 화


def test_add_business_days_skips_holidays():
    cal = WeekdayCalendar({date(2026, 3, 16)})
    assert cal.add_business_days(date(2026, 3, 12), 2) == date(2026, 3, 17)
    assert cal.next_business_day(date(2026, 3, 13)) == date(2026, 3, 17)
    assert cal.previous_business_day(date(2026, 3, 17)) == date(2026, 3, 13)


def test_krx_calendar_uses_pykrx_for_past_and_fallback_for_future(monkeypatch):
    # 과거 3월: pykrx 거래일 목록에 3/3(화)이 없다 → 임시공휴일로 판정
    march = frozenset(d for d in (date(2026, 3, x) for x in range(1, 32))
                      if d.weekday() < 5 and d != date(2026, 3, 3))
    monkeypatch.setattr(KrxCalendar, "_load_pykrx_month", staticmethod(lambda y, m: march if m == 3 else None))
    monkeypatch.setattr(KrxCalendar, "_exchange_calendar", lambda self: None)
    cal = KrxCalendar(extra_holidays={date(2026, 3, 24)}, today=lambda: date(2026, 3, 20))
    assert not cal.is_business_day(date(2026, 3, 3))  # 과거: pykrx
    assert cal.is_business_day(date(2026, 3, 4))
    assert cal.is_business_day(date(2026, 3, 23))  # 미래: 주말 폴백
    assert not cal.is_business_day(date(2026, 3, 24))  # 수동 휴장일이 항상 우선
    assert cal.add_business_days(date(2026, 3, 20), 2) == date(2026, 3, 25)


def test_krx_calendar_falls_back_when_pykrx_unavailable(monkeypatch):
    monkeypatch.setattr(KrxCalendar, "_load_pykrx_month", staticmethod(lambda y, m: None))
    monkeypatch.setattr(KrxCalendar, "_exchange_calendar", lambda self: None)
    cal = KrxCalendar(today=lambda: date(2026, 3, 20))
    assert cal.is_business_day(date(2026, 3, 3)) and not cal.is_business_day(date(2026, 3, 7))


# ── 상태 전이 ──

def test_normal_stays_normal():
    d = risk.evaluate(ratio_input("1.50"), THU, CAL)
    assert d.status == S.NORMAL and d.ratio == Decimal("1.5000") and d.margin_call_deadline is None


def test_normal_to_margin_call_with_business_day_deadline():
    d = risk.evaluate(ratio_input("1.30"), THU, CAL)
    assert d.status == S.MARGIN_CALL
    assert d.margin_call_deadline == date(2026, 3, 16)  # 목 + 2영업일 = 월 (주말 제외)


def test_margin_call_stays_within_grace_and_keeps_deadline():
    deadline = date(2026, 3, 16)
    d = risk.evaluate(ratio_input("1.30", S.MARGIN_CALL, deadline), kst(2026, 3, 16, 15, 29), CAL)
    assert d.status == S.MARGIN_CALL and d.margin_call_deadline == deadline


def test_margin_call_cured_returns_to_normal():
    d = risk.evaluate(ratio_input("1.40", S.MARGIN_CALL, date(2026, 3, 16)), THU, CAL)
    assert d.status == S.NORMAL and d.margin_call_deadline is None


def test_margin_call_grace_expires_at_deadline_close():
    deadline = date(2026, 3, 16)
    d = risk.evaluate(ratio_input("1.30", S.MARGIN_CALL, deadline), kst(2026, 3, 16, 15, 30), CAL)
    assert d.status == S.LIQUIDATION_PENDING and d.liquidation_reasons == (R.MARGIN_CALL_EXPIRED,)


def test_expired_margin_call_but_cured_is_normal():
    d = risk.evaluate(ratio_input("1.45", S.MARGIN_CALL, date(2026, 3, 16)), kst(2026, 3, 17), CAL)
    assert d.status == S.NORMAL


def test_ratio_below_120_is_immediate_liquidation_from_normal():
    d = risk.evaluate(ratio_input("1.1999"), THU, CAL)
    assert d.status == S.LIQUIDATION_PENDING and d.liquidation_reasons == (R.RATIO_BREACH,)


def test_ratio_below_120_during_grace_is_immediate_liquidation():
    d = risk.evaluate(ratio_input("1.10", S.MARGIN_CALL, date(2026, 3, 16)), THU, CAL)
    assert d.status == S.LIQUIDATION_PENDING and R.RATIO_BREACH in d.liquidation_reasons


@pytest.mark.parametrize("ratio,expected", [("1.40", S.NORMAL), ("1.3999", S.MARGIN_CALL), ("1.20", S.MARGIN_CALL)])
def test_threshold_boundaries(ratio, expected):
    assert risk.evaluate(ratio_input(ratio), THU, CAL).status == expected


def test_maturity_grace_boundary():
    maturity = date(2026, 3, 5)  # +7일 = 3/12(THU) → 아직 유예
    assert risk.evaluate(ratio_input("2.0", maturity=maturity), THU, CAL).status == S.NORMAL
    d = risk.evaluate(ratio_input("2.0", maturity=date(2026, 3, 4)), THU, CAL)  # 3/11 경과
    assert d.status == S.LIQUIDATION_PENDING and d.liquidation_reasons == (R.MATURITY_EXPIRED,)


def test_tier_x_collateral_triggers_liquidation():
    d = risk.evaluate(ratio_input("3.0", ineligible=True), THU, CAL)
    assert d.status == S.LIQUIDATION_PENDING and d.liquidation_reasons == (R.INELIGIBLE_COLLATERAL,)


def test_multiple_reasons_are_collected():
    d = risk.evaluate(ratio_input("1.0", S.MARGIN_CALL, date(2026, 3, 10), date(2026, 1, 1), True), THU, CAL)
    assert set(d.liquidation_reasons) == {R.RATIO_BREACH, R.MARGIN_CALL_EXPIRED, R.MATURITY_EXPIRED,
                                          R.INELIGIBLE_COLLATERAL}


def test_liquidation_pending_is_sticky():
    d = risk.evaluate(ratio_input("3.0", S.LIQUIDATION_PENDING), THU, CAL)
    assert d.status == S.LIQUIDATION_PENDING


@pytest.mark.parametrize("status", [S.LIQUIDATING, S.CLOSED])
def test_terminal_or_in_progress_states_unchanged(status):
    assert risk.evaluate(ratio_input("0.5", status), THU, CAL).status == status


def test_no_debt_is_left_alone():
    inp = risk.RiskInput(S.NORMAL, Decimal(100), Decimal(0), MATURITY)
    d = risk.evaluate(inp, THU, CAL)
    assert d.status == S.NORMAL and d.ratio is None


def test_naive_datetime_is_treated_as_kst():
    deadline = date(2026, 3, 16)
    d = risk.evaluate(ratio_input("1.30", S.MARGIN_CALL, deadline), datetime(2026, 3, 16, 15, 30), CAL)
    assert d.status == S.LIQUIDATION_PENDING


def test_display_ratio_never_rounds_up_across_threshold():
    assert risk.display_ratio(Decimal("1.399999")) == Decimal("1.3999")
