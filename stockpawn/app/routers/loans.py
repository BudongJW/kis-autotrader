from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlmodel import Session, desc, select

from app import services
from app.deps import get_deps, get_session
from app.engines import interest
from app.engines.risk import LoanStatus
from app.models import Borrower, Collateral, Event, LiquidationOrder, Loan, Valuation
from app.schemas import (
    CollateralAddRequest,
    LiquidateRequest,
    LoanCreate,
    LoanDetail,
    LoanRead,
    RepayRequest,
    RepayResult,
)

router = APIRouter(prefix="/loans", tags=["loans"])


def loan_detail(session: Session, deps: services.Deps, loan: Loan) -> LoanDetail:
    borrower = session.get(Borrower, loan.borrower_id)
    collaterals = session.exec(select(Collateral).where(Collateral.loan_id == loan.id).order_by(Collateral.id)).all()
    events = session.exec(select(Event).where(Event.loan_id == loan.id).order_by(Event.id)).all()
    orders = session.exec(
        select(LiquidationOrder).where(LiquidationOrder.loan_id == loan.id).order_by(LiquidationOrder.id)
    ).all()
    latest = session.exec(
        select(Valuation).where(Valuation.loan_id == loan.id).order_by(desc(Valuation.id))
    ).first()
    charged = loan.accrued_interest + loan.repaid_interest
    days = 0
    if loan.last_accrued_date is not None:
        days = max(0, (loan.last_accrued_date - (loan.start_date - timedelta(days=1))).days)
    return LoanDetail.model_validate({
        **LoanRead.model_validate(loan).model_dump(),
        "borrower_name": borrower.name if borrower else "",
        "payoff_today": services.payoff_today(loan, deps) if loan.status != LoanStatus.CLOSED else 0,
        "collaterals": list(collaterals),
        "latest_valuation": latest,
        "interest": {
            "days_accrued": days,
            "total_charged": charged,
            "balance_days": loan.balance_days,
            "effective_annual_rate": interest.effective_annual_rate(charged, loan.balance_days).quantize(
                Decimal("0.000001")),
            "legal_max_interest": interest.legal_max_interest(loan.balance_days),
        },
        "liquidation_orders": list(orders),
        "events": list(events),
    })


@router.post("", response_model=LoanDetail, status_code=201, summary="대출 계약 생성 (담보 질권 + Mock 송금)")
def create_loan(body: LoanCreate, session: Session = Depends(get_session),
                deps: services.Deps = Depends(get_deps)) -> LoanDetail:
    loan = services.create_loan(session, deps, body.borrower_id,
                                [(c.code, c.qty) for c in body.collaterals], body.amount)
    return loan_detail(session, deps, loan)


@router.get("", response_model=list[LoanRead])
def list_loans(status: LoanStatus | None = None, session: Session = Depends(get_session)) -> list[Loan]:
    q = select(Loan).order_by(Loan.id)
    if status is not None:
        q = q.where(Loan.status == status)
    return list(session.exec(q).all())


@router.get("/{loan_id}", response_model=LoanDetail, summary="상태·원금·미수이자·담보비율·이벤트")
def get_loan(loan_id: int, session: Session = Depends(get_session),
             deps: services.Deps = Depends(get_deps)) -> LoanDetail:
    return loan_detail(session, deps, services.get_loan(session, loan_id))


@router.post("/{loan_id}/repay", response_model=RepayResult, summary="상환 (일할 이자 정산 → 이자 → 원금)")
def repay(loan_id: int, body: RepayRequest, session: Session = Depends(get_session),
          deps: services.Deps = Depends(get_deps)) -> RepayResult:
    loan, alloc = services.repay(session, deps, loan_id, body.amount)
    return RepayResult(
        loan=LoanRead.model_validate(loan),
        to_interest=alloc.to_interest,
        to_principal=alloc.to_principal,
        remaining_interest=alloc.remaining_interest,
        remaining_principal=alloc.remaining_principal,
        fully_repaid=alloc.fully_repaid,
    )


@router.post("/{loan_id}/collateral", response_model=LoanDetail, summary="추가 담보 제공")
def add_collateral(loan_id: int, body: CollateralAddRequest, session: Session = Depends(get_session),
                   deps: services.Deps = Depends(get_deps)) -> LoanDetail:
    loan = services.add_collateral(session, deps, loan_id, [(c.code, c.qty) for c in body.collaterals])
    return loan_detail(session, deps, loan)


@router.post("/{loan_id}/liquidate", response_model=LoanDetail, summary="수동 처분 트리거 (운영자)")
def liquidate(loan_id: int, body: LiquidateRequest | None = None, session: Session = Depends(get_session),
              deps: services.Deps = Depends(get_deps)) -> LoanDetail:
    loan = services.trigger_manual_liquidation(session, deps, loan_id, body.note if body else "")
    return loan_detail(session, deps, loan)
