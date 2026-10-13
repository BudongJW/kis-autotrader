"""KISBroker — stub. 실제 증권사 주문은 대부업 등록·증권사 질권/반대매매 제휴 후에만.

기본값은 BROKER=mock. BROKER=kis로 바꿔도 ENABLE_LIVE_ORDERS=true가 없으면 생성 자체를 거부한다
(kis-autotrader의 `--live` confirm flag 원칙과 같은 이중 잠금).
구현 시 kis-autotrader `src/kis_client.py`의 KISClient.order_cash(매도 TR TTTC0801U)를 쓰되,
차주 계좌에 대한 주문 권한은 증권사 제휴(질권 계좌 반대매매 위임) 구조로만 얻을 수 있다.
"""

from __future__ import annotations

from app import config
from app.adapters.broker.base import BrokerAdapter, OrderResult, OrderType


class KISBroker(BrokerAdapter):
    def __init__(self) -> None:
        if not config.ENABLE_LIVE_ORDERS:
            raise RuntimeError(
                "실주문 비활성: BROKER=kis는 ENABLE_LIVE_ORDERS=true가 함께 있어야 합니다 "
                "(대부업 등록·증권사 제휴 전에는 켜지 마세요)"
            )

    def place_sell_order(
        self, account: str, code: str, qty: int, order_type: OrderType = OrderType.MARKET
    ) -> OrderResult:
        raise NotImplementedError("KISBroker는 stub입니다 — 증권사 제휴 후 구현")
