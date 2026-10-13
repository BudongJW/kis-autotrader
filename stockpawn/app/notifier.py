"""차주·운영자 알림. MVP는 로그만 남긴다 (알림 원장은 Event 테이블).

운영 시 SMS/알림톡 어댑터로 교체. 마진콜·처분 예정 통지는 금융소비자보호법상
통지 의무와 연결되므로 실제 발송 채널·문구는 법률 검토 후 확정한다.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("stockpawn.notify")


class Notifier(Protocol):
    def notify(self, loan_id: int, kind: str, message: str) -> None: ...


class LogNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str]] = []

    def notify(self, loan_id: int, kind: str, message: str) -> None:
        self.sent.append((loan_id, kind, message))
        log.info("notify loan=%s kind=%s %s", loan_id, kind, message)
