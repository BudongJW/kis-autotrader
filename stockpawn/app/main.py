"""FastAPI 앱 — 라우터 등록, DB 초기화, 스케줄러 시작.

실행: uvicorn app.main:app --env-file .env   (대시보드: http://localhost:8000/ , API 문서: /docs)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine

from app import config
from app.db import init_db, make_engine
from app.deps import build_default_deps, require_api_key
from app.routers import admin, borrowers, dashboard, loans, valuations
from app.scheduler import build_scheduler
from app.services import Deps, ServiceError

log = logging.getLogger("stockpawn")


def create_app(engine: Engine | None = None, deps: Deps | None = None,
               scheduler_enabled: bool = config.SCHEDULER_ENABLED) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.engine is None:
            app.state.engine = make_engine()
        if app.state.deps is None:
            app.state.deps = build_default_deps()
        init_db(app.state.engine)
        scheduler = None
        if scheduler_enabled:
            scheduler = build_scheduler(app.state.engine, app.state.deps)
            scheduler.start()
            log.info("scheduler started: %s", [j.id for j in scheduler.get_jobs()])
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)

    app = FastAPI(
        title="StockPawn — 주식 담보대출 백오피스 MVP",
        version="0.1.0",
        description="담보평가 → 대출 → 이자(연 20% 단리) → 담보비율 모니터링 → 처분(Mock) 자동화",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.deps = deps

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "reasons": exc.reasons})

    protected = [Depends(require_api_key)]
    for r in (borrowers.router, valuations.router, loans.router, dashboard.router, admin.router):
        app.include_router(r, dependencies=protected)
    app.include_router(dashboard.page_router)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
