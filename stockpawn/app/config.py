"""StockPawn 도메인 파라미터 (스펙 2장) + 런타임 설정.

- 모든 금액·비율은 ``Decimal``. float 금지.
- **법정 최고금리(연 20%) 초과 설정은 import 시점에 ValueError** — 앱이 아예 뜨지 않는다.
  연체 가산이자·중도상환수수료도 이자로 간주되므로 합산해서 검사한다.
- 런타임 값(DB·브로커·시세 소스)은 환경변수로만 바꾼다. 비밀값은 코드에 두지 않는다.
"""

from __future__ import annotations

import os
from datetime import date, time
from decimal import Decimal, InvalidOperation

# ──────────────────────────────────────────────────────────
# 법정 상한
# ──────────────────────────────────────────────────────────

LEGAL_MAX_RATE = Decimal("0.20")  # 대부업법·이자제한법 최고금리 (연)


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, "").strip() or default
    try:
        return Decimal(raw)
    except InvalidOperation as e:
        raise ValueError(f"{name}={raw!r} 는 숫자가 아닙니다") from e


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_dates(name: str) -> frozenset[date]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    return frozenset(date.fromisoformat(s.strip()) for s in raw.split(",") if s.strip())


# ──────────────────────────────────────────────────────────
# 핵심 도메인 규칙 (스펙 2장)
# ──────────────────────────────────────────────────────────

ANNUAL_RATE = _env_decimal("ANNUAL_RATE", "0.20")  # 연 20% 고정 (낮추는 것만 허용)
DAY_COUNT = 365

# 연체 가산이자: 약정금리 + 가산금리 총합이 20%를 넘을 수 없으므로 0 고정.
# 연체는 이자 가산이 아니라 만기 유예 후 처분으로 대응한다 (스펙 2.2).
LATE_INTEREST_SURCHARGE = Decimal("0")
# 중도상환수수료: 수수료도 이자로 간주되므로 0 고정. 선이자 공제도 금지.
EARLY_REPAYMENT_FEE_RATE = Decimal("0")

MAX_LTV_BY_TIER: dict[str, Decimal] = {  # 종목 등급별 최대 대출비율 (담보평가액 대비)
    "A": Decimal("0.70"),  # 대형 우량주 (KOSPI 시총 5조↑, 저변동)
    "B": Decimal("0.50"),  # 중형주
    "C": Decimal("0.30"),  # 소형/변동성 높음
    "X": Decimal("0.00"),  # 담보 불가 (관리종목, 거래정지, 정리매매, 스팩 등)
}
TIER_ORDER = ("A", "B", "C", "X")  # 뒤로 갈수록 위험

MAINTENANCE_RATIO = Decimal("1.40")  # 담보유지비율 140% 미만 → 마진콜
LIQUIDATION_RATIO = Decimal("1.20")  # 120% 미만 → 즉시 처분 사유
MARGIN_CALL_GRACE_DAYS = 2  # 마진콜 후 추가담보/상환 유예 (영업일)
LOAN_TERM_DAYS = 90  # 기본 대출기간 (일)
GRACE_AFTER_MATURITY_DAYS = 7  # 만기 후 유예 (일) → 경과 시 처분
MATURITY_NOTICE_DAYS = 7  # 만기 D-7 알림
HAIRCUT_VOLATILITY_WEIGHT = Decimal("0.5")  # 변동성 기반 추가 할인
MAX_HAIRCUT = Decimal("0.5")

# ── 평가 엔진 (2.1) ──
VOLATILITY_WINDOW = 60  # 역사적 변동성 산출 구간 (거래일 수익률 개수)
VOLATILITY_MIN_OBS = 20  # 이보다 이력이 짧으면 담보 불가 (신규상장 등)
TRADING_DAYS_PER_YEAR = 252  # 변동성 연율화
LIQUIDITY_WINDOW = 20  # 평균 거래대금 산출 구간
LIQUIDITY_CAP_RATIO = Decimal("0.10")  # 평가액이 20일 평균 거래대금의 10% 초과분은 가치 0

TIER_A_MIN_MARKET_CAP = Decimal("5000000000000")  # 5조
TIER_B_MIN_MARKET_CAP = Decimal("500000000000")  # 5,000억
TIER_A_MAX_VOLATILITY = Decimal("0.40")  # 초과 시 최고 B
TIER_B_MAX_VOLATILITY = Decimal("0.60")  # 초과 시 최고 C
MAX_ELIGIBLE_VOLATILITY = Decimal("1.00")  # 초과 시 X (변동성 과다)
ELIGIBLE_MARKETS = ("KOSPI", "KOSDAQ")  # 해외주식·KONEX는 Phase 3

# ── 처분 엔진 (2.4) ──
PARTIAL_LIQUIDATION_TARGET_RATIO = Decimal("1.50")  # 부분 처분 시 회복 목표 담보비율
FULL_LIQUIDATION_RATIO = Decimal("1.00")  # 이 미만일 때만 전량 처분
EXPECTED_FILL_DISCOUNT = Decimal("0.97")  # 예상체결가 = 전일 종가 × 0.97
# 처분 실비(증권거래세 + 위탁수수료) 보수적 추정. 대주가 받는 돈이 아니라 제3자에게
# 나가는 매도 비용이므로 이자가 아니다 — 대주 수익으로 잡히는 수수료는 여전히 0.
SELL_COST_RATE = Decimal("0.0025")

# ── 시장 시간 (KST) ──
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)
MONITOR_INTERVAL_MINUTES = 10
INTEREST_JOB_TIME = time(0, 5)  # 전일분 이자 적립
LIQUIDATION_JOB_TIME = time(8, 45)  # 장 시작 동시호가(08:30~09:00) 접수 시간에 처분 주문
POST_CLOSE_JOB_TIME = time(15, 40)  # 장 마감 후 1회 평가 + 등급 재산정


# ──────────────────────────────────────────────────────────
# 런타임 (환경변수)
# ──────────────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/stockpawn.db")
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "pykrx").strip().lower()
BROKER = os.getenv("BROKER", "mock").strip().lower()  # 실주문 브로커는 기본 비활성
ENABLE_LIVE_ORDERS = _env_bool("ENABLE_LIVE_ORDERS", False)
SCHEDULER_ENABLED = _env_bool("SCHEDULER_ENABLED", True)
EXTRA_HOLIDAYS = _env_dates("EXTRA_HOLIDAYS")


# ──────────────────────────────────────────────────────────
# 검증 — import 시점에 실행
# ──────────────────────────────────────────────────────────

def validate_rates(
    annual_rate: Decimal,
    late_surcharge: Decimal = LATE_INTEREST_SURCHARGE,
    fee_rate: Decimal = EARLY_REPAYMENT_FEE_RATE,
) -> None:
    """약정금리 + 연체 가산 + 수수료 총합이 법정 최고금리를 넘으면 ValueError."""
    if annual_rate <= 0:
        raise ValueError(f"ANNUAL_RATE는 양수여야 합니다: {annual_rate}")
    if late_surcharge < 0 or fee_rate < 0:
        raise ValueError("가산이자·수수료율은 음수일 수 없습니다")
    total = annual_rate + late_surcharge + fee_rate
    if total > LEGAL_MAX_RATE:
        raise ValueError(
            f"총 이자율 {total} (약정 {annual_rate} + 연체가산 {late_surcharge} + 수수료 {fee_rate})이 "
            f"법정 최고금리 {LEGAL_MAX_RATE}를 초과합니다"
        )


def _validate_structure() -> None:
    if not LIQUIDATION_RATIO < MAINTENANCE_RATIO < PARTIAL_LIQUIDATION_TARGET_RATIO:
        raise ValueError("LIQUIDATION_RATIO < MAINTENANCE_RATIO < PARTIAL_LIQUIDATION_TARGET_RATIO 이어야 합니다")
    # 신규 대출이 체결 즉시 마진콜 상태가 되지 않도록: 최대 LTV × 유지비율 ≤ 1
    for tier, ltv in MAX_LTV_BY_TIER.items():
        if ltv * MAINTENANCE_RATIO > 1:
            raise ValueError(f"tier {tier} LTV {ltv}는 담보유지비율 {MAINTENANCE_RATIO}와 양립할 수 없습니다")
    if MAX_LTV_BY_TIER["X"] != 0:
        raise ValueError("tier X의 LTV는 0이어야 합니다")
    if PRICE_SOURCE not in ("pykrx", "kis"):
        raise ValueError(f"PRICE_SOURCE={PRICE_SOURCE!r} — pykrx|kis 중 하나")
    if BROKER not in ("mock", "kis"):
        raise ValueError(f"BROKER={BROKER!r} — mock|kis 중 하나")


validate_rates(ANNUAL_RATE)
_validate_structure()
