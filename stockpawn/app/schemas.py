"""API 요청/응답 스키마. Decimal은 JSON에서 문자열로 직렬화된다 (정밀도 보존)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.engines.risk import LoanStatus
from app.models import KycStatus

StockCode = Annotated[str, Field(pattern=r"^[0-9A-Z]{6}$", examples=["005930"])]


def _whole_won(v: Decimal) -> Decimal:
    if v <= 0 or v != v.to_integral_value():
        raise ValueError("양의 원 단위 정수여야 합니다")
    return v


WholeWon = Annotated[Decimal, AfterValidator(_whole_won)]


class BorrowerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=9, max_length=20)
    account_no: str = Field(min_length=4, max_length=30, description="질권 설정 대상 증권계좌")
    kyc_status: KycStatus = Field(KycStatus.PENDING, description="외부 KYC 결과 플래그 (주민번호 등은 받지 않음)")


class BorrowerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    phone: str
    account_no: str
    kyc_status: KycStatus
    created_at: datetime


class KycUpdate(BaseModel):
    kyc_status: KycStatus


class ValuationRequest(BaseModel):
    code: StockCode
    qty: int = Field(gt=0)


class ValuationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    as_of: date | None
    price: Decimal
    qty: int
    raw_value: Decimal
    avg_trading_value: Decimal
    liquidity_cap: Decimal
    eligible_value: Decimal
    volatility: Decimal | None
    haircut: Decimal
    tier: str
    ltv: Decimal
    collateral_value: Decimal
    max_loan: Decimal
    eligible: bool
    reasons: list[str]


class CollateralItem(BaseModel):
    code: StockCode
    qty: int = Field(gt=0)


class LoanCreate(BaseModel):
    borrower_id: int
    collaterals: list[CollateralItem] = Field(min_length=1)
    amount: WholeWon = Field(description="대출 신청액 (≤ 최대대출금)")


class RepayRequest(BaseModel):
    amount: WholeWon


class CollateralAddRequest(BaseModel):
    collaterals: list[CollateralItem] = Field(min_length=1)


class LiquidateRequest(BaseModel):
    note: str = Field("", max_length=200)


class StockControl(BaseModel):
    is_blocked: bool | None = Field(None, description="담보 차단 (관리종목·정리매매 등)")
    tier_floor: Literal["A", "B", "C", "X"] | None = Field(None, description="운영자 지정 등급 (보수적 방향만)")
    clear_tier_floor: bool = False


class StockRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    market: str
    tier: str
    is_blocked: bool
    tier_floor: str | None
    haircut: Decimal
    volatility: Decimal | None
    last_price: Decimal | None
    last_reasons: list[str]
    updated_at: datetime


class CollateralRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    stock_code: str
    qty: int
    active_qty: int
    pledge_price: Decimal
    pledge_date: date
    released_qty: int
    liquidated_qty: int


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: datetime
    type: str
    payload: dict


class LiquidationOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    stock_code: str
    qty: int
    mode: str
    expected_price: Decimal
    order_ts: datetime
    broker_order_id: str | None
    fill_price: Decimal | None
    fill_qty: int
    fee: Decimal
    status: str
    message: str


class ValuationSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ts: datetime
    market_value: Decimal
    collateral_value: Decimal
    debt: Decimal
    ratio: Decimal | None
    status_at_ts: LoanStatus


class LoanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    borrower_id: int
    principal: Decimal
    annual_rate: Decimal
    start_date: date
    maturity_date: date
    status: LoanStatus
    outstanding_principal: Decimal
    accrued_interest: Decimal
    repaid_principal: Decimal
    repaid_interest: Decimal
    last_accrued_date: date | None
    last_ratio: Decimal | None
    margin_call_deadline: date | None
    liquidation_reasons: list[str]
    surplus_returned: Decimal
    shortfall: Decimal
    closed_at: datetime | None


class InterestSummary(BaseModel):
    days_accrued: int
    total_charged: Decimal  # 지금까지 부과한 이자 (미수 + 수납)
    balance_days: Decimal  # 적수
    effective_annual_rate: Decimal  # 실효 연이율 (≤ 20%)
    legal_max_interest: Decimal  # 같은 적수에 대한 법정 상한


class LoanDetail(LoanRead):
    borrower_name: str
    payoff_today: Decimal  # 오늘 완제 금액 (원금 잔액 + 어제까지 이자, 수수료 없음)
    collaterals: list[CollateralRead]
    latest_valuation: ValuationSnapshotRead | None
    interest: InterestSummary
    liquidation_orders: list[LiquidationOrderRead]
    events: list[EventRead]


class RepayResult(BaseModel):
    loan: LoanRead
    to_interest: Decimal
    to_principal: Decimal
    remaining_interest: Decimal
    remaining_principal: Decimal
    fully_repaid: bool
