"""전략 기본 클래스.

모든 전략은 BaseStrategy를 상속하고 generate_signal()을 구현한다.
백테스트와 실시간 봇 양쪽에서 같은 인터페이스를 사용한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    type: SignalType
    symbol: str
    price: float
    reason: str
    # 포지션 크기 비율 (0.0 ~ 1.0). 1.0이면 자본의 100% 할당.
    weight: float = 1.0


class BaseStrategy(ABC):
    """모든 전략의 부모 클래스.

    구현해야 할 것:
      - generate_signal(symbol, history): 가장 최근 캔들 기준 매매 신호 반환
      - required_lookback: 신호 생성에 필요한 최소 캔들 수
    """

    name: str = "base"
    required_lookback: int = 60

    @abstractmethod
    def generate_signal(self, symbol: str, history: pd.DataFrame) -> Signal:
        """과거 OHLCV로 신호 생성.

        Args:
            symbol: 종목코드
            history: 가장 최신 행이 가장 최근 캔들인 OHLCV DataFrame.
                     컬럼: ['open', 'high', 'low', 'close', 'volume']
        """
        ...
