"""리플레이 시뮬레이터 — 과거 시세로 대출 1건을 하루씩 리플레이 (스펙 7장 8단계).

담보비율·마진콜·처분 발생일과 최종 손익을 표로 출력한다. 운영과 같은 서비스 계층(app.services)을
인메모리 DB 위에서 그대로 돌리므로 계산 로직이 운영과 다르지 않다.

사용 (stockpawn/ 디렉터리에서):
  python -m scripts.simulate --demo                                     # 오프라인 합성 급락 시나리오
  python -m scripts.simulate --code 005930 --qty 1000 --start 2025-01-02 # pykrx (KRX_ID/KRX_PW 권장)
  python -m scripts.simulate --code 005930 --qty 1000 --start 2025-01-02 --amount 30000000 --days 100
  python -m scripts.simulate --csv bars.csv --code 005930 --qty 1000 --start 2025-01-02 \\
      --market KOSPI --market-cap 400000000000000                       # CSV: date,close,volume[,trading_value]

하루 순서 (스케줄러와 동일):
  00:05  전일분 이자 적립
  08:45  (거래일) LIQUIDATION_PENDING → 처분 주문. MockBroker가 전일 종가 × (1 − slippage)로 체결
  15:40  (거래일) 장후 담보비율 평가 → 상태 전이

한계: 일봉 종가만 쓰므로 장중 10분 평가는 장후 1회로 대체된다. 장중 급락 후 종가 회복 같은
경우를 놓치므로 실제 운영보다 마진콜·처분이 **덜** 잡힐 수 있다. 체결도 전일 종가 기준이라
갭하락 손실을 과소평가한다 — `--slippage 0.03` 등으로 보수적으로 돌려 볼 것.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

if __package__ in (None, ""):  # `python scripts/simulate.py`로 실행해도 app 패키지를 찾게
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app import config, services
from app.adapters.broker import MockBroker
from app.adapters.price import DailyBar, InMemoryPriceSource, PykrxSource, StockInfo
from app.business_days import TradingCalendar, WeekdayCalendar
from app.clock import KST
from app.db import init_db, make_engine
from app.engines.risk import LoanStatus
from app.models import Collateral, Event, KycStatus, Loan

DEFAULT_DAYS = config.LOAN_TERM_DAYS


class BarsCalendar(TradingCalendar):
    """시세 데이터에 있는 날 = 거래일. 데이터 범위를 넘어서면 주말만 휴장으로 본다."""

    def __init__(self, days: Iterable[date]) -> None:
        self.days = frozenset(days)
        self.last = max(self.days)

    def is_business_day(self, d: date) -> bool:
        return d in self.days if d <= self.last else d.weekday() < 5


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def today(self) -> date:
        return self.now.date()


def _at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST)


@dataclass
class DayRow:
    date: date
    close: Decimal | None
    ratio: Decimal | None
    status: str
    outstanding_principal: Decimal
    accrued_interest: Decimal
    events: list[str] = field(default_factory=list)


@dataclass
class SimulationResult:
    code: str
    qty: int
    amount: Decimal
    start: date
    rows: list[DayRow]
    loan: Loan
    max_loan: Decimal
    tier: str
    haircut: Decimal
    liquidated_qty: int
    released_qty: int
    data_end: date | None = None  # 시세가 리플레이 기간보다 먼저 끝나면 그 마지막 날

    def first(self, event_type: str) -> date | None:
        return next((r.date for r in self.rows if event_type in r.events), None)


# ──────────────────────────────────────────────────────────
# 시세 로드
# ──────────────────────────────────────────────────────────

def load_pykrx(code: str, start: date, days: int) -> tuple[list[DailyBar], StockInfo]:
    src = PykrxSource()
    end = start + timedelta(days=days + 5)
    bars = src.get_daily_bars(code, end, config.VOLATILITY_WINDOW + 1 + days + 10)
    if not bars:
        raise SystemExit(f"{code}: pykrx 시세가 비어 있습니다 (네트워크/KRX_ID·KRX_PW 확인, 또는 --csv/--demo 사용)")
    return bars, src.get_stock_info(code, start)


def load_csv(path: Path, code: str, name: str, market: str, market_cap: Decimal | None) -> tuple[list[DailyBar], StockInfo]:
    bars = []
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            close, volume = Decimal(row["close"]), Decimal(row.get("volume") or 0)
            value = Decimal(row["trading_value"]) if row.get("trading_value") else close * volume
            bars.append(DailyBar(date.fromisoformat(row["date"]), close, volume, value))
    return bars, StockInfo(code=code, name=name or code, market=market, market_cap=market_cap)


# 합성 시나리오: 영업일 인덱스 → 종가 (사이는 선형 보간)
DEMO_PATH: Sequence[tuple[int, int]] = (
    (0, 50000), (10, 51500), (18, 46000), (21, 43000), (25, 42500),
    (30, 37000), (33, 34000), (45, 40000), (64, 45000), (80, 46000),  # 80영업일 ≈ 115일
)


def demo_bars(start: date) -> tuple[list[DailyBar], StockInfo]:
    cal = WeekdayCalendar()
    value = Decimal("50000000000")  # 일 거래대금 500억 — 유동성 한도 여유
    history: list[DailyBar] = []
    d = start
    for i in range(80):  # 변동성 산출용 과거: 50,000 ↔ 50,600 (σ ≈ 19%)
        d = cal.previous_business_day(d)
        history.append(DailyBar(d, Decimal(50600 if i % 2 == 0 else 50000), Decimal(1_000_000), value))
    history.reverse()
    future: list[DailyBar] = []
    d = start if cal.is_business_day(start) else cal.next_business_day(start)
    for (i0, p0), (i1, p1) in pairwise(DEMO_PATH):
        for i in range(i0, i1):
            price = p0 + (p1 - p0) * (i - i0) // (i1 - i0)
            future.append(DailyBar(d, Decimal(price), Decimal(1_000_000), value))
            d = cal.next_business_day(d)
    future.append(DailyBar(d, Decimal(DEMO_PATH[-1][1]), Decimal(1_000_000), value))
    info = StockInfo(code="DEMO01", name="합성급락주", market="KOSPI", market_cap=Decimal("10000000000000"))
    return history + future, info


# ──────────────────────────────────────────────────────────
# 리플레이
# ──────────────────────────────────────────────────────────

def run_simulation(
    code: str,
    qty: int,
    start: date,
    bars: Sequence[DailyBar],
    info: StockInfo,
    amount: Decimal | None = None,
    days: int = DEFAULT_DAYS,
    slippage: Decimal = Decimal("0"),
) -> SimulationResult:
    bars = sorted(bars, key=lambda b: b.date)
    trading_days = [b.date for b in bars]
    later = [d for d in trading_days if d >= start]
    if not later:
        raise SystemExit(f"{start} 이후 시세가 없습니다")
    start = later[0]  # 시작일이 휴장일이면 다음 거래일
    close_by_day = {b.date: b.close for b in bars}

    prices = InMemoryPriceSource({code: bars}, {code: info})
    clock = _Clock(_at(start, 10, 0))
    deps = services.Deps(prices=prices, broker=MockBroker(prices, today=clock.today, slippage=slippage),
                         calendar=BarsCalendar(trading_days), clock=clock)
    engine = make_engine("sqlite://")
    init_db(engine)

    with Session(engine) as s:
        borrower = services.create_borrower(s, "시뮬레이션 차주", "010-0000-0000", "SIM-0001", KycStatus.VERIFIED)
        valuation = services.value_collateral(s, deps, code, qty, start)
        if not valuation.eligible:
            raise SystemExit(f"담보 불가({valuation.tier}): {', '.join(valuation.reasons)}")
        amount = amount if amount is not None else valuation.max_loan
        loan = services.create_loan(s, deps, borrower.id, [(code, qty)], amount)

        rows: list[DayRow] = []
        last_event_id = 0
        for i in range(days):
            d = start + timedelta(days=i)
            if i > 0:
                clock.now = _at(d, 0, 5)
                services.accrue_all(s, deps)
            if d in close_by_day:
                if i > 0:
                    clock.now = _at(d, 8, 45)
                    services.liquidate_pending(s, deps)
                clock.now = _at(d, 15, 40)
                services.evaluate_all(s, deps)
            s.refresh(loan)
            new_events = s.exec(select(Event).where(Event.loan_id == loan.id, Event.id > last_event_id)
                                .order_by(Event.id)).all()
            if new_events:
                last_event_id = new_events[-1].id
            if d in close_by_day or new_events:
                rows.append(DayRow(d, close_by_day.get(d), loan.last_ratio, str(loan.status),
                                   loan.outstanding_principal, loan.accrued_interest,
                                   [str(e.type) for e in new_events]))
            if loan.status == LoanStatus.CLOSED:
                break

        pledged = s.exec(select(Collateral).where(Collateral.loan_id == loan.id)).all()
        s.expunge(loan)
        return SimulationResult(
            code=code, qty=qty, amount=amount, start=start, rows=rows, loan=loan,
            max_loan=valuation.max_loan, tier=valuation.tier, haircut=valuation.haircut,
            liquidated_qty=sum(c.liquidated_qty for c in pledged),
            released_qty=sum(c.released_qty for c in pledged),
            data_end=trading_days[-1] if trading_days[-1] < start + timedelta(days=days - 1) else None,
        )


# ──────────────────────────────────────────────────────────
# 출력
# ──────────────────────────────────────────────────────────

_SKIP_EVENTS = {"COLLATERAL_PLEDGED", "LOAN_CREATED", "DISBURSED"}


def _won(x: Decimal | None) -> str:
    return "-" if x is None else f"{x:,.0f}"


def _pct(x: Decimal | None) -> str:
    return "-" if x is None else f"{x:.1%}"


def format_report(r: SimulationResult) -> str:
    loan = r.loan
    lines = [
        f"■ 리플레이: {r.code} {r.qty:,}주, 시작 {r.start}, 등급 {r.tier} (헤어컷 {r.haircut:.2%})",
        f"  대출 {_won(r.amount)}원 (최대 {_won(r.max_loan)}원), 연 {loan.annual_rate:.0%} 단리, 만기 {loan.maturity_date}",
        "",
        f"{'날짜':<10} {'종가':>9} {'담보비율':>8}  {'상태':<20} {'원금잔액':>13} {'미수이자':>10}  이벤트",
    ]
    for row in r.rows:
        events = ", ".join(e for e in row.events if e not in _SKIP_EVENTS)
        lines.append(f"{row.date.isoformat():<10} {_won(row.close):>9} {_pct(row.ratio):>8}  {row.status:<20} "
                     f"{_won(row.outstanding_principal):>13} {_won(row.accrued_interest):>10}  {events}")
    realized = loan.repaid_interest - loan.shortfall
    closes = [row.close for row in r.rows if row.close is not None]
    peak, mdd = Decimal(0), Decimal(0)
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)
    lines += [
        "",
        "■ 결과",
        f"  최종 상태        {loan.status}",
        f"  마진콜 최초      {r.first('MARGIN_CALL') or '-'}",
        f"  처분 대기 최초   {r.first('LIQUIDATION_PENDING') or '-'}",
        f"  처분 체결        {', '.join(row.date.isoformat() for row in r.rows if 'FILL' in row.events) or '-'}",
        f"  담보 처분/반환   {r.liquidated_qty:,}주 / {r.released_qty:,}주",
        f"  주가 최대낙폭    {mdd:.1%} (리플레이 구간 종가 기준)",
        "",
        "■ 대주(대부업자) 손익",
        f"  회수 원금        {_won(loan.repaid_principal)}원 / 원금 잔액 {_won(loan.outstanding_principal)}원",
        f"  이자 수입(수납)  {_won(loan.repaid_interest)}원 / 미수이자 {_won(loan.accrued_interest)}원",
        f"  잉여금 반환      {_won(loan.surplus_returned)}원 (차주 몫)",
        f"  미수채권(부족분) {_won(loan.shortfall)}원",
        f"  실현 손익        {_won(realized)}원 (= 이자수입 − 미수채권)",
    ]
    if loan.status != LoanStatus.CLOSED:
        lines.append(f"  미종결 — 미수이자 포함 시 {_won(realized + loan.accrued_interest)}원 (미실현)")
    if r.data_end and loan.status != LoanStatus.CLOSED:
        lines.append(f"\n※ 시세 데이터가 {r.data_end}에 끝나 그 이후는 평가·처분하지 않았다.")
    lines += ["", "※ 단일 경로 리플레이 결과일 뿐이다. 다른 구간·종목(out-of-sample)으로 반복 검증할 것."]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="StockPawn 리플레이 시뮬레이터")
    p.add_argument("--code", help="종목코드 (6자리)")
    p.add_argument("--qty", type=int, help="담보 수량")
    p.add_argument("--amount", type=Decimal, help="대출금 (기본: 최대대출금)")
    p.add_argument("--start", type=date.fromisoformat, help="시작일 YYYY-MM-DD")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"리플레이 일수 (기본 {DEFAULT_DAYS})")
    p.add_argument("--slippage", type=Decimal, default=Decimal("0"), help="MockBroker 체결 할인율 (예 0.03)")
    p.add_argument("--csv", type=Path, help="오프라인 시세 CSV (date,close,volume[,trading_value])")
    p.add_argument("--name", default="", help="CSV 모드 종목명")
    p.add_argument("--market", default="KOSPI", help="CSV 모드 시장 구분")
    p.add_argument("--market-cap", type=Decimal, help="CSV 모드 시가총액(원). 없으면 최고 C등급")
    p.add_argument("--demo", action="store_true", help="합성 급락 시나리오 (네트워크 불필요)")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.ERROR)

    if a.demo:
        start = a.start or date(2026, 1, 5)
        bars, info = demo_bars(start)
        code, qty = info.code, a.qty or 1000
    else:
        if not (a.code and a.qty and a.start):
            p.error("--code, --qty, --start가 필요합니다 (또는 --demo)")
        code, qty, start = a.code, a.qty, a.start
        bars, info = (load_csv(a.csv, code, a.name, a.market, a.market_cap) if a.csv
                      else load_pykrx(code, start, a.days))
    result = run_simulation(code, qty, start, bars, info, a.amount, a.days, a.slippage)
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
