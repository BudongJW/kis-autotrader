"""SQLModel 테이블 (스펙 3장).

금액·비율 컬럼은 ``DecimalType``: SQLite에선 TEXT로 저장해 Decimal을 정확히 왕복시키고
(SQLAlchemy Numeric은 SQLite에서 float를 경유한다), PostgreSQL에선 NUMERIC을 쓴다.
SQL 레벨 숫자 비교·집계는 하지 않는다 — 집계는 파이썬 Decimal로.

개인정보(주민번호 등)는 저장하지 않는다. KYC는 외부 인증 결과 플래그만.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Numeric, String
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from app.clock import as_kst, now_kst
from app.engines.risk import LoanStatus


class DecimalType(TypeDecorator):
    impl = Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(40))
        return dialect.type_descriptor(Numeric(24, 6))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("float 금액은 저장할 수 없습니다 — Decimal을 쓰세요")
        value = Decimal(value)
        return str(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value: Any, dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value)) if not isinstance(value, Decimal) else value


class KSTDateTime(TypeDecorator):
    """항상 KST aware datetime으로 왕복. SQLite엔 KST 벽시계(naive)로 저장 — SQLAlchemy 버전마다
    aware 값 처리(UTC 변환/ tz 버림)가 달라 직접 고정한다. PostgreSQL은 timestamptz."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=dialect.name != "sqlite"))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        value = as_kst(value)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return None if value is None else as_kst(value)


def _money(**kw: Any) -> Any:
    kw.setdefault("default", Decimal("0"))
    return Field(sa_type=DecimalType, **kw)


def _ts(**kw: Any) -> Any:
    return Field(sa_type=KSTDateTime, **kw)


class KycStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class EventType(StrEnum):
    LOAN_CREATED = "LOAN_CREATED"
    COLLATERAL_PLEDGED = "COLLATERAL_PLEDGED"
    DISBURSED = "DISBURSED"
    COLLATERAL_ADDED = "COLLATERAL_ADDED"
    REPAY = "REPAY"
    MARGIN_CALL = "MARGIN_CALL"
    MARGIN_CALL_CURED = "MARGIN_CALL_CURED"
    MATURITY_NOTICE = "MATURITY_NOTICE"
    TIER_CHANGED = "TIER_CHANGED"
    LIQUIDATION_PENDING = "LIQUIDATION_PENDING"
    LIQ_ORDER = "LIQ_ORDER"
    FILL = "FILL"
    ORDER_FAILED = "ORDER_FAILED"
    SURPLUS_RETURNED = "SURPLUS_RETURNED"
    SHORTFALL_RECORDED = "SHORTFALL_RECORDED"
    COLLATERAL_RELEASED = "COLLATERAL_RELEASED"
    CLOSED = "CLOSED"


class Borrower(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    phone: str
    account_no: str  # 질권 설정 대상 증권계좌
    kyc_status: KycStatus = KycStatus.PENDING  # 외부 KYC 결과 플래그만 저장
    created_at: datetime = _ts(default_factory=now_kst)


class Stock(SQLModel, table=True):
    """종목 마스터 + 마지막 평가 캐시. 장중 10분 평가는 이 캐시(등급·헤어컷)를 쓰고
    전체 재평가(변동성·유동성·등급)는 장 마감 후 1회 돌린다."""

    code: str = Field(primary_key=True, max_length=12)
    name: str = ""
    market: str = "UNKNOWN"
    tier: str = "X"
    is_blocked: bool = False  # 운영자 차단 → tier X (관리종목 등 pykrx로 못 잡는 사유)
    tier_floor: str | None = None  # 운영자 지정 등급 (보수적 방향으로만 반영)
    haircut: Decimal = _money(default=Decimal("0.5"))
    volatility: Decimal | None = _money(default=None, nullable=True)
    last_price: Decimal | None = _money(default=None, nullable=True)
    last_reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    updated_at: datetime = _ts(default_factory=now_kst)


class Loan(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    borrower_id: int = Field(foreign_key="borrower.id", index=True)
    principal: Decimal = _money()  # 최초 대출원금 (선이자 공제 없이 전액 송금)
    annual_rate: Decimal = _money()
    start_date: date
    maturity_date: date
    status: LoanStatus = Field(default=LoanStatus.NORMAL, index=True)
    accrued_interest: Decimal = _money()  # 미수이자 (적립됐으나 미납)
    repaid_principal: Decimal = _money()
    repaid_interest: Decimal = _money()
    closed_at: datetime | None = _ts(default=None, nullable=True)

    # ── 구현 필드 ──
    balance_days: Decimal = _money()  # 적수 = Σ 일별 원금잔액 (이자 = floor(적수 × 연이율 / 365))
    last_accrued_date: date | None = None  # 이자 적립이 끝난 마지막 날
    last_ratio: Decimal | None = _money(default=None, nullable=True)
    margin_call_at: datetime | None = _ts(default=None, nullable=True)
    margin_call_deadline: date | None = None  # 이날 장 마감까지 유예
    liquidation_reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    liquidation_pending_at: datetime | None = _ts(default=None, nullable=True)
    surplus_returned: Decimal = _money()  # 처분 후 차주에게 반환한 잉여금
    shortfall: Decimal = _money()  # 처분 후 남은 미수채권
    created_at: datetime = _ts(default_factory=now_kst)

    @property
    def outstanding_principal(self) -> Decimal:
        return self.principal - self.repaid_principal

    @property
    def debt(self) -> Decimal:
        return self.outstanding_principal + self.accrued_interest


class Collateral(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    loan_id: int = Field(foreign_key="loan.id", index=True)
    stock_code: str = Field(foreign_key="stock.code", index=True)
    qty: int  # 질권 설정 수량
    pledge_price: Decimal = _money()  # 질권 설정 시 기준가
    pledge_date: date
    released_qty: int = 0  # 질권 해제(차주 반환) 수량
    liquidated_qty: int = 0  # 처분(매도) 수량

    @property
    def active_qty(self) -> int:
        return self.qty - self.released_qty - self.liquidated_qty


class Valuation(SQLModel, table=True):
    """담보비율 평가 스냅샷 (장중 10분 + 장후 1회)."""

    id: int | None = Field(default=None, primary_key=True)
    loan_id: int = Field(foreign_key="loan.id", index=True)
    ts: datetime = _ts(default_factory=now_kst)
    market_value: Decimal = _money()  # 헤어컷 미적용 시가
    collateral_value: Decimal = _money()  # 헤어컷 적용 평가액 (참고용)
    debt: Decimal = _money()
    ratio: Decimal | None = _money(default=None, nullable=True)
    status_at_ts: LoanStatus


class InterestLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    loan_id: int = Field(foreign_key="loan.id", index=True)
    date: date
    daily_interest: Decimal = _money()
    principal_base: Decimal = _money()  # 그날 이자 기준 원금 잔액 (미수이자 미포함 = 단리)


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    loan_id: int | None = Field(default=None, foreign_key="loan.id", index=True)
    ts: datetime = _ts(default_factory=now_kst)
    type: EventType = Field(index=True)
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))


class LiquidationOrder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    loan_id: int = Field(foreign_key="loan.id", index=True)
    stock_code: str
    qty: int
    mode: str  # PARTIAL | PAYOFF | FULL
    expected_price: Decimal = _money()
    order_ts: datetime = _ts(default_factory=now_kst)
    broker_order_id: str | None = None
    fill_price: Decimal | None = _money(default=None, nullable=True)
    fill_qty: int = 0
    fee: Decimal = _money()
    status: str = "CREATED"  # CREATED | FILLED | PARTIAL | REJECTED
    message: str = ""
