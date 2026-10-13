"""FastAPI 의존성 + 기본 어댑터 조립. 테스트는 create_app(engine=..., deps=...)로 갈아끼운다."""

from __future__ import annotations

import hmac
import os
from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlmodel import Session

from app import config
from app.adapters.broker import KISBroker, MockBroker
from app.adapters.price import KISSource, PykrxSource
from app.business_days import default_calendar
from app.clock import now_kst, today_kst
from app.services import Deps


def build_default_deps() -> Deps:
    prices = PykrxSource() if config.PRICE_SOURCE == "pykrx" else KISSource()
    # BROKER=mock 기본. kis는 ENABLE_LIVE_ORDERS=true 없이는 생성 단계에서 RuntimeError.
    broker = MockBroker(prices, today=today_kst) if config.BROKER == "mock" else KISBroker()
    return Deps(prices=prices, broker=broker, calendar=default_calendar(), clock=now_kst)


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


def get_deps(request: Request) -> Deps:
    return request.app.state.deps


def require_api_key(request: Request) -> None:
    """API_KEY 환경변수가 설정돼 있으면 모든 API에 X-API-Key 헤더 요구 (차주 개인정보 보호)."""
    expected = os.getenv("API_KEY", "")
    if not expected:
        return
    given = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="X-API-Key 필요")
