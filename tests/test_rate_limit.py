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


def test_over_limit_does_not_deadlock():
    """한도 초과 시 영구 정지하지 않는다 (회귀 방지).

    예전 구현은 `with self._lock:` 안에서 sleep 후 self.acquire()를 재귀
    호출했다. threading.Lock은 재진입 불가라 한도를 넘는 순간 같은 스레드가
    자기 락을 기다리며 멈췄다. 루프가 acquire() 안에서 정지하니 봇의
    MAX_LOOP_RUNTIME_SEC 자체 종료도 발동하지 못하고, GitHub 하드 타임아웃에
    강제 종료돼 거래기록 업로드가 스킵된다.
    """
    import threading

    limiter = TokenBucketLimiter(rate_per_sec=2, window_sec=0.3)
    done = threading.Event()

    def run():
        for _ in range(6):        # 한도를 여러 번 넘긴다
            limiter.acquire()
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(timeout=10), "acquire()가 한도 초과 시 정지했다 (데드락)"


def test_rate_is_actually_enforced():
    """윈도우당 호출 수가 한도를 넘지 않는지 실제로 검증."""
    limiter = TokenBucketLimiter(rate_per_sec=3, window_sec=0.5)
    stamps = []
    for _ in range(9):
        limiter.acquire()
        stamps.append(time.monotonic())

    # 임의의 0.5초 윈도우 안에 4건 이상이 들어가면 안 된다
    for i in range(len(stamps)):
        in_window = [s for s in stamps if stamps[i] <= s < stamps[i] + 0.5]
        assert len(in_window) <= 3, f"윈도우 내 {len(in_window)}건 (한도 3)"


def test_concurrent_threads_respect_limit():
    """여러 스레드가 동시에 써도 한도를 지키고, sleep이 락을 잡고 있지 않다."""
    import threading

    limiter = TokenBucketLimiter(rate_per_sec=4, window_sec=0.4)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker():
        for _ in range(4):
            limiter.acquire()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "스레드가 정지했다 (락 보유 중 sleep 의심)"

    assert len(stamps) == 16
    stamps.sort()
    for i in range(len(stamps)):
        in_window = [s for s in stamps if stamps[i] <= s < stamps[i] + 0.4]
        assert len(in_window) <= 4, f"윈도우 내 {len(in_window)}건 (한도 4)"
    # 16건 / 4건당 0.4초 → 최소 1.2초는 걸려야 한다 (실제로 제한이 걸렸는지)
    assert time.monotonic() - start >= 1.2
