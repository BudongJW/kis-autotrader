"""담보가치 평가 엔진 (스펙 2.1) — 순수 함수.

입력: 종목 시세·정보(MarketData) + 수량
  1. 최근 종가 P
  2. 유동성: 평가액이 20일 평균 거래대금의 10%를 넘으면 초과분은 담보가치 0
  3. 60일 역사적 변동성 σ(연율화) → 헤어컷 h = min(0.5, σ × 0.5)
  4. 등급 tier (시총·시장·관리종목·거래정지·변동성)
  5. 담보평가액 = min(P × 수량, 유동성 한도) × (1 − h)
  6. 최대대출금 = 담보평가액 × MAX_LTV_BY_TIER[tier]

유동성 한도는 헤어컷 **전** 시가에 먼저 적용한다. "처분 가능한 시가"를 먼저 자르고
그 위에 변동성 할인을 얹는 쪽이 반대 순서보다 항상 같거나 보수적이다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise

from app import config
from app.adapters.price.base import DailyBar, StockInfo
from app.money import ONE, ZERO, floor_won

_VOL_Q = Decimal("0.000001")
_HAIRCUT_Q = Decimal("0.0001")


@dataclass(frozen=True)
class MarketData:
    info: StockInfo
    bars: Sequence[DailyBar]  # 오래된 → 최신. 변동성용으로 VOLATILITY_WINDOW + 1개 이상 권장
    is_blocked: bool = False  # 운영자 담보 차단 (Stock.is_blocked)
    tier_floor: str | None = None  # 운영자 지정 등급 — 더 보수적인 방향으로만 반영


@dataclass(frozen=True)
class Valuation:
    code: str
    name: str
    as_of: date | None
    price: Decimal
    qty: int
    raw_value: Decimal  # P × 수량
    avg_trading_value: Decimal  # 20일 평균 거래대금
    liquidity_cap: Decimal  # 평균 거래대금 × 10%
    eligible_value: Decimal  # min(raw_value, liquidity_cap)
    volatility: Decimal | None  # 연율화 σ
    haircut: Decimal
    tier: str
    collateral_value: Decimal  # 담보평가액 (원 미만 절사)
    max_loan: Decimal  # 최대대출금 (원 미만 절사)
    reasons: tuple[str, ...]

    @property
    def ltv(self) -> Decimal:
        return config.MAX_LTV_BY_TIER[self.tier]

    @property
    def eligible(self) -> bool:
        return self.tier != "X"


def annualized_volatility(
    closes: Sequence[Decimal], window: int = config.VOLATILITY_WINDOW
) -> tuple[Decimal | None, int]:
    """최근 window개 일간 로그수익률의 표본표준편차 × √252. (σ, 사용한 수익률 개수)."""
    recent = list(closes)[-(window + 1):]
    rets = [(c1 / c0).ln() for c0, c1 in pairwise(recent) if c0 > 0 and c1 > 0]
    n = len(rets)
    if n < 2:
        return None, n
    mean = sum(rets, ZERO) / n
    var = sum(((r - mean) ** 2 for r in rets), ZERO) / (n - 1)
    sigma = var.sqrt() * Decimal(config.TRADING_DAYS_PER_YEAR).sqrt()
    return sigma.quantize(_VOL_Q), n


def haircut_for(volatility: Decimal | None) -> Decimal:
    """h = min(0.5, σ × 0.5). σ를 모르면 최대 헤어컷. 반올림은 보수적으로 올림."""
    if volatility is None:
        return config.MAX_HAIRCUT
    h = min(config.MAX_HAIRCUT, volatility * config.HAIRCUT_VOLATILITY_WEIGHT)
    return h.quantize(_HAIRCUT_Q, rounding=ROUND_CEILING)


def worse_tier(a: str, b: str) -> str:
    return a if config.TIER_ORDER.index(a) >= config.TIER_ORDER.index(b) else b


def determine_tier(
    info: StockInfo,
    volatility: Decimal | None,
    n_obs: int,
    avg_trading_value: Decimal,
    is_blocked: bool = False,
    tier_floor: str | None = None,
) -> tuple[str, list[str]]:
    """(등급, 사유). 담보 불가 사유가 하나라도 있으면 X."""
    blockers: list[str] = []
    if is_blocked:
        blockers.append("운영자 담보 차단 종목")
    if info.is_administrative:
        blockers.append("관리종목")
    if info.is_halted:
        blockers.append("거래정지")
    if info.is_liquidation_trading:
        blockers.append("정리매매")
    if info.is_spac:
        blockers.append("스팩")
    if info.market not in config.ELIGIBLE_MARKETS and info.market != "UNKNOWN":
        blockers.append(f"담보 대상 시장 아님({info.market})")
    if volatility is None or n_obs < config.VOLATILITY_MIN_OBS:
        blockers.append(f"시세 이력 부족({n_obs}일 < {config.VOLATILITY_MIN_OBS}일)")
    elif volatility > config.MAX_ELIGIBLE_VOLATILITY:
        blockers.append(f"변동성 과다(σ={volatility:.1%} > {config.MAX_ELIGIBLE_VOLATILITY:.0%})")
    if avg_trading_value <= 0:
        blockers.append("유동성 부족(최근 거래대금 없음)")
    if tier_floor == "X":
        blockers.append("운영자 지정 등급 X")
    if blockers:
        return "X", blockers

    reasons: list[str] = []
    cap = info.market_cap
    if cap is None:
        tier = "C"
        reasons.append("시가총액 미상 → 최고 C등급")
    elif info.market == "KOSPI" and cap >= config.TIER_A_MIN_MARKET_CAP:
        tier = "A"
    elif cap >= config.TIER_B_MIN_MARKET_CAP:
        tier = "B"
    else:
        tier = "C"
    if info.market == "UNKNOWN" and tier != "C":
        tier = "C"
        reasons.append("시장 구분 미확인 → 최고 C등급")

    assert volatility is not None
    if volatility > config.TIER_B_MAX_VOLATILITY and tier != "C":
        tier = "C"
        reasons.append(f"변동성 높음(σ={volatility:.1%}) → 최고 C등급")
    elif volatility > config.TIER_A_MAX_VOLATILITY and tier == "A":
        tier = "B"
        reasons.append(f"변동성 높음(σ={volatility:.1%}) → 최고 B등급")

    if n_obs < config.VOLATILITY_WINDOW:
        reasons.append(f"변동성 표본 {n_obs}일(< {config.VOLATILITY_WINDOW}일) — 신규상장 등")
    if tier_floor and worse_tier(tier, tier_floor) != tier:
        tier = tier_floor
        reasons.append(f"운영자 지정 등급 {tier_floor} 적용")
    return tier, reasons


def evaluate(data: MarketData, qty: int) -> Valuation:
    if qty <= 0:
        raise ValueError("수량은 1주 이상")
    info, bars = data.info, list(data.bars)
    if not bars:
        return Valuation(
            code=info.code, name=info.name, as_of=None, price=ZERO, qty=qty, raw_value=ZERO,
            avg_trading_value=ZERO, liquidity_cap=ZERO, eligible_value=ZERO, volatility=None,
            haircut=config.MAX_HAIRCUT, tier="X", collateral_value=ZERO, max_loan=ZERO,
            reasons=("시세 없음",),
        )

    price = bars[-1].close
    raw_value = price * qty

    recent = bars[-config.LIQUIDITY_WINDOW:]
    avg_value = floor_won(sum((b.trading_value for b in recent), ZERO) / len(recent))
    liquidity_cap = floor_won(avg_value * config.LIQUIDITY_CAP_RATIO)
    eligible_value = min(raw_value, liquidity_cap)

    vol, n_obs = annualized_volatility([b.close for b in bars])
    tier, reasons = determine_tier(info, vol, n_obs, avg_value, data.is_blocked, data.tier_floor)

    if raw_value > liquidity_cap:
        reasons.append(
            f"유동성 부족: 평가액 {raw_value:,}원 중 {liquidity_cap:,}원만 인정 "
            f"(20일 평균 거래대금 {avg_value:,}원의 {config.LIQUIDITY_CAP_RATIO:.0%})"
        )
    haircut = haircut_for(vol)
    if vol is not None:
        reasons.append(f"변동성 헤어컷 {haircut:.2%} (σ={vol:.1%})")
    reasons.extend(info.notes)

    collateral_value = floor_won(eligible_value * (ONE - haircut))
    max_loan = floor_won(collateral_value * config.MAX_LTV_BY_TIER[tier])
    return Valuation(
        code=info.code, name=info.name, as_of=bars[-1].date, price=price, qty=qty,
        raw_value=raw_value, avg_trading_value=avg_value, liquidity_cap=liquidity_cap,
        eligible_value=eligible_value, volatility=vol, haircut=haircut, tier=tier,
        collateral_value=collateral_value, max_loan=max_loan, reasons=tuple(reasons),
    )
