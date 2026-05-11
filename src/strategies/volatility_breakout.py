"""변동성 돌파 전략 — 래리 윌리엄스 기반 + 추세 필터.

규칙:
  - 목표가 = 당일 시가 + (전일 고가 - 전일 저가) × K
  - 현재가가 목표가를 돌파하면 BUY
  - 추세 필터: 현재가가 MA(trend_ma) 위에 있을 때만 진입
  - 청산: 익일 시가에 매도 (봇에서는 장 시작 직후 매도)

참고:
  - K=0.5가 래리 윌리엄스 기본값. 한국 시장에서는 0.4~0.6이 일반적.
  - K가 낮을수록 진입 빈번(노이즈 많음), 높을수록 보수적.
  - 추세 필터 없이 쓰면 횡보장에서 연속 손실 발생.
"""

from __future__ import annotations

import pandas as pd

from src.strategies.base import BaseStrategy, Signal, SignalType


class VolatilityBreakoutStrategy(BaseStrategy):
    name = "volatility_breakout"

    def __init__(
        self,
        k: float = 0.5,
        trend_ma: int = 20,
    ) -> None:
        if not 0.0 < k < 1.0:
            raise ValueError(f"K값은 0~1 사이여야 함: {k}")
        self.k = k
        self.trend_ma = trend_ma
        self.required_lookback = trend_ma + 5

    def generate_signal(self, symbol: str, history: pd.DataFrame) -> Signal:
        if len(history) < self.required_lookback:
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                price=float(history["close"].iloc[-1]) if len(history) else 0.0,
                reason=f"데이터 부족 ({len(history)} < {self.required_lookback})",
            )

        today = history.iloc[-1]
        yesterday = history.iloc[-2]
        cur_price = float(today["close"])

        # 전일 레인지
        prev_range = float(yesterday["high"]) - float(yesterday["low"])
        # 목표가 = 당일 시가 + 전일 레인지 × K
        target_price = float(today["open"]) + prev_range * self.k

        # 추세 필터: MA 위에 있는지
        ma = history["close"].rolling(self.trend_ma).mean()
        above_trend = cur_price > float(ma.iloc[-1])

        # 돌파 확인: 현재가가 목표가 이상이고 추세 필터 통과
        breakout = cur_price >= target_price

        if breakout and above_trend:
            return Signal(
                type=SignalType.BUY,
                symbol=symbol,
                price=cur_price,
                reason=f"변동성 돌파 (목표가 {target_price:,.0f}, K={self.k}, MA{self.trend_ma} 위)",
            )

        # 이미 보유 중이면 익일 시가 매도 (봇에서 처리)
        # 여기서는 돌파 실패 시 HOLD
        return Signal(
            type=SignalType.HOLD,
            symbol=symbol,
            price=cur_price,
            reason=f"미돌파 (현재 {cur_price:,.0f} < 목표 {target_price:,.0f})"
            if not breakout
            else f"추세 필터 미통과 (MA{self.trend_ma} 아래)",
        )
