"""테스트 데이터 팩토리. 네트워크 없이 결정적인 시세를 만든다."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.adapters.price.base import DailyBar, StockInfo
from app.business_days import WeekdayCalendar
from app.clock import KST

CAL = WeekdayCalendar()
BIG_VALUE = Decimal("1000000000000")  # 일 거래대금 1조 — 유동성 한도가 걸리지 않을 만큼


def business_days_ending(end: date, n: int) -> list[date]:
    days: list[date] = []
    d = end
    while len(days) < n:
        if CAL.is_business_day(d):
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def zigzag(low: int, high: int, n: int) -> list[Decimal]:
    """low, high, low, ... — 로그수익률 ±ln(high/low) 교대 → 변동성을 정확히 알 수 있다."""
    return [Decimal(low if i % 2 == 0 else high) for i in range(n)]


def bars(
    closes: Sequence[int | Decimal],
    end: date,
    trading_value: Decimal = BIG_VALUE,
    volume: int = 1_000_000,
) -> list[DailyBar]:
    dates = business_days_ending(end, len(closes))
    return [DailyBar(d, Decimal(c), Decimal(volume), trading_value) for d, c in zip(dates, closes, strict=True)]


def info(
    code: str = "005930",
    name: str = "삼성전자",
    market: str = "KOSPI",
    market_cap: Decimal | None = Decimal("400000000000000"),
    **flags: bool,
) -> StockInfo:
    return StockInfo(code=code, name=name, market=market, market_cap=market_cap, **flags)


def kst(y: int, m: int, d: int, hh: int = 10, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=KST)


class FakeClock:
    """조작 가능한 시계 (Deps.clock에 주입)."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now

    def today(self) -> date:
        return self.now.date()
