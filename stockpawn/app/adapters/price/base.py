"""시세 소스 인터페이스. MVP는 pykrx, 운영 시 KIS OpenAPI로 교체 (어댑터 패턴)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


class PriceSourceError(RuntimeError):
    """시세 조회 실패 (네트워크·로그인·미상장 종목 등)."""


@dataclass(frozen=True)
class DailyBar:
    date: date
    close: Decimal
    volume: Decimal
    trading_value: Decimal  # 거래대금(원). 소스에 없으면 종가 × 거래량으로 근사


@dataclass(frozen=True)
class StockInfo:
    code: str
    name: str
    market: str  # KOSPI | KOSDAQ | KONEX | UNKNOWN
    market_cap: Decimal | None  # 시가총액(원). 모르면 None → 최고 C등급
    is_administrative: bool = False  # 관리종목
    is_halted: bool = False  # 거래정지
    is_liquidation_trading: bool = False  # 정리매매
    is_spac: bool = False  # 스팩
    notes: tuple[str, ...] = field(default=())  # 소스 한계 등 평가 사유에 덧붙일 메모


class PriceSource(ABC):
    @abstractmethod
    def get_daily_bars(self, code: str, end: date, count: int) -> list[DailyBar]:
        """end(포함) 이전 최근 count개 거래일 일봉. 오래된 → 최신 순."""

    @abstractmethod
    def get_stock_info(self, code: str, as_of: date) -> StockInfo:
        """종목 기본정보 + 담보 적격 판정용 상태 플래그."""

    def get_last_bar(self, code: str, as_of: date) -> DailyBar:
        bars = self.get_daily_bars(code, as_of, 1)
        if not bars:
            raise PriceSourceError(f"{code}: {as_of} 이전 시세 없음")
        return bars[-1]
