"""KIS 토큰 캐시 동시성 안전성 회귀 테스트.

- atomic write: 임시 파일 → os.replace
- file lock: 상호 배제
- 갱신 마진: 만료 30분 전엔 미리 갱신
- expiring 폴백: KIS API 실패 시 만료 직전 토큰도 사용
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_token_cache(tmp_path, monkeypatch):
    """각 테스트마다 별도 캐시 경로 사용. 실제 .env에 영향 없음."""
    import src.kis_auth as auth

    cache_path = tmp_path / "tok.json"
    lock_path = tmp_path / "tok.lock"
    monkeypatch.setattr(auth, "TOKEN_CACHE_PATH", cache_path)
    monkeypatch.setattr(auth, "TOKEN_LOCK_PATH", lock_path)
    yield


def test_atomic_write_roundtrip():
    """atomic write로 저장한 토큰을 정상 로드."""
    from src.config import settings
    from src.kis_auth import TokenBundle, _save_cached_token, _load_cached_token

    b = TokenBundle(
        access_token="abc123",
        expires_at=datetime.now() + timedelta(hours=20),
        mode=settings.mode.value,
    )
    _save_cached_token(b)
    c = _load_cached_token()
    assert c is not None
    assert c.access_token == "abc123"


def test_load_returns_none_when_below_refresh_margin():
    """만료 30분 이내면 is_valid=False → 로드 시 None."""
    from src.config import settings
    from src.kis_auth import TokenBundle, _save_cached_token, _load_cached_token

    # 만료 15분 후 → 갱신 마진(30분) 안에 들어감
    b = TokenBundle(
        access_token="expiring",
        expires_at=datetime.now() + timedelta(minutes=15),
        mode=settings.mode.value,
    )
    _save_cached_token(b)
    assert _load_cached_token() is None


def test_allow_expiring_fallback():
    """allow_expiring=True면 갱신 마진 무시하고 30초 이상 남은 토큰 반환."""
    from src.config import settings
    from src.kis_auth import TokenBundle, _save_cached_token, _load_cached_token

    b = TokenBundle(
        access_token="last_chance",
        expires_at=datetime.now() + timedelta(minutes=10),
        mode=settings.mode.value,
    )
    _save_cached_token(b)
    c = _load_cached_token(allow_expiring=True)
    assert c is not None
    assert c.access_token == "last_chance"


def test_file_lock_mutex():
    """첫 lock 잡으면 두 번째는 대기 후 fail."""
    from src.kis_auth import _acquire_file_lock, _release_file_lock

    assert _acquire_file_lock(timeout_sec=1) is True
    try:
        assert _acquire_file_lock(timeout_sec=1) is False
    finally:
        _release_file_lock()
    # release 후엔 재획득 가능
    assert _acquire_file_lock(timeout_sec=1) is True
    _release_file_lock()


def test_file_lock_concurrent_threads():
    """두 스레드가 동시에 lock 잡으려 해도 한 번에 하나만 성공."""
    from src.kis_auth import _acquire_file_lock, _release_file_lock

    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()  # 두 스레드 동시 시작 보장
        acquired = _acquire_file_lock(timeout_sec=2)
        results.append(acquired)
        if acquired:
            # 짧게 잡고 풀어줌
            import time as _t
            _t.sleep(0.3)
            _release_file_lock()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 둘 다 결국 성공해야 (한 번에 하나씩)
    assert len(results) == 2
    assert all(results), f"both should eventually acquire, got {results}"


def test_atomic_write_no_partial_file(tmp_path, monkeypatch):
    """동시 쓰기 중 다른 reader가 partial JSON을 못 보게 — tmp file은 다른 이름."""
    from src.config import settings
    from src.kis_auth import TOKEN_CACHE_PATH, TokenBundle, _save_cached_token, _load_cached_token

    b = TokenBundle(
        access_token="atomic",
        expires_at=datetime.now() + timedelta(hours=20),
        mode=settings.mode.value,
    )
    _save_cached_token(b)
    # 쓰기 완료 후 디렉토리에 tmp 파일이 남지 않아야 함
    cache_dir = TOKEN_CACHE_PATH.parent
    leftovers = [p for p in cache_dir.iterdir() if p.name.startswith(".kis_token_cache.tmp.")]
    assert leftovers == [], f"tmp files leaked: {leftovers}"

    # 최종 파일은 valid JSON
    with TOKEN_CACHE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["access_token"] == "atomic"


def test_mode_mismatch_returns_none():
    """캐시된 토큰의 mode가 settings와 다르면 None (paper/live 혼동 방지)."""
    from src.config import settings
    from src.kis_auth import TOKEN_CACHE_PATH, TokenBundle, _save_cached_token, _load_cached_token

    other_mode = "live" if settings.mode.value == "paper" else "paper"
    b = TokenBundle(
        access_token="wrong_mode",
        expires_at=datetime.now() + timedelta(hours=20),
        mode=other_mode,
    )
    _save_cached_token(b)
    assert _load_cached_token() is None
