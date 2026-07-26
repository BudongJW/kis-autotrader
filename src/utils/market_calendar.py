"""거래일·조기폐장 캘린더 — KRX(XKRX)와 NYSE(XNYS).

기존엔 휴장일 판정이 **주말 체크 하나뿐**이었다. 그래서:

  - 한국 공휴일(설·추석·대체공휴일 등)에도 봇이 09:00~15:30 루프를 돌며 전일
    종가로 매매 판단을 내리고 거부될 주문을 냈다.
  - 미국 **조기 폐장일**(추수감사절 다음날·크리스마스 이브 등, 13:00 ET 마감)에
    봇은 폐장을 16:00 ET로 믿고 폐장 15분 전 강제청산을 시도했다. 실제로는 이미
    3시간 전에 장이 닫혀 주문이 실패 → **의도치 않은 오버나이트 캐리**.
    (DST 수동 플래그와 정확히 같은 실패 모드)

`exchange_calendars`가 KRX 음력 공휴일과 NYSE 조기폐장을 모두 다루므로 이를 쓴다.
라이브러리가 없으면 **주말 판정으로 폴백**한다 — 기존 동작과 같으므로 회귀는
없고, 폴백 중임을 경고 로그로 남긴다.

임시공휴일처럼 라이브러리 릴리스 후에 지정된 날짜는 configs/strategy.yaml의
`calendar.extra_holidays_kr` / `extra_holidays_us`로 덮어쓸 수 있다.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime
from functools import lru_cache
from pathlib import Path

import yaml

from src.utils.clock import (
    ET, US_EARLY_CLOSE_ET, US_REGULAR_CLOSE_ET,
    us_market_times_kst as _regular_times_kst,
    us_session_date_et, today_kst,
)
from src.utils.logger import log

CONFIG_PATH = Path("configs/strategy.yaml")


@lru_cache(maxsize=2)
def _calendar(code: str):
    """exchange_calendars 핸들. 미설치/오류면 None (주말 폴백)."""
    try:
        import exchange_calendars as xcals
    except ImportError:
        log.warning("exchange_calendars_missing",
                    hint="pip install exchange_calendars — 휴장일 판정이 주말 체크로 "
                         "폴백합니다. 공휴일·조기폐장에 잘못된 매매가 날 수 있습니다.")
        return None
    try:
        return xcals.get_calendar(code)
    except Exception as e:  # noqa: BLE001
        log.warning("exchange_calendar_load_failed", code=code, error=str(e))
        return None


@lru_cache(maxsize=1)
def _extra_holidays() -> tuple[frozenset[str], frozenset[str]]:
    """yaml에 수동 지정한 추가 휴장일 (임시공휴일 등). (KR, US) ISO 날짜 문자열."""
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cal = (yaml.safe_load(f) or {}).get("calendar", {}) or {}
    except Exception:
        return frozenset(), frozenset()
    def _norm(key: str) -> frozenset[str]:
        return frozenset(str(d)[:10] for d in (cal.get(key) or []))
    return _norm("extra_holidays_kr"), _norm("extra_holidays_us")


def _is_session(code: str, d: date, extra: frozenset[str]) -> bool:
    if d.isoformat() in extra:
        return False
    cal = _calendar(code)
    if cal is None:
        return d.weekday() < 5          # 폴백: 주말만 휴장 (기존 동작)
    try:
        import pandas as pd
        return bool(cal.is_session(pd.Timestamp(d)))
    except Exception as e:  # noqa: BLE001
        log.warning("is_session_failed", code=code, date=d.isoformat(), error=str(e))
        return d.weekday() < 5


# ──────────────────────────────────────────────────────────
# 한국 (KRX)
# ──────────────────────────────────────────────────────────

def is_kr_trading_day(d: date | None = None) -> bool:
    """KRX 정규 거래일인지 (KST 날짜 기준)."""
    return _is_session("XKRX", d or today_kst(), _extra_holidays()[0])


# ──────────────────────────────────────────────────────────
# 미국 (NYSE)
# ──────────────────────────────────────────────────────────

def is_us_trading_day(session_et: date | None = None) -> bool:
    """해당 **US 거래일(ET 날짜)**이 정규 거래일인지."""
    return _is_session("XNYS", session_et or us_session_date_et(),
                       _extra_holidays()[1])


def us_close_time_et(session_et: date | None = None) -> dtime:
    """해당 세션의 실제 폐장 시각(ET). 조기 폐장일이면 13:00."""
    d = session_et or us_session_date_et()
    cal = _calendar("XNYS")
    if cal is None:
        return US_REGULAR_CLOSE_ET
    try:
        import pandas as pd
        ts = pd.Timestamp(d)
        if not cal.is_session(ts):
            return US_REGULAR_CLOSE_ET
        return cal.session_close(ts).tz_convert(ET).time()
    except Exception as e:  # noqa: BLE001
        log.warning("us_session_close_failed", date=d.isoformat(), error=str(e))
        return US_REGULAR_CLOSE_ET


def is_us_early_close(session_et: date | None = None) -> bool:
    """조기 폐장일(정규 16:00보다 이른 마감)인지."""
    return us_close_time_et(session_et) < US_REGULAR_CLOSE_ET


def us_market_times_kst(now: datetime | None = None) -> tuple[dtime, dtime]:
    """캘린더를 반영한 (개장, 폐장) KST 시각. 조기 폐장일이면 폐장이 3시간 앞당겨진다.

    clock.us_market_times_kst()의 캘린더 인식 버전 — 봇은 이쪽을 써야 한다.
    """
    d = us_session_date_et(now)
    close_et = us_close_time_et(d)
    # 13:00 ET 이외의 비정규 마감은 아직 관측된 바 없다. 정규/조기 두 갈래로 처리하되
    # 예상 밖 값이면 경고를 남겨 조용히 틀리지 않게 한다.
    if close_et not in (US_REGULAR_CLOSE_ET, US_EARLY_CLOSE_ET):
        log.warning("us_unexpected_close_time", date=d.isoformat(),
                    close_et=close_et.strftime("%H:%M"))
    return _regular_times_kst(now, early_close=close_et < US_REGULAR_CLOSE_ET)
