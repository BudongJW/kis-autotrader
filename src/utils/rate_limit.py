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
        """다음 호출 가능 시점까지 대기 후 반환.

        구현 주의 — 예전 버전은 `with self._lock:` 블록 **안에서** sleep 후
        self.acquire()를 재귀 호출했다. threading.Lock은 재진입 불가이므로
        한도를 넘는 순간 같은 스레드가 자기 락을 기다리며 **영구 정지**했다.
        루프가 acquire() 안에서 멈추니 봇의 MAX_LOOP_RUNTIME_SEC 자체 종료도
        발동하지 못하고, GitHub 하드 타임아웃에 강제 종료돼 정리 스텝(거래기록
        업로드)이 통째로 스킵된다.

        그래서 (1) 재귀 대신 루프, (2) sleep은 **락을 놓은 상태에서** 한다.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.window
                while self._calls and self._calls[0] <= cutoff:
                    self._calls.popleft()

                if len(self._calls) < self.rate:
                    self._calls.append(now)
                    return

                # 가장 오래된 호출이 윈도우를 벗어날 때까지 남은 시간
                wait = self._calls[0] + self.window - now

            if wait > 0:
                time.sleep(wait)


rate_limiter = TokenBucketLimiter(rate_per_sec=settings.rate_limit)
