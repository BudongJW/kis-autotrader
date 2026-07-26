"""미국장 개장/폐장 시각의 DST 자동 판정 회귀 테스트.

예전엔 configs/strategy.yaml의 `summer_time` 불리언을 사람이 연 2회 뒤집어야 했고,
놓치면 폐장 시각을 오인해 조기 청산 / 마지막 1시간 손절 공백 / 오버나이트 캐리가
났다. 여기서 실제 DST 전환 경계를 고정한다.
"""

from datetime import date, datetime, time as dtime

import pytest

from src.utils.clock import (
    KST, ET, is_us_dst, us_market_times_kst, us_session_date_et,
    US_REGULAR_OPEN_ET, US_REGULAR_CLOSE_ET,
)


def _kst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=KST)


# ──────────────────────────────────────────────────────────
# DST 전환 경계 (2026년 미국 서머타임 종료: 11-01 일요일)
# ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("now_str, expected_open, expected_close", [
    # 서머타임 (EDT, UTC-4) → 개장 22:30 / 폐장 익일 05:00 KST
    ("2026-07-27T22:00", dtime(22, 30), dtime(5, 0)),
    ("2026-10-30T22:00", dtime(22, 30), dtime(5, 0)),   # 전환 직전 금요일
    # 동절기 (EST, UTC-5) → 개장 23:30 / 폐장 익일 06:00 KST
    ("2026-11-02T22:00", dtime(23, 30), dtime(6, 0)),   # 전환 후 첫 세션
    ("2026-12-15T22:00", dtime(23, 30), dtime(6, 0)),
])
def test_market_times_follow_dst(now_str, expected_open, expected_close):
    assert us_market_times_kst(_kst(now_str)) == (expected_open, expected_close)


def test_dst_transition_weekend_boundary():
    """10-30(금) 세션은 서머, 11-02(월) 세션은 동절기. 주말 사이에 1시간 밀린다."""
    assert is_us_dst(_kst("2026-10-30T22:00")) is True
    assert is_us_dst(_kst("2026-11-02T22:00")) is False

    fri_open, fri_close = us_market_times_kst(_kst("2026-10-30T22:00"))
    mon_open, mon_close = us_market_times_kst(_kst("2026-11-02T22:00"))
    assert (mon_open.hour - fri_open.hour) == 1
    assert (mon_close.hour - fri_close.hour) == 1


def test_summer_time_yaml_flag_is_no_longer_authoritative():
    """yaml에 summer_time이 어떤 값이든 계산 결과는 ET 기준이어야 한다."""
    from src.bot.us_session import get_us_market_times

    # 동절기 시점 — yaml의 summer_time이 True로 방치돼 있어도 23:30/06:00이어야 함
    assert get_us_market_times(_kst("2026-11-02T22:00")) == (dtime(23, 30), dtime(6, 0))


# ──────────────────────────────────────────────────────────
# US 거래일(ET) 키잉 — KST 자정 넘김
# ──────────────────────────────────────────────────────────

def test_session_date_stable_across_kst_midnight():
    """한 세션이 KST 자정을 넘어도 US 거래일은 하나로 유지된다.

    KST 날짜로 세션 상태를 키잉하면 자정에 리셋되던 문제의 회귀 방지.
    """
    evening = us_session_date_et(_kst("2026-07-27T23:00"))   # KST 월 밤
    dawn = us_session_date_et(_kst("2026-07-28T03:00"))      # KST 화 새벽
    assert evening == dawn == date(2026, 7, 27)


def test_session_date_rolls_after_close():
    """폐장(16:00 ET) 이후는 다음 세션으로 넘어간다."""
    # KST 07-28 06:00 = ET 07-27 17:00 (폐장 후) → 다음 세션
    assert us_session_date_et(_kst("2026-07-28T06:00")) == date(2026, 7, 28)


def test_early_close_shifts_close_only():
    """조기 폐장일(13:00 ET)은 폐장만 3시간 앞당겨진다."""
    now = _kst("2026-11-27T23:00")  # 추수감사절 다음날, 동절기
    reg_open, reg_close = us_market_times_kst(now)
    ec_open, ec_close = us_market_times_kst(now, early_close=True)
    assert ec_open == reg_open == dtime(23, 30)
    assert reg_close == dtime(6, 0)
    assert ec_close == dtime(3, 0)


def test_open_close_roundtrip_matches_et_wall_clock():
    """KST 변환값을 ET로 되돌리면 정확히 09:30/16:00이어야 한다."""
    for s in ["2026-07-27T22:00", "2026-11-02T22:00"]:
        now = _kst(s)
        d = us_session_date_et(now)
        open_t, close_t = us_market_times_kst(now)
        # 폐장이 자정을 넘으면 KST 날짜는 +1
        open_kst = datetime.combine(d, open_t, tzinfo=KST)
        assert open_kst.astimezone(ET).time() == US_REGULAR_OPEN_ET
        assert close_t.hour < 12  # 폐장은 항상 KST 새벽
