"""KIS API 인증 — OAuth 토큰 발급·캐싱·갱신.

토큰은 24시간 유효. 디스크에 캐싱해 매 실행마다 재발급하지 않도록 한다.

동시성 안전성 (GitHub Actions 두 워크플로가 같은 캐시 공유 시):
1. atomic write — 임시 파일에 쓰고 os.replace로 원자적 교체. 부분 파일 노출 X.
2. 갱신 마진 30분 — 만료 30분 전부터 갱신 가능. 동시 갱신 race 확률 ↓.
3. 발급 직전 캐시 재확인 — file lock으로 직렬화하고, lock 안에서 다시 cache 읽어서
   다른 프로세스가 이미 갱신했으면 새 토큰 발급 안 함.
4. KIS API "1분 내 재발급 금지" 위반 시 캐시 폴백 — 발급 실패해도 기존 토큰으로 진행.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests

from src.config import settings

TOKEN_CACHE_PATH = Path("logs/.kis_token_cache.json")
TOKEN_LOCK_PATH = Path("logs/.kis_token_cache.lock")
TOKEN_REFRESH_MARGIN_SEC = 1800  # 만료 30분 전엔 미리 갱신 (race 마진 ↑)
TOKEN_LOCK_TIMEOUT_SEC = 30


@dataclass
class TokenBundle:
    access_token: str
    expires_at: datetime
    mode: str  # "paper" or "live"

    @property
    def is_valid(self) -> bool:
        return datetime.now() < self.expires_at - timedelta(seconds=TOKEN_REFRESH_MARGIN_SEC)

    @property
    def is_usable(self) -> bool:
        """만료까지 30초 이상 남았으면 일단 쓸 수 있음 (race 시 폴백용)."""
        return datetime.now() < self.expires_at - timedelta(seconds=30)

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "expires_at": self.expires_at.isoformat(),
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TokenBundle:
        return cls(
            access_token=d["access_token"],
            expires_at=datetime.fromisoformat(d["expires_at"]),
            mode=d["mode"],
        )


def _load_cached_token(*, allow_expiring: bool = False) -> TokenBundle | None:
    """캐시 토큰 로드.

    Args:
        allow_expiring: True면 갱신 마진 무시하고 "사용 가능"하면 반환 (race 폴백용).
    """
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        with TOKEN_CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        bundle = TokenBundle.from_dict(data)
        if bundle.mode != settings.mode.value:
            return None  # 모드가 바뀌면 무효
        if allow_expiring:
            return bundle if bundle.is_usable else None
        if not bundle.is_valid:
            return None
        return bundle
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _save_cached_token(bundle: TokenBundle) -> None:
    """원자적 쓰기 — 임시 파일 작성 후 os.replace로 교체.

    부분 파일이 다른 프로세스에 노출되지 않음. POSIX/Windows 모두 atomic.
    """
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".kis_token_cache.tmp.",
        dir=str(TOKEN_CACHE_PATH.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bundle.to_dict(), f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # Windows tmpfile fsync 실패 무시
        os.replace(tmp_path, TOKEN_CACHE_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _acquire_file_lock(timeout_sec: int = TOKEN_LOCK_TIMEOUT_SEC):
    """간이 file lock (filelock 패키지 없이 cross-platform).

    원자적 O_CREAT|O_EXCL로 락 파일 생성. 폴링으로 재시도.
    같은 머신 안에서만 유효 (GitHub Actions 한 run 안 또는 한 PC 안).
    """
    TOKEN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_sec
    while True:
        try:
            fd = os.open(str(TOKEN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() > deadline:
                # stale lock 추정 (60초 이상 된 lock은 강제 제거)
                try:
                    age = time.time() - TOKEN_LOCK_PATH.stat().st_mtime
                    if age > 60:
                        TOKEN_LOCK_PATH.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                return False
            time.sleep(0.5)


def _release_file_lock() -> None:
    try:
        TOKEN_LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _request_new_token() -> TokenBundle:
    settings.validate_runtime()

    url = f"{settings.base_url}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": settings.appkey,
        "appsecret": settings.appsecret,
    }
    headers = {"Content-Type": "application/json"}

    resp = requests.post(url, json=body, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(
            f"KIS 토큰 발급 실패 (status={resp.status_code}): {resp.text}"
        )

    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"KIS 토큰 응답 이상: {data}")

    expires_in_sec = int(data.get("expires_in", 86400))
    return TokenBundle(
        access_token=data["access_token"],
        expires_at=datetime.now() + timedelta(seconds=expires_in_sec),
        mode=settings.mode.value,
    )


def get_token(force_refresh: bool = False) -> TokenBundle:
    """유효한 토큰을 반환. 캐시가 있고 유효하면 그대로, 아니면 재발급.

    동시성 처리:
    1. lock 없이 먼저 캐시 시도 (hot path 빠르게)
    2. 캐시 invalid면 file lock 잡고
    3. lock 안에서 캐시 다시 체크 (다른 프로세스가 갱신했으면 그것 사용)
    4. 그래도 invalid면 KIS API 호출
    5. KIS API 실패 시 만료 안 된 캐시로 폴백 ("1분 내 재발급 금지" 대응)
    """
    # 1) Fast path: lock 없이 캐시 hit 시 즉시 반환
    if not force_refresh:
        cached = _load_cached_token()
        if cached:
            return cached

    # 2) 캐시 miss/expired → lock 잡고 직렬화
    lock_held = _acquire_file_lock()
    try:
        # 3) Lock 안에서 캐시 재확인 — race 시 다른 프로세스가 갱신했을 수 있음
        if not force_refresh:
            cached = _load_cached_token()
            if cached:
                return cached

        # 4) 진짜 발급
        try:
            bundle = _request_new_token()
            _save_cached_token(bundle)
            return bundle
        except RuntimeError as exc:
            # 5) KIS API 실패 ("1분 내 재발급" 등) → 만료 안 된 캐시 폴백
            fallback = _load_cached_token(allow_expiring=True)
            if fallback:
                return fallback
            raise
    finally:
        if lock_held:
            _release_file_lock()


def auth_headers(
    tr_id: str,
    *,
    custtype: str = "P",  # P=개인, B=법인
) -> dict[str, str]:
    """KIS REST 호출에 필요한 표준 헤더 묶음을 생성."""
    token = get_token()
    return {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token.access_token}",
        "appkey": settings.appkey,
        "appsecret": settings.appsecret,
        "tr_id": tr_id,
        "custtype": custtype,
    }
