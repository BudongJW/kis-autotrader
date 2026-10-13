from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlmodel import Session

from app import services
from app.deps import get_deps, get_session

router = APIRouter(tags=["dashboard"])
page_router = APIRouter()  # 정적 HTML — 데이터가 없으므로 API 키 없이 제공, 데이터는 JS가 키를 붙여 조회

_HTML = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


@router.get("/dashboard", summary="전체 대출 현황, 위험 등급별 집계")
def dashboard(session: Session = Depends(get_session), deps: services.Deps = Depends(get_deps)) -> dict:
    return services.dashboard(session, deps)


@page_router.get("/", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return FileResponse(_HTML, media_type="text/html")
