"""MockBroker — 전일 종가 기준 체결 시뮬레이션. 실제 주문은 절대 나가지 않는다."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from app import config
from app.adapters.broker.base import BrokerAdapter, OrderResult, OrderStatus, OrderType
from app.adapters.price.base import PriceSource, PriceSourceError
from app.clock import today_kst
from app.money import ONE, floor_won


class MockBroker(BrokerAdapter):
    def __init__(
        self,
        prices: PriceSource,
        today: Callable[[], date] = today_kst,
        slippage: Decimal = Decimal("0"),
        cost_rate: Decimal = config.SELL_COST_RATE,
    ) -> None:
        if not Decimal("0") <= slippage < ONE:
            raise ValueError("slippage는 0 이상 1 미만")
        self.prices = prices
        self.today = today
        self.slippage = slippage
        self.cost_rate = cost_rate
        self.orders: list[tuple[str, str, int, OrderResult]] = []  # 감사용 기록

    def place_sell_order(
        self, account: str, code: str, qty: int, order_type: OrderType = OrderType.MARKET
    ) -> OrderResult:
        order_id = f"MOCK-{uuid.uuid4().hex[:12]}"
        if qty <= 0:
            result = OrderResult(order_id, OrderStatus.REJECTED, 0, None, Decimal(0), "수량 0")
        else:
            result = self._simulate(order_id, code, qty)
        self.orders.append((account, code, qty, result))
        return result

    def _simulate(self, order_id: str, code: str, qty: int) -> OrderResult:
        try:
            prev = self.prices.get_last_bar(code, self.today() - timedelta(days=1))
        except PriceSourceError as e:
            return OrderResult(order_id, OrderStatus.REJECTED, 0, None, Decimal(0), f"시세 없음: {e}")
        if prev.volume == 0:
            return OrderResult(order_id, OrderStatus.REJECTED, 0, None, Decimal(0), "거래정지(전일 거래량 0)")
        fill_price = floor_won(prev.close * (ONE - self.slippage))
        fee = floor_won(fill_price * qty * self.cost_rate)
        return OrderResult(order_id, OrderStatus.FILLED, qty, fill_price, fee, "mock fill @ 전일 종가")
