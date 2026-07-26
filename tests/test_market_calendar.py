"""거래일·조기폐장 캘린더 회귀 테스트.

기존엔 휴장일 판정이 주말 체크 하나뿐이라
  - 한국 공휴일에 봇이 전일 종가로 매매 판단을 내리고
  - 미국 조기 폐장일(13:00 ET)에 폐장 3시간 뒤 강제청산을 시도해 실패
    → 의도치 않은 오버나이트 캐리
가 났다. 여기서 그 케이스를 고정한다.
"""

from datetime import date, datetime, time as dtime

import pytest

from src.utils.clock import KST
from src.utils.market_calendar import (
    is_kr_trading_day, is_us_trading_day, is_us_early_close,
    us_close_time_et, us_market_times_kst,
)

xcals = pytest.importorskip(
    "exchange_calendars",
    reason="exchange_calendars 미설치 — 주말 폴백 경로만 동작",
)


def _kst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=KST)


# ──────────────────────────────────────────────────────────
# 한국 (KRX)
# ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("d, expected", [
    ("2026-07-27", True),    # 평범한 월요일
    ("2026-08-14", True),    # 광복절 전날(금)
    ("2026-08-17", False),   # 광복절(8/15 토) 대체공휴일 월요일
    ("2026-09-24", False),   # 추석 연휴
    ("2026-09-25", False),   # 추석 연휴
    ("2026-07-25", False),   # 토요일
])
def test_kr_trading_day(d, expected):
    assert is_kr_trading_day(date.fromisoformat(d)) is expected


def test_kr_lunar_holiday_is_not_weekday_detectable():
    """추석은 평일인데도 휴장 — 주말 체크만으로는 절대 못 잡는 케이스."""
    chuseok = date(2026, 9, 25)
    assert chuseok.weekday() < 5      # 금요일
    assert is_kr_trading_day(chuseok) is False


# ──────────────────────────────────────────────────────────
# 미국 (NYSE) 휴장·조기폐장
# ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("d, expected", [
    ("2026-11-25", True),    # 추수감사절 전날
    ("2026-11-26", False),   # 추수감사절
    ("2026-11-27", True),    # 다음날 — 열리지만 조기 폐장
    ("2026-12-25", False),   # 크리스마스
    ("2026-07-03", False),   # 독립기념일(7/4 토) 대체 휴장
])
def test_us_trading_day(d, expected):
    assert is_us_trading_day(date.fromisoformat(d)) is expected


@pytest.mark.parametrize("d, expected_close, early", [
    ("2026-11-25", dtime(16, 0), False),
    ("2026-11-27", dtime(13, 0), True),    # 추수감사절 다음날
    ("2026-12-24", dtime(13, 0), True),    # 크리스마스 이브
    ("2026-07-27", dtime(16, 0), False),
])
def test_us_early_close(d, expected_close, early):
    dd = date.fromisoformat(d)
    assert us_close_time_et(dd) == expected_close
    assert is_us_early_close(dd) is early


def test_early_close_pulls_kst_close_forward():
    """조기 폐장일엔 KST 폐장이 3시간 앞당겨져야 강제청산이 장중에 실행된다.

    이게 없으면 봇은 06:00 KST 폐장으로 믿고 05:45에 청산을 시도하는데
    실제로는 03:00에 이미 닫혀 주문이 실패 → 오버나이트 캐리.
    """
    # 2026-11-27은 동절기 → 정규 폐장 06:00 KST, 조기 폐장 03:00 KST
    open_t, close_t = us_market_times_kst(_kst("2026-11-27T23:45"))
    assert open_t == dtime(23, 30)
    assert close_t == dtime(3, 0)

    # 같은 주 정규 거래일은 06:00 유지
    _, normal_close = us_market_times_kst(_kst("2026-11-25T23:45"))
    assert normal_close == dtime(6, 0)


def test_us_holiday_blocks_market_hours():
    """휴장일엔 is_us_market_hours()가 시간대와 무관하게 False."""
    import src.bot.us_session as us

    # 추수감사절(2026-11-26) 세션 시간대에 해당하는 KST 시각
    holiday_kst = _kst("2026-11-26T23:45")
    assert is_us_trading_day(date(2026, 11, 26)) is False

    # enabled=True여도 휴장일이면 False여야 한다
    orig = us.load_us_config
    us.load_us_config = lambda: {"enabled": True}
    orig_now = us.now_kst
    us.now_kst = lambda: holiday_kst
    try:
        assert us.is_us_market_hours() is False
    finally:
        us.load_us_config = orig
        us.now_kst = orig_now
