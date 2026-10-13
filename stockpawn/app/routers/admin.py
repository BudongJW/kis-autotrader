"""운영자·테스트용 수동 실행 엔드포인트."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app import services
from app.deps import get_deps, get_session
from app.models import Stock
from app.schemas import StockControl, StockRead

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/run-valuation", summary="담보비율 평가 수동 실행 (refresh_tiers=true면 등급 재산정 포함)")
def run_valuation(refresh_tiers: bool = False, session: Session = Depends(get_session),
                  deps: services.Deps = Depends(get_deps)) -> dict:
    return services.evaluate_all(session, deps, refresh_tiers=refresh_tiers)


@router.post("/run-interest", summary="이자 적립 수동 실행 (기본: 어제까지)")
def run_interest(through: date | None = None, session: Session = Depends(get_session),
                 deps: services.Deps = Depends(get_deps)) -> dict:
    if through is not None and through >= deps.today():
        raise services.Rejected("이자는 어제까지만 적립할 수 있습니다 (당일분은 한편넣기로 미포함)")
    return services.accrue_all(session, deps, through)


@router.post("/run-liquidation", summary="LIQUIDATION_PENDING 대출 처분 주문 수동 실행")
def run_liquidation(session: Session = Depends(get_session), deps: services.Deps = Depends(get_deps)) -> dict:
    return services.liquidate_pending(session, deps)


@router.get("/stocks", response_model=list[StockRead])
def list_stocks(session: Session = Depends(get_session)) -> list[Stock]:
    return list(session.exec(select(Stock).order_by(Stock.code)).all())


@router.put("/stocks/{code}", response_model=StockRead, summary="종목 담보 차단 / 등급 상한 지정")
def control_stock(code: str, body: StockControl, session: Session = Depends(get_session)) -> Stock:
    return services.update_stock_controls(session, code, body.is_blocked, body.tier_floor, body.clear_tier_floor)
