"""KIS API 인증 — OAuth 토큰 발급·캐싱·갱신.

토큰은 24시간 유효. 디스크에 캐싱해 매 실행마다 재발급하지 않도록 한다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests

from src.config import settings

TOKEN_CACHE_PATH = Path("logs/.kis_token_cache.json")
TOKEN_REFRESH_MARGIN_SEC = 600  # 만료 10분 전엔 미리 갱신


@dataclass
class TokenBundle:
    access_token: str
    expires_at: datetime
    mode: str  # "paper" or "live"

    @property
    def is_valid(self) -> bool:
        return datetime.now() < self.expires_at - timedelta(seconds=TOKEN_REFRESH_MARGIN_SEC)

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


def _load_cached_token() -> TokenBundle | None:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        with TOKEN_CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        bundle = TokenBundle.from_dict(data)
        if bundle.mode != settings.mode.value:
            return None  # 모드가 바뀌면 무효
        if not bundle.is_valid:
            return None
        return bundle
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _save_cached_token(bundle: TokenBundle) -> None:
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TOKEN_CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(bundle.to_dict(), f)


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
    """유효한 토큰을 반환. 캐시가 있고 유효하면 그대로, 아니면 재발급."""
    if not force_refresh:
        cached = _load_cached_token()
        if cached:
            return cached

    bundle = _request_new_token()
    _save_cached_token(bundle)
    return bundle


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
