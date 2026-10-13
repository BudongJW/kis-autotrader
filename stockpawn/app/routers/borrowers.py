from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import services
from app.deps import get_session
from app.models import Borrower
from app.schemas import BorrowerCreate, BorrowerRead, KycUpdate

router = APIRouter(prefix="/borrowers", tags=["borrowers"])


@router.post("", response_model=BorrowerRead, status_code=201, summary="차주 등록")
def create_borrower(body: BorrowerCreate, session: Session = Depends(get_session)) -> Borrower:
    return services.create_borrower(session, body.name, body.phone, body.account_no, body.kyc_status)


@router.get("", response_model=list[BorrowerRead])
def list_borrowers(session: Session = Depends(get_session)) -> list[Borrower]:
    return list(session.exec(select(Borrower).order_by(Borrower.id)).all())


@router.get("/{borrower_id}", response_model=BorrowerRead)
def get_borrower(borrower_id: int, session: Session = Depends(get_session)) -> Borrower:
    b = session.get(Borrower, borrower_id)
    if b is None:
        raise HTTPException(404, f"차주 {borrower_id} 없음")
    return b


@router.patch("/{borrower_id}/kyc", response_model=BorrowerRead, summary="외부 KYC 결과 반영")
def update_kyc(borrower_id: int, body: KycUpdate, session: Session = Depends(get_session)) -> Borrower:
    return services.set_kyc(session, borrower_id, body.kyc_status)
