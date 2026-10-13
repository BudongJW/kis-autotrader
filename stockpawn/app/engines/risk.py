"""담보비율 모니터링·상태 전이 (스펙 2.3) — 순수 함수.

collateral_ratio = 담보 시가(헤어컷 미적용) / (원금 잔액 + 미수이자)

  NORMAL              ratio ≥ 140%
  MARGIN_CALL         120% ≤ ratio < 140% → 알림, 유예 2영업일 (만료 = T+2 영업일 장 마감)
  LIQUIDATION_PENDING ratio < 120% | 마진콜 유예 만료 | 만기+7일 경과 | 담보종목 tier X
  LIQUIDATING         처분 주문 생성 (다음 거래일 장 시작 동시호가) — 처분 엔진이 전이
  CLOSED              원리금 회수 완료 — 상환/정산 로직이 전이

LIQUIDATION_PENDING은 **끈적한(sticky) 상태**다. 한 번 처분 사유가 확정되면 일시 반등으로
되돌리지 않는다(장중 시세 출렁임에 따른 상태 왕복 방지). 벗어나는 길은 완제(CLOSED)와
처분 진행(LIQUIDATING)뿐이다. 마진콜 상태에선 추가담보·상환으로 140%를 회복하면 NORMAL 복귀.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from app import config
from app.business_days import TradingCalendar
from app.clock import KST, as_kst

_RATIO_Q = Decimal("0.0001")


class LoanStatus(StrEnum):
    NORMAL = "NORMAL"
    MARGIN_CALL = "MARGIN_CALL"
    LIQUIDATION_PENDING = "LIQUIDATION_PENDING"
    LIQUIDATING = "LIQUIDATING"
    CLOSED = "CLOSED"


ACTIVE_STATUSES = (LoanStatus.NORMAL, LoanStatus.MARGIN_CALL)


class LiquidationReason(StrEnum):
    RATIO_BREACH = "RATIO_BREACH"  # 담보비율 < 120%
    MARGIN_CALL_EXPIRED = "MARGIN_CALL_EXPIRED"  # 마진콜 유예 만료
    MATURITY_EXPIRED = "MATURITY_EXPIRED"  # 만기 + 7일 경과
    INELIGIBLE_COLLATERAL = "INELIGIBLE_COLLATERAL"  # 담보종목 tier X
    MANUAL = "MANUAL"  # 운영자 수동 트리거


@dataclass(frozen=True)
class RiskInput:
    status: LoanStatus
    market_value: Decimal  # 담보 시가 합계 (헤어컷 미적용)
    debt: Decimal  # 원금 잔액 + 미수이자
    maturity_date: date
    margin_call_deadline: date | None = None
    has_ineligible_collateral: bool = False


@dataclass(frozen=True)
class RiskDecision:
    status: LoanStatus
    ratio: Decimal | None  # 표시용(소수 4자리 절사)
    margin_call_deadline: date | None
    liquidation_reasons: tuple[LiquidationReason, ...] = ()
    message: str = ""


def collateral_ratio(market_value: Decimal, debt: Decimal) -> Decimal | None:
    """담보비율(배수). 채무가 없으면 None."""
    if debt <= 0:
        return None
    return market_value / debt


def display_ratio(ratio: Decimal | None) -> Decimal | None:
    # 절사: 임계값(소수 2자리)을 위로 넘기는 일이 없다 → 표시값과 판정이 어긋나지 않음
    return None if ratio is None else ratio.quantize(_RATIO_Q, rounding=ROUND_FLOOR)


def margin_call_deadline(issued_on: date, calendar: TradingCalendar) -> date:
    """마진콜 발생일 T → 유예 만료일 T + 2영업일 (그날 장 마감 시각까지 유예)."""
    return calendar.add_business_days(issued_on, config.MARGIN_CALL_GRACE_DAYS)


def grace_expired(deadline: date, now: datetime) -> bool:
    return as_kst(now) >= datetime.combine(deadline, config.MARKET_CLOSE, tzinfo=KST)


def maturity_grace_expired(maturity_date: date, today: date) -> bool:
    """만기 + 7일 '경과': 만기 M이면 M+7일까지 유예, M+8일부터 처분 사유."""
    return today > maturity_date + timedelta(days=config.GRACE_AFTER_MATURITY_DAYS)


def evaluate(inp: RiskInput, now: datetime, calendar: TradingCalendar) -> RiskDecision:
    now = as_kst(now)
    today = now.date()
    ratio = collateral_ratio(inp.market_value, inp.debt)
    shown = display_ratio(ratio)

    if inp.status not in ACTIVE_STATUSES:
        # LIQUIDATION_PENDING(sticky)·LIQUIDATING·CLOSED는 여기서 바꾸지 않는다
        return RiskDecision(inp.status, shown, inp.margin_call_deadline, message="상태 유지")
    if ratio is None:
        return RiskDecision(inp.status, None, inp.margin_call_deadline, message="채무 없음")

    reasons: list[LiquidationReason] = []
    notes: list[str] = []
    if ratio < config.LIQUIDATION_RATIO:
        reasons.append(LiquidationReason.RATIO_BREACH)
        notes.append(f"담보비율 {shown:.2%} < {config.LIQUIDATION_RATIO:.0%}")
    if (
        inp.status == LoanStatus.MARGIN_CALL
        and inp.margin_call_deadline is not None
        and ratio < config.MAINTENANCE_RATIO
        and grace_expired(inp.margin_call_deadline, now)
    ):
        reasons.append(LiquidationReason.MARGIN_CALL_EXPIRED)
        notes.append(f"마진콜 유예 만료({inp.margin_call_deadline} 장 마감)")
    if maturity_grace_expired(inp.maturity_date, today):
        reasons.append(LiquidationReason.MATURITY_EXPIRED)
        notes.append(f"만기({inp.maturity_date}) + {config.GRACE_AFTER_MATURITY_DAYS}일 경과")
    if inp.has_ineligible_collateral:
        reasons.append(LiquidationReason.INELIGIBLE_COLLATERAL)
        notes.append("담보종목 tier X 편입")

    if reasons:
        return RiskDecision(
            LoanStatus.LIQUIDATION_PENDING, shown, inp.margin_call_deadline, tuple(reasons), "; ".join(notes)
        )
    if ratio < config.MAINTENANCE_RATIO:
        if inp.status == LoanStatus.MARGIN_CALL and inp.margin_call_deadline is not None:
            return RiskDecision(
                LoanStatus.MARGIN_CALL, shown, inp.margin_call_deadline,
                message=f"마진콜 유예 중(만료 {inp.margin_call_deadline} 장 마감)",
            )
        deadline = margin_call_deadline(today, calendar)
        return RiskDecision(
            LoanStatus.MARGIN_CALL, shown, deadline,
            message=f"담보비율 {shown:.2%} < {config.MAINTENANCE_RATIO:.0%} — {deadline} 장 마감까지 추가담보/상환 필요",
        )
    return RiskDecision(LoanStatus.NORMAL, shown, None, message=f"담보비율 {shown:.2%}")
