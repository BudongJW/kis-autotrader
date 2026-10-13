from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app import services
from app.deps import get_deps, get_session
from app.schemas import ValuationRead, ValuationRequest

router = APIRouter(prefix="/valuations", tags=["valuations"])


@router.post("", response_model=ValuationRead, summary="담보평가 + 최대대출금 (계약 전 조회)")
def create_valuation(
    body: ValuationRequest,
    session: Session = Depends(get_session),
    deps: services.Deps = Depends(get_deps),
) -> ValuationRead:
    v = services.value_collateral(session, deps, body.code, body.qty)
    session.commit()  # 종목 캐시 갱신
    return ValuationRead.model_validate(v)
