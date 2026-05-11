"""골든크로스 전략 단위 테스트."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategies.base import SignalType
from src.strategies.golden_cross import GoldenCrossStrategy


def _make_history(close_prices: list[float]) -> pd.DataFrame:
    n = len(close_prices)
    return pd.DataFrame(
        {
            "open": close_prices,
            "high": close_prices,
            "low": close_prices,
            "close": close_prices,
            "volume": [1_000_000] * n,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


def test_short_window_must_be_less_than_long():
    with pytest.raises(ValueError):
        GoldenCrossStrategy(short_window=20, long_window=5)


def test_hold_when_insufficient_history():
    strat = GoldenCrossStrategy(short_window=5, long_window=20)
    history = _make_history([100.0] * 10)
    signal = strat.generate_signal("TEST", history)
    assert signal.type == SignalType.HOLD


def test_golden_cross_triggers_buy():
    """하락 후 반등 → 골든크로스 → BUY.

    MA5 < MA20 상태에서 마지막 캔들에서 MA5가 MA20을 상향 돌파해야 한다.
    """
    strat = GoldenCrossStrategy(short_window=5, long_window=20)
    # 15일 횡보(100) + 5일 하락(95→89) → MA5 < MA20 상태
    # 마지막 5일 급반등(89→115) → 마지막 캔들에서 MA5가 MA20 돌파
    prices = [100]*15 + [95, 93, 91, 90, 89] + [89, 90, 95, 102, 115]
    signal = strat.generate_signal("TEST", _make_history(prices))
    assert signal.type == SignalType.BUY
    assert "골든크로스" in signal.reason


def test_dead_cross_triggers_sell():
    """상승 후 하락 → 데드크로스 → SELL.

    MA5 > MA20 상태에서 마지막 캔들에서 MA5가 MA20을 하향 돌파해야 한다.
    """
    strat = GoldenCrossStrategy(short_window=5, long_window=20)
    # 15일 횡보(100) + 5일 상승(105→111) → MA5 > MA20 상태
    # 마지막 5일 급락(111→85) → 마지막 캔들에서 MA5가 MA20 아래로
    prices = [100]*15 + [105, 107, 109, 110, 111] + [111, 110, 105, 98, 85]
    signal = strat.generate_signal("TEST", _make_history(prices))
    assert signal.type == SignalType.SELL
    assert "데드크로스" in signal.reason


def test_no_cross_returns_hold():
    """완전 횡보 → HOLD."""
    strat = GoldenCrossStrategy(short_window=5, long_window=20)
    prices = [100.0] * 30
    signal = strat.generate_signal("TEST", _make_history(prices))
    assert signal.type == SignalType.HOLD
