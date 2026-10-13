"""KRX 영업일 캘린더 — 마진콜 유예(2영업일) 만료일 계산용.

스펙 5단계: "영업일 기준(주말·공휴일 제외, pykrx 거래일 캘린더 사용)".

pykrx의 거래일 목록은 **실제 지난 거래일**(임시공휴일까지 반영)이라 과거 판정엔
가장 정확하지만 미래 날짜는 모른다. 마진콜 만료일은 미래이므로 소스를 나눈다:

  1) 어제까지: pykrx 실제 거래일
  2) 오늘 이후: ``exchange_calendars`` XKRX (설치 시) — 음력 공휴일·대체공휴일 규칙 내장
  3) 둘 다 불가: 주말 + ``EXTRA_HOLIDAYS`` 폴백 (경고 로그)

어느 경우든 ``EXTRA_HOLIDAYS``(임시공휴일 수동 지정)는 항상 휴장으로 덮어쓴다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from datetime import date, timedelta

from app import config
from app.clock import today_kst

log = logging.getLogger(__name__)


class TradingCalendar(ABC):
    @abstractmethod
    def is_business_day(self, d: date) -> bool: ...

    def next_business_day(self, d: date) -> date:
        """d 이후(당일 제외) 첫 영업일."""
        cur = d + timedelta(days=1)
        while not self.is_business_day(cur):
            cur += timedelta(days=1)
        return cur

    def previous_business_day(self, d: date) -> date:
        """d 이전(당일 제외) 마지막 영업일."""
        cur = d - timedelta(days=1)
        while not self.is_business_day(cur):
            cur -= timedelta(days=1)
        return cur

    def add_business_days(self, d: date, n: int) -> date:
        """d 다음 날부터 세어 n번째 영업일. 마진콜일 T + 2영업일 = 유예 만료일."""
        if n < 0:
            raise ValueError("n은 0 이상")
        cur = d
        for _ in range(n):
            cur = self.next_business_day(cur)
        return cur


class WeekdayCalendar(TradingCalendar):
    """주말 + 지정 휴일만 휴장. 테스트·오프라인 시뮬레이션·최후 폴백용."""

    def __init__(self, holidays: Iterable[date] = ()) -> None:
        self.holidays = frozenset(holidays)

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays


class KrxCalendar(TradingCalendar):
    """pykrx(과거) + exchange_calendars(미래) + 수동 휴장일. 위 모듈 docstring 참고."""

    def __init__(
        self,
        extra_holidays: Iterable[date] = (),
        today: Callable[[], date] = today_kst,
    ) -> None:
        self.extra = frozenset(extra_holidays)
        self._today = today
        self._fallback = WeekdayCalendar(self.extra)
        self._past_cache: dict[tuple[int, int], frozenset[date] | None] = {}
        self._xkrx = None
        self._xkrx_loaded = False

    def is_business_day(self, d: date) -> bool:
        if d in self.extra:
            return False
        if d < self._today():
            past = self._pykrx_month(d.year, d.month)
            if past is not None:
                return d in past
        xkrx = self._exchange_calendar()
        if xkrx is not None:
            try:
                import pandas as pd

                return bool(xkrx.is_session(pd.Timestamp(d)))
            except Exception as e:  # noqa: BLE001 — 범위 밖 날짜 등
                log.warning("xkrx_is_session_failed date=%s error=%s", d, e)
        return self._fallback.is_business_day(d)

    def _pykrx_month(self, year: int, month: int) -> frozenset[date] | None:
        key = (year, month)
        if key not in self._past_cache:
            self._past_cache[key] = self._load_pykrx_month(year, month)
        return self._past_cache[key]

    @staticmethod
    def _load_pykrx_month(year: int, month: int) -> frozenset[date] | None:
        try:
            from pykrx import stock

            days = stock.get_previous_business_days(year=year, month=month)
        except Exception as e:  # noqa: BLE001
            log.warning("pykrx_business_days_failed %04d-%02d error=%s", year, month, e)
            return None
        if not days:  # KRX 로그인 실패 시 빈 리스트 — 폴백으로 넘긴다
            log.warning("pykrx_business_days_empty %04d-%02d (KRX_ID/KRX_PW 확인)", year, month)
            return None
        return frozenset(ts.date() for ts in days)

    def _exchange_calendar(self):
        if not self._xkrx_loaded:
            self._xkrx_loaded = True
            try:
                import exchange_calendars as xcals

                self._xkrx = xcals.get_calendar("XKRX")
            except Exception as e:  # noqa: BLE001 — 미설치 포함
                log.warning(
                    "exchange_calendars 사용 불가(%s) — 미래 휴장일은 주말+EXTRA_HOLIDAYS로만 판정", e
                )
        return self._xkrx


def default_calendar() -> TradingCalendar:
    return KrxCalendar(extra_holidays=config.EXTRA_HOLIDAYS)
