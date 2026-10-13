"""처분(반대매매) 주문 인터페이스. MVP는 MockBroker, 운영은 KISBroker(stub)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class OrderType(StrEnum):
    MARKET = "MARKET"  # 시장가 — 장 시작 동시호가에 접수


class OrderStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: OrderStatus
    fill_qty: int
    fill_price: Decimal | None  # 평균 체결가
    fee: Decimal  # 매도 비용(증권거래세 + 위탁수수료) — 제3자 지급분
    message: str = ""

    @property
    def gross(self) -> Decimal:
        return (self.fill_price or Decimal(0)) * self.fill_qty

    @property
    def net_proceeds(self) -> Decimal:
        return self.gross - self.fee


class BrokerAdapter(ABC):
    @abstractmethod
    def place_sell_order(
        self, account: str, code: str, qty: int, order_type: OrderType = OrderType.MARKET
    ) -> OrderResult:
        """질권 설정된 차주 계좌의 담보주식 매도."""
