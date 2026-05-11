"""Rate limit 단위 테스트 — 초당 N건 한도 보장."""

from __future__ import annotations

import time

from src.utils.rate_limit import TokenBucketLimiter


def test_under_limit_no_wait():
    limiter = TokenBucketLimiter(rate_per_sec=5)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1  # 5건 이내는 즉시


def test_over_limit_waits():
    limiter = TokenBucketLimiter(rate_per_sec=2, window_sec=1.0)
    start = time.monotonic()
    for _ in range(3):  # 3번째는 1초 이상 대기해야 함
        limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.9
