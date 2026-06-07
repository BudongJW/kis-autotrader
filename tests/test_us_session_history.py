"""미국 야간봇 회귀 테스트.

- 개장대기 dead-zone 종료: 폐장 후 깬 watchdog run이 다음 개장까지
  PREOPEN_WAIT_LIMIT_MIN 넘게 남았으면 즉시 종료해야 함.
- fetch_us_history yfinance 폴백: KIS 일봉이 비거나 실패하면 yfinance로 폴백.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

KST = ZoneInfo("Asia/Seoul")


# ──────────────────────────────────────────────────────────
# 개장대기 dead-zone
# ──────────────────────────────────────────────────────────

def _dt(h, m):
    return datetime(2026, 5, 29, h, m, tzinfo=KST)


def test_minutes_until_open_preopen():
    """개장 직전(22:10, 개장 22:30) → 약 20분."""
    from src.bot.night_run import _minutes_until_open
    mins = _minutes_until_open(_dt(22, 10), dtime(22, 30))
    assert 19 <= mins <= 21


def test_minutes_until_open_postclose_is_huge():
    """폐장 후(06:35, 개장 22:30) → 다음날 개장까지 950분+."""
    from src.bot.night_run import _minutes_until_open
    mins = _minutes_until_open(_dt(6, 35), dtime(22, 30))
    assert mins > 900


def test_deadzone_distinguished_by_limit():
    """pre-open은 한도 이내, 폐장 후 dead zone은 한도 초과여야 한다."""
    from src.bot.night_run import _minutes_until_open, PREOPEN_WAIT_LIMIT_MIN
    preopen = _minutes_until_open(_dt(22, 10), dtime(22, 30))
    deadzone = _minutes_until_open(_dt(6, 35), dtime(22, 30))
    # 가장 이른 pre-open cron(개장 60분 전, 21:30)도 한도 안에 들어와야 함
    earliest_cron = _minutes_until_open(_dt(21, 30), dtime(22, 30))
    assert preopen <= PREOPEN_WAIT_LIMIT_MIN
    assert earliest_cron <= PREOPEN_WAIT_LIMIT_MIN
    assert deadzone > PREOPEN_WAIT_LIMIT_MIN


def test_minutes_until_open_winter():
    """동절기 개장 23:30 기준. 폐장 후 07:00 → dead zone."""
    from src.bot.night_run import _minutes_until_open, PREOPEN_WAIT_LIMIT_MIN
    assert _minutes_until_open(_dt(7, 0), dtime(23, 30)) > PREOPEN_WAIT_LIMIT_MIN
    assert _minutes_until_open(_dt(23, 0), dtime(23, 30)) <= PREOPEN_WAIT_LIMIT_MIN


# ──────────────────────────────────────────────────────────
# 주말 휴장 체크: 금요일 미국장의 토요일 새벽분(KST)을 막던 버그 수정 (6-08)
# Mon=0 .. Sat=5, Sun=6 / open 22:30, close 05:00 KST(서머)
# ──────────────────────────────────────────────────────────
OPEN = dtime(22, 30)
CLOSE = dtime(5, 0)


def test_friday_us_session_saturday_dawn_allowed():
    """토요일 새벽(00:00~05:00 KST)은 미국 금요일장 → 거래 허용(버그 수정 핵심)."""
    from src.bot.night_run import _us_weekend_closed
    assert _us_weekend_closed(5, dtime(0, 34), OPEN, CLOSE) is False
    assert _us_weekend_closed(5, dtime(3, 5), OPEN, CLOSE) is False


def test_saturday_evening_closed():
    """토요일 저녁(22:30 KST~)은 미국 토요일 → 휴장."""
    from src.bot.night_run import _us_weekend_closed
    assert _us_weekend_closed(5, dtime(22, 30), OPEN, CLOSE) is True
    assert _us_weekend_closed(5, dtime(23, 0), OPEN, CLOSE) is True


def test_sunday_and_monday_dawn_closed():
    """일요일 전체·월요일 새벽(=일요일 미국장)은 휴장."""
    from src.bot.night_run import _us_weekend_closed
    assert _us_weekend_closed(6, dtime(2, 0), OPEN, CLOSE) is True   # 일 새벽(토US)
    assert _us_weekend_closed(6, dtime(23, 0), OPEN, CLOSE) is True  # 일 저녁
    assert _us_weekend_closed(0, dtime(2, 0), OPEN, CLOSE) is True   # 월 새벽(일US)


def test_weekday_sessions_open():
    """평일 미국장(저녁·새벽)은 모두 거래일."""
    from src.bot.night_run import _us_weekend_closed
    assert _us_weekend_closed(4, dtime(23, 0), OPEN, CLOSE) is False  # 금 저녁
    assert _us_weekend_closed(1, dtime(2, 0), OPEN, CLOSE) is False   # 화 새벽(월US)
    assert _us_weekend_closed(2, dtime(23, 0), OPEN, CLOSE) is False  # 수 저녁


# ──────────────────────────────────────────────────────────
# fetch_us_history 폴백 디스패치
# ──────────────────────────────────────────────────────────

def _df(n=10):
    idx = pd.date_range("2026-01-01", periods=n, freq="D", name="date")
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
        index=idx,
    )


def test_fetch_uses_kis_when_ok(monkeypatch):
    """KIS가 충분한 데이터를 주면 yfinance를 부르지 않는다."""
    import src.bot.us_session as us

    kis_df = _df(20)
    monkeypatch.setattr(us, "_fetch_us_history_kis", lambda *a, **k: kis_df)

    def _yf_should_not_be_called(*a, **k):
        raise AssertionError("yfinance 폴백이 불려서는 안 됨")

    monkeypatch.setattr(us, "_fetch_us_history_yf", _yf_should_not_be_called)
    out = us.fetch_us_history(None, "QQQ", "NASD")
    assert len(out) == 20


def test_fetch_falls_back_when_kis_empty(monkeypatch):
    """KIS가 None(빈 데이터) → yfinance 폴백 사용."""
    import src.bot.us_session as us

    yf_df = _df(15)
    monkeypatch.setattr(us, "_fetch_us_history_kis", lambda *a, **k: None)
    monkeypatch.setattr(us, "_fetch_us_history_yf", lambda *a, **k: yf_df)
    out = us.fetch_us_history(None, "SPY", "NYSE")
    assert len(out) == 15


def test_fetch_falls_back_when_kis_raises(monkeypatch):
    """KIS가 예외(rt_cd!=0 등) → yfinance 폴백 사용."""
    import src.bot.us_session as us

    def _raise(*a, **k):
        raise RuntimeError("해외 일봉 실패")

    yf_df = _df(12)
    monkeypatch.setattr(us, "_fetch_us_history_kis", _raise)
    monkeypatch.setattr(us, "_fetch_us_history_yf", lambda *a, **k: yf_df)
    out = us.fetch_us_history(None, "SMH", "NASD")
    assert len(out) == 12


def test_fetch_raises_when_both_empty(monkeypatch):
    """KIS·yfinance 둘 다 비면 RuntimeError."""
    import src.bot.us_session as us

    monkeypatch.setattr(us, "_fetch_us_history_kis", lambda *a, **k: None)
    monkeypatch.setattr(us, "_fetch_us_history_yf", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        us.fetch_us_history(None, "QQQ", "NASD")


def test_yf_fallback_flattens_multiindex(monkeypatch):
    """yfinance 신버전 MultiIndex 컬럼을 평탄화하고 소문자로 정규화."""
    import src.bot.us_session as us

    idx = pd.date_range("2026-01-01", periods=30, freq="D")
    cols = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close", "Volume"], ["QQQ"]]
    )
    raw = pd.DataFrame(1.0, index=idx, columns=cols)

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = lambda *a, **k: raw
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    out = us._fetch_us_history_yf("QQQ", days=70)
    assert out is not None
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.name == "date"
    assert len(out) == 30
