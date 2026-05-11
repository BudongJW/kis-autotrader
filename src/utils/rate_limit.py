"""KIS API rate limit 회피 — 토큰 버킷.

실전 초당 20건, 모의 초당 2건이 한도. 안전 마진 적용 후 사용.
초과 시 5분 쿨다운이 발생할 수 있어 무조건 회피.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from src.config import settings


class TokenBucketLimiter:
    """초당 N건 한도를 보장하는 sliding-window 리미터."""

    def __init__(self, rate_per_sec: int, window_sec: float = 1.0) -> None:
        self.rate = rate_per_sec
        self.window = window_sec
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """다음 호출 가능 시점까지 대기 후 반환."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()

            if len(self._calls) >= self.rate:
                wait = self._calls[0] + self.window - now
                if wait > 0:
                    time.sleep(wait)
                    return self.acquire()

            self._calls.append(time.monotonic())


rate_limiter = TokenBucketLimiter(rate_per_sec=settings.rate_limit)
