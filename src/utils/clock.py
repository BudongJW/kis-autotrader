"""시간대 단일 진실 공급원 — KST 벽시계와 미국장(ET) 시각 변환.

이 프로젝트의 시간 로직은 이중 구조다:
  - GitHub Actions cron은 **UTC**로 해석된다.
  - 워크플로가 `env: TZ=Asia/Seoul`을 설정하므로 실행 중 파이썬의 벽시계는 **KST**다.

여기 있는 헬퍼를 쓰면 `TZ` 환경변수 설정 여부와 무관하게 항상 올바른 시각을 얻는다.
naive `datetime.now()`는 `TZ`가 빠진 환경(로컬 UTC 컨테이너, 새 워크플로에서 `TZ`
누락)에서 조용히 9시간 밀리므로 신규 코드에서는 쓰지 않는다.

미국장 개장/폐장은 **서머타임을 수동 플래그로 관리하지 않는다.** ET 09:30/16:00을
`America/New_York`에서 KST로 변환해 계산하므로 DST 전환일에 자동으로 따라간다.
(2026-10-30 금 22:30 개장 → 2026-11-02 월 23:30 개장)
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

# 미국 정규장 (ET 기준)
US_REGULAR_OPEN_ET = dtime(9, 30)
US_REGULAR_CLOSE_ET = dtime(16, 0)
# 조기 폐장일(추수감사절 다음날·크리스마스 이브 등)의 폐장 시각
US_EARLY_CLOSE_ET = dtime(13, 0)


# ──────────────────────────────────────────────────────────
# 현재 시각
# ──────────────────────────────────────────────────────────

def now_kst() -> datetime:
    """KST 기준 현재 시각 (tz-aware). TZ 환경변수와 무관하게 안전."""
    return datetime.now(KST)


def today_kst() -> date:
    """KST 기준 오늘 날짜."""
    return now_kst().date()


def kst_stamp(now: datetime | None = None) -> str:
    """상태 파일·CSV에 기록할 KST 타임스탬프 문자열.

    **naive 표기**를 유지하는 게 의도적이다: trades.csv·positions.json 등 기존
    기록이 전부 offset 없는 "YYYY-MM-DDTHH:MM:SS"라, 여기서 "+09:00"을 붙이기
    시작하면 날짜 prefix 매칭·외부 저널 소비자와의 호환이 깨진다.
    값은 항상 KST로 정확하고, 읽을 땐 parse_kst()가 tz를 붙여준다.
    """
    return (now or now_kst()).replace(tzinfo=None).isoformat(timespec="seconds")


def parse_kst(value: str | datetime) -> datetime:
    """저장된 타임스탬프를 **aware KST datetime**으로 정규화.

    기록 시점에 따라 naive("...T09:00:00")와 aware("...T09:00:00+09:00")가
    섞여 있다. 정규화 없이 now_kst()와 빼면
    `TypeError: can't subtract offset-naive and offset-aware datetimes`가 난다.
    (tests/test_regression_bugs.py가 이미 한 번 잡은 버그 클래스)
    """
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def now_et() -> datetime:
    """미국 동부 기준 현재 시각 (tz-aware). DST 자동 반영."""
    return datetime.now(ET)


def today_et() -> date:
    """미국 동부 기준 오늘 날짜. 한국 야간 세션 중 KST 날짜와 하루 어긋난다."""
    return now_et().date()


# ──────────────────────────────────────────────────────────
# 미국장 세션
# ──────────────────────────────────────────────────────────

def us_session_date_et(now: datetime | None = None) -> date:
    """지금 진행 중이거나 곧 시작할 미국 정규장 세션의 **ET 날짜**.

    US 세션은 KST로 '월밤~토새벽'에 걸쳐 있어 KST 날짜로는 한 세션이 이틀에
    나뉜다. 세션 단위 상태(재진입 쿨다운·일일 손익 집계 등)는 KST 날짜가 아니라
    이 ET 날짜로 키잉해야 자정을 넘어도 일관된다.

    폐장(16:00 ET) 이후는 다음 세션으로 넘긴다.
    """
    et = (now or now_kst()).astimezone(ET)
    if et.time() >= US_REGULAR_CLOSE_ET:
        return (et + timedelta(days=1)).date()
    return et.date()


def us_market_times_kst(now: datetime | None = None,
                        early_close: bool = False) -> tuple[dtime, dtime]:
    """해당 세션의 (개장, 폐장) 시각을 **KST naive time**으로 반환.

    서머타임 22:30~05:00, 동절기 23:30~06:00이 자동으로 나온다. 반환값이 naive
    `time`인 것은 기존 호출부(자정 넘김 비교 로직)와의 호환 때문이다.

    Args:
        now: 기준 시각 (기본: 지금). 어떤 세션인지 판정하는 데만 쓴다.
        early_close: 조기 폐장일(13:00 ET)이면 True.
    """
    d = us_session_date_et(now)
    close_et = US_EARLY_CLOSE_ET if early_close else US_REGULAR_CLOSE_ET
    open_dt = datetime.combine(d, US_REGULAR_OPEN_ET, tzinfo=ET)
    close_dt = datetime.combine(d, close_et, tzinfo=ET)
    return open_dt.astimezone(KST).time(), close_dt.astimezone(KST).time()


def is_us_dst(now: datetime | None = None) -> bool:
    """해당 세션이 미국 서머타임(EDT) 구간인지."""
    d = us_session_date_et(now)
    return datetime.combine(d, US_REGULAR_OPEN_ET, tzinfo=ET).dst() != timedelta(0)
