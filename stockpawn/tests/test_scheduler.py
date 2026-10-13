"""스케줄러: 잡 등록(KST cron) + 거래일·장시간 가드."""

from datetime import date

import pytest

from app import scheduler
from app.adapters.broker import MockBroker
from app.adapters.price import InMemoryPriceSource
from app.business_days import WeekdayCalendar
from app.db import init_db, make_engine
from app.services import Deps
from factories import FakeClock, kst


@pytest.fixture
def setup():
    clock = FakeClock(kst(2026, 3, 2))
    prices = InMemoryPriceSource()
    deps = Deps(prices=prices, broker=MockBroker(prices, today=clock.today),
                calendar=WeekdayCalendar({date(2026, 3, 3)}), clock=clock)
    engine = make_engine("sqlite://")
    init_db(engine)
    return engine, deps, clock


def test_jobs_are_registered_with_kst_cron(setup):
    engine, deps, _ = setup
    sched = scheduler.build_scheduler(engine, deps)
    jobs = {j.id: str(j.trigger) for j in sched.get_jobs()}
    assert jobs == {
        "accrue_interest": "cron[hour='0', minute='5']",
        "liquidation_orders": "cron[day_of_week='mon-fri', hour='8', minute='45']",
        "intraday_valuation": "cron[day_of_week='mon-fri', hour='9-15', minute='*/10']",
        "post_close_valuation": "cron[day_of_week='mon-fri', hour='15', minute='40']",
    }
    assert str(sched.timezone) == "Asia/Seoul"


@pytest.mark.parametrize(
    "now,expected",
    [
        (kst(2026, 3, 7, 10), "skip:holiday"),  # 토요일
        (kst(2026, 3, 3, 10), "skip:holiday"),  # 공휴일
        (kst(2026, 3, 2, 15, 50), "skip:market_closed"),  # 15:30 이후 (cron 15:50 회차)
        (kst(2026, 3, 2, 8, 50), "skip:market_closed"),
    ],
)
def test_intraday_guard(setup, now, expected):
    engine, deps, clock = setup
    clock.set(now)
    assert scheduler.job_intraday_valuation(engine, deps) == expected


def test_jobs_run_on_trading_day(setup):
    engine, deps, clock = setup
    clock.set(kst(2026, 3, 2, 15, 30))
    assert scheduler.job_intraday_valuation(engine, deps)["evaluated"] == 0
    clock.set(kst(2026, 3, 2, 15, 40))
    assert "tiers_refreshed" in scheduler.job_post_close_valuation(engine, deps)
    clock.set(kst(2026, 3, 2, 8, 45))
    assert scheduler.job_liquidation_orders(engine, deps)["processed"] == 0
    clock.set(kst(2026, 3, 3, 8, 45))
    assert scheduler.job_liquidation_orders(engine, deps) == "skip:holiday"


def test_interest_job_runs_every_day(setup):
    engine, deps, clock = setup
    clock.set(kst(2026, 3, 7, 0, 5))  # 토요일에도 이자는 쌓인다
    assert scheduler.job_accrue_interest(engine, deps)["through"] == "2026-03-06"
