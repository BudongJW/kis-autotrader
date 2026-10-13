"""서비스 계층 — DB 상태 + 순수 엔진 + 어댑터를 엮는다.

라우터·스케줄러·리플레이 시뮬레이터가 모두 이 함수들만 호출한다. 엔진은 계산만,
상태 변경·이벤트 기록·커밋은 여기서만 한다.

- 시간은 ``deps.now()`` 하나로만 얻는다 (테스트·리플레이에서 시계 주입).
- 원금이 바뀌기 전(상환·처분 정산)엔 항상 이자를 어제까지 따라잡는다(catch-up) —
  그래야 적수에 '바뀌기 전 잔액'이 정확히 들어간다.
- 금액을 바꾸는 진입점은 프로세스 단위 락으로 직렬화한다 (스케줄러 스레드 vs API 요청
  사이의 read-modify-write 경합 방지. 다중 프로세스 운영 시엔 DB 행 잠금으로 대체).
"""

from __future__ import annotations

import functools
import logging
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlmodel import Session, col, select

from app import config
from app.adapters.broker.base import BrokerAdapter, OrderStatus
from app.adapters.price.base import DailyBar, PriceSource, PriceSourceError
from app.business_days import TradingCalendar
from app.clock import as_kst, now_kst
from app.engines import interest, liquidation, risk
from app.engines import valuation as valuation_engine
from app.engines.risk import ACTIVE_STATUSES, LiquidationReason, LoanStatus
from app.models import (
    Borrower,
    Collateral,
    Event,
    EventType,
    InterestLog,
    KycStatus,
    LiquidationOrder,
    Loan,
    Stock,
)
from app.models import Valuation as ValuationSnapshot
from app.money import ONE, ZERO, floor_won, is_whole_won
from app.notifier import LogNotifier, Notifier

log = logging.getLogger("stockpawn")


# ──────────────────────────────────────────────────────────
# 의존성 묶음 · 오류
# ──────────────────────────────────────────────────────────

@dataclass
class Deps:
    prices: PriceSource
    broker: BrokerAdapter
    calendar: TradingCalendar
    clock: Callable[[], datetime] = now_kst
    notifier: Notifier = field(default_factory=LogNotifier)

    def now(self) -> datetime:
        return as_kst(self.clock())

    def today(self) -> date:
        return self.now().date()


class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str, reasons: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.reasons = list(reasons)


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Rejected(ServiceError):
    """업무 규칙상 거절 (KYC 미완료, tier X 담보, 최대대출금 초과 등)."""

    status_code = 422


class UpstreamError(ServiceError):
    """시세 조회 실패 등 외부 의존성 오류."""

    status_code = 503


_write_lock = threading.RLock()


def serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _write_lock:
            return fn(*args, **kwargs)

    return wrapper


# ──────────────────────────────────────────────────────────
# 공통 헬퍼
# ──────────────────────────────────────────────────────────

def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def add_event(session: Session, loan_id: int | None, type_: EventType, now: datetime, **payload: Any) -> Event:
    ev = Event(loan_id=loan_id, ts=now, type=type_, payload=_jsonable(payload))
    session.add(ev)
    return ev


def get_loan(session: Session, loan_id: int) -> Loan:
    loan = session.get(Loan, loan_id)
    if loan is None:
        raise NotFound(f"대출 {loan_id} 없음")
    return loan


def active_collaterals(session: Session, loan_id: int) -> list[Collateral]:
    rows = session.exec(select(Collateral).where(Collateral.loan_id == loan_id).order_by(Collateral.id)).all()
    return [c for c in rows if c.active_qty > 0]


def _daterange(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _merge_items(items: Iterable[tuple[str, int]]) -> list[tuple[str, int]]:
    merged: dict[str, int] = defaultdict(int)
    for code, qty in items:
        if qty <= 0:
            raise Rejected(f"{code}: 수량은 1주 이상이어야 합니다")
        merged[code] += qty
    if not merged:
        raise Rejected("담보 종목이 없습니다")
    return list(merged.items())


# ──────────────────────────────────────────────────────────
# 종목·담보평가
# ──────────────────────────────────────────────────────────

def value_collateral(
    session: Session, deps: Deps, code: str, qty: int, as_of: date | None = None
) -> valuation_engine.Valuation:
    """담보평가 + 종목 캐시 갱신. 등급이 바뀌면 그 종목을 담보로 잡은 대출에 TIER_CHANGED 기록."""
    as_of = as_of or deps.today()
    stock = session.get(Stock, code)
    try:
        info = deps.prices.get_stock_info(code, as_of)
        bars = deps.prices.get_daily_bars(code, as_of, config.VOLATILITY_WINDOW + 1)
    except PriceSourceError as e:
        raise UpstreamError(f"시세 조회 실패: {e}") from e
    data = valuation_engine.MarketData(
        info=info,
        bars=bars,
        is_blocked=bool(stock and stock.is_blocked),
        tier_floor=stock.tier_floor if stock else None,
    )
    v = valuation_engine.evaluate(data, qty)

    now = deps.now()
    old_tier = stock.tier if stock else None
    if stock is None:
        stock = Stock(code=code)
    stock.name = info.name
    stock.market = info.market
    stock.tier = v.tier
    stock.haircut = v.haircut
    stock.volatility = v.volatility
    stock.last_price = v.price if bars else None
    stock.last_reasons = list(v.reasons)
    stock.updated_at = now
    session.add(stock)

    if old_tier is not None and old_tier != v.tier:
        loan_ids = {
            c.loan_id
            for c in session.exec(select(Collateral).where(Collateral.stock_code == code)).all()
            if c.active_qty > 0
        }
        for loan_id in sorted(loan_ids):
            loan = session.get(Loan, loan_id)
            if loan and loan.status != LoanStatus.CLOSED:
                add_event(session, loan_id, EventType.TIER_CHANGED, now,
                          code=code, old=old_tier, new=v.tier, reasons=list(v.reasons))
    return v


@serialized
def update_stock_controls(
    session: Session, code: str, is_blocked: bool | None, tier_floor: str | None, clear_floor: bool = False
) -> Stock:
    """운영자 통제: 담보 차단(관리종목·정리매매 등 pykrx로 못 잡는 사유) / 등급 상한 지정.
    실제 등급 반영은 다음 재평가(장후 평가 또는 /admin/run-valuation?refresh_tiers=true) 때."""
    stock = session.get(Stock, code) or Stock(code=code)
    if is_blocked is not None:
        stock.is_blocked = is_blocked
        if is_blocked:
            stock.tier = "X"  # 차단은 즉시 반영 — 다음 리스크 평가에서 바로 처분 사유가 된다
    if clear_floor:
        stock.tier_floor = None
    elif tier_floor is not None:
        if tier_floor not in config.TIER_ORDER:
            raise Rejected(f"등급은 {config.TIER_ORDER} 중 하나")
        stock.tier_floor = tier_floor
        if tier_floor == "X":
            stock.tier = "X"
    session.add(stock)
    session.commit()
    session.refresh(stock)
    return stock


def refresh_collateral_tiers(session: Session, deps: Deps, as_of: date | None = None) -> dict[str, str]:
    """미종결 대출의 담보종목 전체 재평가 (변동성·유동성·등급·헤어컷). 장 마감 후 1회."""
    open_loan_ids = set(session.exec(select(Loan.id).where(Loan.status != LoanStatus.CLOSED)).all())
    codes = sorted({
        c.stock_code
        for c in session.exec(select(Collateral)).all()
        if c.active_qty > 0 and c.loan_id in open_loan_ids
    })
    tiers: dict[str, str] = {}
    for code in codes:
        try:
            tiers[code] = value_collateral(session, deps, code, 1, as_of).tier
        except UpstreamError as e:
            log.warning("tier_refresh_failed code=%s %s", code, e.message)
    return tiers


# ──────────────────────────────────────────────────────────
# 차주
# ──────────────────────────────────────────────────────────

@serialized
def create_borrower(session: Session, name: str, phone: str, account_no: str,
                    kyc_status: KycStatus = KycStatus.PENDING) -> Borrower:
    b = Borrower(name=name, phone=phone, account_no=account_no, kyc_status=kyc_status)
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


@serialized
def set_kyc(session: Session, borrower_id: int, status: KycStatus) -> Borrower:
    b = session.get(Borrower, borrower_id)
    if b is None:
        raise NotFound(f"차주 {borrower_id} 없음")
    b.kyc_status = status
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


# ──────────────────────────────────────────────────────────
# 대출 계약
# ──────────────────────────────────────────────────────────

def _check_amount(amount: Decimal) -> None:
    if amount <= 0 or not is_whole_won(amount):
        raise Rejected(f"금액은 양의 원 단위 정수여야 합니다: {amount}")


@serialized
def create_loan(session: Session, deps: Deps, borrower_id: int,
                collaterals: Iterable[tuple[str, int]], amount: Decimal) -> Loan:
    borrower = session.get(Borrower, borrower_id)
    if borrower is None:
        raise NotFound(f"차주 {borrower_id} 없음")
    if borrower.kyc_status != KycStatus.VERIFIED:
        raise Rejected(f"KYC 미완료 차주입니다 (상태 {borrower.kyc_status})")
    _check_amount(amount)
    items = _merge_items(collaterals)

    today, now = deps.today(), deps.now()
    valuations = [value_collateral(session, deps, code, qty, today) for code, qty in items]
    ineligible = [v for v in valuations if not v.eligible]
    if ineligible:
        session.commit()  # 종목 캐시 갱신은 남긴다
        raise Rejected("담보 불가 종목이 포함되어 있습니다",
                       [f"{v.code} {v.name}: {r}" for v in ineligible for r in v.reasons])
    max_loan = sum((v.max_loan for v in valuations), ZERO)
    if amount > max_loan:
        session.commit()
        raise Rejected(f"신청액 {amount:,}원이 최대대출금 {max_loan:,}원을 초과합니다")

    interest.check_rate(config.ANNUAL_RATE)
    loan = Loan(
        borrower_id=borrower_id,
        principal=amount,
        annual_rate=config.ANNUAL_RATE,
        start_date=today,
        maturity_date=today + timedelta(days=config.LOAN_TERM_DAYS),
        status=LoanStatus.NORMAL,
        last_accrued_date=today - timedelta(days=1),  # 대출일 당일분부터 적립 (한편넣기)
        created_at=now,
    )
    session.add(loan)
    session.flush()
    for v in valuations:
        session.add(Collateral(loan_id=loan.id, stock_code=v.code, qty=v.qty,
                               pledge_price=v.price, pledge_date=today))
        add_event(session, loan.id, EventType.COLLATERAL_PLEDGED, now, code=v.code, qty=v.qty,
                  price=v.price, tier=v.tier, collateral_value=v.collateral_value, max_loan=v.max_loan)
    add_event(session, loan.id, EventType.LOAN_CREATED, now, principal=amount, annual_rate=loan.annual_rate,
              maturity_date=loan.maturity_date, max_loan=max_loan)
    # 선이자 공제 금지: 송금액 == 원금
    add_event(session, loan.id, EventType.DISBURSED, now, amount=amount, account_no=borrower.account_no,
              mock=True)
    session.flush()
    evaluate_loan(session, deps, loan, now)
    session.commit()
    session.refresh(loan)
    return loan


# ──────────────────────────────────────────────────────────
# 이자
# ──────────────────────────────────────────────────────────

def accrue_loan(session: Session, loan: Loan, through: date) -> list[interest.AccrualPosting]:
    """last_accrued_date 다음 날부터 through까지 하루씩 적립. 기준 원금 = 현재 원금 잔액(단리)."""
    if loan.status == LoanStatus.CLOSED:
        return []
    start = (loan.last_accrued_date or loan.start_date - timedelta(days=1)) + timedelta(days=1)
    if through < start:
        return []
    postings = interest.accrue_days(loan.balance_days, loan.outstanding_principal,
                                    _daterange(start, through), loan.annual_rate)
    for p in postings:
        session.add(InterestLog(loan_id=loan.id, date=p.date, daily_interest=p.daily_interest,
                                principal_base=p.principal_base))
    loan.accrued_interest += sum((p.daily_interest for p in postings), ZERO)
    loan.balance_days = postings[-1].balance_days_after
    loan.last_accrued_date = through
    # 단리 불변식: 원금은 줄기만 하므로 적수 ≤ 최초원금 × 경과일수. 넘으면 미수이자가 이자 기준에
    # 섞인 것(복리) — 법정 상한 검사보다 먼저 잡는다.
    if loan.balance_days > loan.principal * ((through - loan.start_date).days + 1):
        raise interest.LegalRateExceeded(f"대출 {loan.id}: 적수가 최초원금×경과일수 초과 — 복리 의심")
    # 코드 레벨 상한 강제: 지금까지 부과한 이자 총액 ≤ floor(적수 × 20% / 365)
    interest.assert_within_legal_cap(loan.accrued_interest + loan.repaid_interest, loan.balance_days)
    session.add(loan)
    return postings


def catch_up_interest(session: Session, deps: Deps, loan: Loan) -> None:
    """어제까지 적립 (오늘은 한편넣기로 미포함). 원금이 바뀌기 전에 반드시 호출."""
    accrue_loan(session, loan, deps.today() - timedelta(days=1))


def pending_interest(loan: Loan, through: date) -> Decimal:
    """아직 적립 안 된 through까지의 이자 (DB 변경 없이 계산 — 조회용 완제금액)."""
    start = (loan.last_accrued_date or loan.start_date - timedelta(days=1)) + timedelta(days=1)
    days = (through - start).days + 1
    if loan.status == LoanStatus.CLOSED or days <= 0:
        return ZERO
    before = interest.interest_for_balance_days(loan.balance_days, loan.annual_rate)
    after = interest.interest_for_balance_days(loan.balance_days + loan.outstanding_principal * days,
                                               loan.annual_rate)
    return after - before


@serialized
def accrue_all(session: Session, deps: Deps, through: date | None = None) -> dict:
    """일일 이자 적립 (매일 00:05, 전일분까지)."""
    through = through or deps.today() - timedelta(days=1)
    loans = session.exec(select(Loan).where(Loan.status != LoanStatus.CLOSED)).all()
    days = 0
    total = ZERO
    for loan in loans:
        postings = accrue_loan(session, loan, through)
        days += len(postings)
        total += sum((p.daily_interest for p in postings), ZERO)
    session.commit()
    return {"through": through.isoformat(), "loans": len(loans), "days_posted": days, "interest": str(total)}


# ──────────────────────────────────────────────────────────
# 담보비율 평가 · 상태 전이
# ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Holding:
    collateral: Collateral
    stock: Stock
    bar: DailyBar


def _holdings(session: Session, deps: Deps, loan: Loan, as_of: date) -> list[_Holding]:
    out = []
    for c in active_collaterals(session, loan.id):
        stock = session.get(Stock, c.stock_code)
        bar = deps.prices.get_last_bar(c.stock_code, as_of)  # PriceSourceError는 호출측에서
        out.append(_Holding(c, stock, bar))
    return out


def evaluate_loan(session: Session, deps: Deps, loan: Loan, now: datetime | None = None) -> risk.RiskDecision | None:
    """담보비율 계산 → 상태 전이 → 스냅샷 기록. 시세를 못 받으면 상태를 건드리지 않는다
    (시세 0으로 간주해 멀쩡한 대출을 처분하는 사고 방지)."""
    now = as_kst(now or deps.now())
    if loan.status not in ACTIVE_STATUSES:
        return None
    try:
        holdings = _holdings(session, deps, loan, now.date())
    except PriceSourceError as e:
        log.warning("evaluate_skipped loan=%s %s", loan.id, e)
        return None
    market_value = sum((h.bar.close * h.collateral.active_qty for h in holdings), ZERO)
    collateral_value = sum(
        (floor_won(h.bar.close * h.collateral.active_qty * (ONE - h.stock.haircut)) for h in holdings), ZERO
    )
    decision = risk.evaluate(
        risk.RiskInput(
            status=loan.status,
            market_value=market_value,
            debt=loan.debt,
            maturity_date=loan.maturity_date,
            margin_call_deadline=loan.margin_call_deadline,
            has_ineligible_collateral=any(h.stock.tier == "X" for h in holdings),
        ),
        now,
        deps.calendar,
    )
    _apply_decision(session, deps, loan, decision, now)
    session.add(ValuationSnapshot(loan_id=loan.id, ts=now, market_value=market_value,
                                  collateral_value=collateral_value, debt=loan.debt,
                                  ratio=decision.ratio, status_at_ts=loan.status))
    return decision


def _apply_decision(session: Session, deps: Deps, loan: Loan, decision: risk.RiskDecision, now: datetime) -> None:
    old, new = loan.status, decision.status
    loan.last_ratio = decision.ratio
    if new != old:
        if new == LoanStatus.MARGIN_CALL:
            loan.margin_call_at = now
            loan.margin_call_deadline = decision.margin_call_deadline
            add_event(session, loan.id, EventType.MARGIN_CALL, now, ratio=decision.ratio,
                      deadline=decision.margin_call_deadline, message=decision.message)
            deps.notifier.notify(loan.id, "MARGIN_CALL", decision.message)
        elif new == LoanStatus.NORMAL and old == LoanStatus.MARGIN_CALL:
            loan.margin_call_at = None
            loan.margin_call_deadline = None
            add_event(session, loan.id, EventType.MARGIN_CALL_CURED, now, ratio=decision.ratio)
            deps.notifier.notify(loan.id, "MARGIN_CALL_CURED", decision.message)
        elif new == LoanStatus.LIQUIDATION_PENDING:
            _mark_liquidation_pending(session, deps, loan, decision.liquidation_reasons, now,
                                      decision.ratio, decision.message)
        loan.status = new
    session.add(loan)


def _mark_liquidation_pending(session: Session, deps: Deps, loan: Loan, reasons: Iterable[LiquidationReason],
                              now: datetime, ratio: Decimal | None, message: str) -> None:
    loan.status = LoanStatus.LIQUIDATION_PENDING
    loan.liquidation_reasons = sorted({*loan.liquidation_reasons, *(str(r) for r in reasons)})
    loan.liquidation_pending_at = now
    add_event(session, loan.id, EventType.LIQUIDATION_PENDING, now, reasons=loan.liquidation_reasons,
              ratio=ratio, message=message)
    deps.notifier.notify(loan.id, "LIQUIDATION_PENDING", message or ", ".join(loan.liquidation_reasons))


def _maturity_notices(session: Session, deps: Deps, now: datetime) -> int:
    """만기 D-7 알림 (하루 늦게 돌아도 만기 전이면 1회 발송)."""
    today = now.date()
    sent = 0
    loans = session.exec(select(Loan).where(col(Loan.status).in_(list(ACTIVE_STATUSES)))).all()
    for loan in loans:
        if not (loan.maturity_date - timedelta(days=config.MATURITY_NOTICE_DAYS) <= today <= loan.maturity_date):
            continue
        exists = session.exec(select(Event).where(Event.loan_id == loan.id,
                                                  Event.type == EventType.MATURITY_NOTICE)).first()
        if exists:
            continue
        payoff = loan.debt + pending_interest(loan, loan.maturity_date - timedelta(days=1))  # 만기일 당일분 없음
        msg = (f"만기 {loan.maturity_date} — 만기일 완제 금액 {payoff:,}원. "
               f"만기 후 {config.GRACE_AFTER_MATURITY_DAYS}일 경과 시 담보 처분")
        add_event(session, loan.id, EventType.MATURITY_NOTICE, now, maturity_date=loan.maturity_date, message=msg)
        deps.notifier.notify(loan.id, "MATURITY_NOTICE", msg)
        sent += 1
    return sent


@serialized
def evaluate_all(session: Session, deps: Deps, now: datetime | None = None, refresh_tiers: bool = False) -> dict:
    """담보비율 일괄 평가 (장중 10분 + 장후 1회). 장후엔 refresh_tiers=True로 등급까지 재산정."""
    now = as_kst(now or deps.now())
    tiers = refresh_collateral_tiers(session, deps, now.date()) if refresh_tiers else {}
    counts: dict[str, int] = defaultdict(int)
    skipped = 0
    loans = session.exec(select(Loan).where(col(Loan.status).in_(list(ACTIVE_STATUSES)))).all()
    for loan in loans:
        if evaluate_loan(session, deps, loan, now) is None:
            skipped += 1
        counts[str(loan.status)] += 1
    notices = _maturity_notices(session, deps, now)
    session.commit()
    return {"ts": now.isoformat(), "evaluated": len(loans) - skipped, "skipped": skipped,
            "status_counts": dict(counts), "maturity_notices": notices, "tiers_refreshed": tiers}


# ──────────────────────────────────────────────────────────
# 상환 · 추가담보 · 종결
# ──────────────────────────────────────────────────────────

def _release_and_close(session: Session, loan: Loan, now: datetime, **payload: Any) -> None:
    for c in active_collaterals(session, loan.id):
        add_event(session, loan.id, EventType.COLLATERAL_RELEASED, now, code=c.stock_code, qty=c.active_qty)
        c.released_qty += c.active_qty
        session.add(c)
    loan.status = LoanStatus.CLOSED
    loan.closed_at = now
    loan.margin_call_deadline = None
    add_event(session, loan.id, EventType.CLOSED, now, **payload)
    session.add(loan)


@serialized
def repay(session: Session, deps: Deps, loan_id: int, amount: Decimal) -> tuple[Loan, interest.PaymentAllocation]:
    """상환: 어제까지 일할 이자 정산 → 이자 먼저, 남으면 원금. 완제면 담보 해제·종결. 수수료 없음."""
    loan = get_loan(session, loan_id)
    if loan.status == LoanStatus.CLOSED:
        raise Conflict("이미 종결된 대출입니다")
    if loan.status == LoanStatus.LIQUIDATING:
        raise Conflict("처분 주문 진행 중에는 상환할 수 없습니다 — 체결 정산 후 다시 시도")
    _check_amount(amount)
    now = deps.now()
    catch_up_interest(session, deps, loan)
    try:
        alloc = interest.allocate_payment(amount, loan.accrued_interest, loan.outstanding_principal)
    except ValueError as e:
        raise Rejected(str(e)) from e
    loan.accrued_interest -= alloc.to_interest
    loan.repaid_interest += alloc.to_interest
    loan.repaid_principal += alloc.to_principal
    add_event(session, loan.id, EventType.REPAY, now, amount=amount, to_interest=alloc.to_interest,
              to_principal=alloc.to_principal, remaining_principal=alloc.remaining_principal,
              remaining_interest=alloc.remaining_interest)
    if alloc.fully_repaid:
        _release_and_close(session, loan, now, reason="REPAID")
    else:
        evaluate_loan(session, deps, loan, now)  # 마진콜 해소 여부 재판정
    session.add(loan)
    session.commit()
    session.refresh(loan)
    return loan, alloc


@serialized
def add_collateral(session: Session, deps: Deps, loan_id: int, items: Iterable[tuple[str, int]]) -> Loan:
    loan = get_loan(session, loan_id)
    if loan.status not in ACTIVE_STATUSES:
        raise Conflict(f"{loan.status} 상태에서는 추가 담보를 받을 수 없습니다")
    merged = _merge_items(items)
    today, now = deps.today(), deps.now()
    valuations = [value_collateral(session, deps, code, qty, today) for code, qty in merged]
    ineligible = [v for v in valuations if not v.eligible]
    if ineligible:
        session.commit()
        raise Rejected("담보 불가 종목이 포함되어 있습니다",
                       [f"{v.code} {v.name}: {r}" for v in ineligible for r in v.reasons])
    session.flush()  # 새 종목(Stock)을 먼저 INSERT — relationship이 없어 UOW가 FK 순서를 모른다
    for v in valuations:
        session.add(Collateral(loan_id=loan.id, stock_code=v.code, qty=v.qty, pledge_price=v.price,
                               pledge_date=today))
        add_event(session, loan.id, EventType.COLLATERAL_ADDED, now, code=v.code, qty=v.qty, price=v.price,
                  tier=v.tier)
    session.flush()
    evaluate_loan(session, deps, loan, now)
    session.commit()
    session.refresh(loan)
    return loan


# ──────────────────────────────────────────────────────────
# 처분
# ──────────────────────────────────────────────────────────

@serialized
def trigger_manual_liquidation(session: Session, deps: Deps, loan_id: int, note: str = "") -> Loan:
    """운영자 수동 처분 — 원리금 회수분(PAYOFF)을 즉시 MockBroker로 처분한다."""
    loan = get_loan(session, loan_id)
    if loan.status in (LoanStatus.CLOSED, LoanStatus.LIQUIDATING):
        raise Conflict(f"{loan.status} 상태에서는 처분을 시작할 수 없습니다")
    now = deps.now()
    _mark_liquidation_pending(session, deps, loan, [LiquidationReason.MANUAL], now, loan.last_ratio,
                              note or "운영자 수동 처분")
    session.flush()
    liquidate_loan(session, deps, loan, now)
    session.commit()
    session.refresh(loan)
    return loan


@serialized
def liquidate_pending(session: Session, deps: Deps, now: datetime | None = None) -> dict:
    """LIQUIDATION_PENDING 대출 처분 주문 (거래일 장 시작 동시호가)."""
    now = as_kst(now or deps.now())
    loans = session.exec(select(Loan).where(Loan.status == LoanStatus.LIQUIDATION_PENDING)).all()
    results = {}
    for loan in loans:
        plan = liquidate_loan(session, deps, loan, now)
        results[loan.id] = {"mode": str(plan.mode) if plan else None, "status": str(loan.status)}
    session.commit()
    return {"ts": now.isoformat(), "processed": len(loans), "loans": results}


def liquidate_loan(session: Session, deps: Deps, loan: Loan, now: datetime) -> liquidation.LiquidationPlan | None:
    if loan.status != LoanStatus.LIQUIDATION_PENDING:
        return None
    catch_up_interest(session, deps, loan)
    prev_day = now.date() - timedelta(days=1)

    by_code: dict[str, list[Collateral]] = defaultdict(list)
    for c in active_collaterals(session, loan.id):
        by_code[c.stock_code].append(c)
    positions = []
    for code, rows in by_code.items():
        stock = session.get(Stock, code)
        try:
            bar = deps.prices.get_last_bar(code, prev_day)  # 기준가 = 전일 종가
        except PriceSourceError as e:
            log.warning("liquidation_price_missing loan=%s code=%s %s", loan.id, code, e)
            continue
        positions.append(liquidation.Position(
            code=code, qty=sum(c.active_qty for c in rows), ref_price=bar.close,
            tier=stock.tier if stock else "X", haircut=stock.haircut if stock else config.MAX_HAIRCUT,
            tradable=bar.volume > 0,
        ))

    market_value = sum((p.market_value for p in positions), ZERO)
    mode = liquidation.choose_mode((LiquidationReason(r) for r in loan.liquidation_reasons),
                                   risk.collateral_ratio(market_value, loan.debt))
    plan = liquidation.plan_liquidation(positions, loan.debt, mode)
    if not plan.orders:
        add_event(session, loan.id, EventType.ORDER_FAILED, now, mode=plan.mode,
                  message="처분 가능한 담보 없음 (거래정지·시세 없음) — 다음 거래일 재시도", notes=plan.notes)
        session.add(loan)
        return plan

    loan.status = LoanStatus.LIQUIDATING
    borrower = session.get(Borrower, loan.borrower_id)
    any_fill = False
    for order in plan.orders:
        lo = LiquidationOrder(loan_id=loan.id, stock_code=order.code, qty=order.qty, mode=str(plan.mode),
                              expected_price=order.expected_price, order_ts=now)
        session.add(lo)
        session.flush()
        add_event(session, loan.id, EventType.LIQ_ORDER, now, order_id=lo.id, code=order.code, qty=order.qty,
                  mode=plan.mode, expected_price=order.expected_price, ratio_before=plan.ratio_before,
                  expected_ratio_after=plan.expected_ratio_after)
        result = deps.broker.place_sell_order(borrower.account_no, order.code, order.qty)
        lo.broker_order_id = result.order_id
        lo.status = str(result.status)
        lo.message = result.message
        if result.status == OrderStatus.REJECTED or result.fill_qty <= 0:
            add_event(session, loan.id, EventType.ORDER_FAILED, now, order_id=lo.id, code=order.code,
                      message=result.message)
            session.add(lo)
            continue
        any_fill = True
        lo.fill_qty = result.fill_qty
        lo.fill_price = result.fill_price
        lo.fee = result.fee
        session.add(lo)
        _allocate_liquidated(session, by_code[order.code], result.fill_qty)
        s = liquidation.settle(result.net_proceeds, loan.accrued_interest, loan.outstanding_principal)
        loan.accrued_interest -= s.to_interest
        loan.repaid_interest += s.to_interest
        loan.repaid_principal += s.to_principal
        loan.surplus_returned += s.surplus
        add_event(session, loan.id, EventType.FILL, now, order_id=lo.id, code=order.code, qty=result.fill_qty,
                  price=result.fill_price, fee=result.fee, net=s.net_proceeds, to_interest=s.to_interest,
                  to_principal=s.to_principal, surplus=s.surplus)

    _after_liquidation(session, deps, loan, now, any_fill)
    return plan


def _allocate_liquidated(session: Session, rows: list[Collateral], qty: int) -> None:
    remaining = qty
    for c in rows:  # 질권 설정 순서(FIFO)
        take = min(remaining, c.active_qty)
        c.liquidated_qty += take
        remaining -= take
        session.add(c)
        if remaining == 0:
            break


def _after_liquidation(session: Session, deps: Deps, loan: Loan, now: datetime, any_fill: bool) -> None:
    if loan.surplus_returned > 0 and loan.debt == 0:
        add_event(session, loan.id, EventType.SURPLUS_RETURNED, now, amount=loan.surplus_returned, mock=True)
    if loan.debt == 0:
        _release_and_close(session, loan, now, reason="LIQUIDATED", surplus=loan.surplus_returned)
        return
    session.flush()
    if not active_collaterals(session, loan.id):
        # 담보 전량 처분 후에도 남은 채무 → 미수채권. 이후 회수는 시스템 밖(추심 절차)
        loan.shortfall = loan.debt
        add_event(session, loan.id, EventType.SHORTFALL_RECORDED, now, amount=loan.shortfall,
                  principal=loan.outstanding_principal, interest=loan.accrued_interest)
        loan.status = LoanStatus.CLOSED
        loan.closed_at = now
        add_event(session, loan.id, EventType.CLOSED, now, reason="LIQUIDATED_WITH_SHORTFALL",
                  shortfall=loan.shortfall)
        session.add(loan)
        return
    if not any_fill:
        loan.status = LoanStatus.LIQUIDATION_PENDING  # 전부 거부 → 다음 거래일 재시도
        session.add(loan)
        return
    # 부분 처분 완료 → 정상 상태로 되돌린 뒤 현재 시세로 재평가 (사유가 남아 있으면 다시 PENDING)
    loan.status = LoanStatus.NORMAL
    loan.margin_call_at = None
    loan.margin_call_deadline = None
    loan.liquidation_reasons = []
    loan.liquidation_pending_at = None
    session.add(loan)
    evaluate_loan(session, deps, loan, now)


# ──────────────────────────────────────────────────────────
# 조회
# ──────────────────────────────────────────────────────────

RISK_BANDS: tuple[tuple[str, Decimal | None], ...] = (
    ("<120%", config.LIQUIDATION_RATIO),
    ("120~140%", config.MAINTENANCE_RATIO),
    ("140~150%", config.PARTIAL_LIQUIDATION_TARGET_RATIO),
    ("150~200%", Decimal("2.00")),
    ("≥200%", None),
)


def risk_band(ratio: Decimal | None) -> str:
    if ratio is None:
        return "미평가"
    for name, upper in RISK_BANDS:
        if upper is None or ratio < upper:
            return name
    return RISK_BANDS[-1][0]


def payoff_today(loan: Loan, deps: Deps) -> Decimal:
    """오늘 완제 시 금액 = 원금 잔액 + 어제까지 이자 (오늘분 없음, 수수료 없음)."""
    return interest.payoff_amount(loan.accrued_interest + pending_interest(loan, deps.today() - timedelta(days=1)),
                                  loan.outstanding_principal)


def dashboard(session: Session, deps: Deps) -> dict:
    loans = session.exec(select(Loan)).all()
    by_status: dict[str, dict] = {
        str(s): {"count": 0, "outstanding_principal": ZERO, "accrued_interest": ZERO} for s in LoanStatus
    }
    bands: dict[str, dict] = {name: {"count": 0, "outstanding_principal": ZERO}
                              for name in [b[0] for b in RISK_BANDS] + ["미평가"]}
    totals = {"principal_disbursed": ZERO, "outstanding_principal": ZERO, "accrued_interest": ZERO,
              "interest_collected": ZERO, "surplus_returned": ZERO, "shortfall": ZERO}
    at_risk = []
    today = deps.today()
    upcoming = []
    for loan in loans:
        s = by_status[str(loan.status)]
        s["count"] += 1
        s["outstanding_principal"] += loan.outstanding_principal
        s["accrued_interest"] += loan.accrued_interest
        totals["principal_disbursed"] += loan.principal
        totals["interest_collected"] += loan.repaid_interest
        totals["surplus_returned"] += loan.surplus_returned
        totals["shortfall"] += loan.shortfall
        if loan.status == LoanStatus.CLOSED:
            continue
        totals["outstanding_principal"] += loan.outstanding_principal
        totals["accrued_interest"] += loan.accrued_interest
        band = bands[risk_band(loan.last_ratio)]
        band["count"] += 1
        band["outstanding_principal"] += loan.outstanding_principal
        borrower = session.get(Borrower, loan.borrower_id)
        row = {"loan_id": loan.id, "borrower": borrower.name if borrower else "", "status": str(loan.status),
               "ratio": loan.last_ratio, "outstanding_principal": loan.outstanding_principal,
               "accrued_interest": loan.accrued_interest, "maturity_date": loan.maturity_date,
               "margin_call_deadline": loan.margin_call_deadline,
               "liquidation_reasons": list(loan.liquidation_reasons)}
        if loan.status != LoanStatus.NORMAL:
            at_risk.append(row)
        if (loan.maturity_date - today).days <= config.MATURITY_NOTICE_DAYS:
            upcoming.append(row)
    at_risk.sort(key=lambda r: (r["ratio"] is None, r["ratio"] or ZERO))
    upcoming.sort(key=lambda r: r["maturity_date"])
    return _jsonable({
        "as_of": deps.now(),
        "params": {"annual_rate": config.ANNUAL_RATE, "maintenance_ratio": config.MAINTENANCE_RATIO,
                   "liquidation_ratio": config.LIQUIDATION_RATIO, "loan_term_days": config.LOAN_TERM_DAYS,
                   "broker": config.BROKER, "price_source": config.PRICE_SOURCE},
        "totals": totals,
        "by_status": by_status,
        "risk_bands": bands,
        "at_risk": at_risk,
        "upcoming_maturities": upcoming,
    })
