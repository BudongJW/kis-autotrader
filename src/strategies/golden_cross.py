"""골든크로스 전략 — 가장 단순한 추세추종 예제.

규칙:
  - 단기 이동평균(MA5)이 장기 이동평균(MA20)을 상향 돌파 → BUY
  - 단기가 장기를 하향 돌파 (데드크로스) → SELL
  - 그 외 → HOLD

주의:
  - 횡보장에서 휩쏘(whipsaw) 손실이 매우 흔하다.
  - 백테스트 결과가 좋아 보여도 거래비용·슬리피지 반영 후 다시 검증할 것.
  - 단독 사용 비추천. 추세 필터 (예: MA60 위만 진입) 추가하면 개선.
"""

from __future__ import annotations

import pandas as pd

from src.strategies.base import BaseStrategy, Signal, SignalType


class GoldenCrossStrategy(BaseStrategy):
    name = "golden_cross"

    def __init__(self, short_window: int = 5, long_window: int = 20) -> None:
        if short_window >= long_window:
            raise ValueError("short_window는 long_window보다 작아야 함")
        self.short_window = short_window
        self.long_window = long_window
        self.required_lookback = long_window + 5

    def generate_signal(self, symbol: str, history: pd.DataFrame) -> Signal:
        if len(history) < self.required_lookback:
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                price=float(history["close"].iloc[-1]) if len(history) else 0.0,
                reason=f"데이터 부족 ({len(history)} < {self.required_lookback})",
            )

        short_ma = history["close"].rolling(self.short_window).mean()
        long_ma = history["close"].rolling(self.long_window).mean()

        prev_short, prev_long = short_ma.iloc[-2], long_ma.iloc[-2]
        cur_short, cur_long = short_ma.iloc[-1], long_ma.iloc[-1]
        cur_price = float(history["close"].iloc[-1])

        crossed_up = prev_short <= prev_long and cur_short > cur_long
        crossed_down = prev_short >= prev_long and cur_short < cur_long

        if crossed_up:
            return Signal(
                type=SignalType.BUY,
                symbol=symbol,
                price=cur_price,
                reason=f"골든크로스 MA{self.short_window}={cur_short:.0f} > MA{self.long_window}={cur_long:.0f}",
            )
        if crossed_down:
            return Signal(
                type=SignalType.SELL,
                symbol=symbol,
                price=cur_price,
                reason=f"데드크로스 MA{self.short_window}={cur_short:.0f} < MA{self.long_window}={cur_long:.0f}",
            )
        return Signal(
            type=SignalType.HOLD,
            symbol=symbol,
            price=cur_price,
            reason="크로스 없음",
        )
