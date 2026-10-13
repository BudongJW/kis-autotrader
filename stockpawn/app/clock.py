"""KST 시계. naive ``datetime.now()`` 금지 — 컨테이너 TZ가 UTC면 9시간 밀린다."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


def today_kst() -> date:
    return now_kst().date()


def as_kst(dt: datetime) -> datetime:
    """naive면 KST로 간주, aware면 KST로 변환. SQLite는 tz를 버리므로 읽을 때 정규화한다."""
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)
