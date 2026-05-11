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
    """장기 횡보 후 상승 → 골든크로스 → BUY."""
    strat = GoldenCrossStrategy(short_window=5, long_window=20)
    # 25일 횡보 (100) 후 5일 상승 → 단기 평균이 장기를 위로 통과
    prices = [100.0] * 25 + list(np.linspace(101, 130, 5))
    signal = strat.generate_signal("TEST", _make_history(prices))
    assert signal.type == SignalType.BUY
    assert "골든크로스" in signal.reason


def test_dead_cross_triggers_sell():
    """장기 횡보 후 하락 → 데드크로스 → SELL."""
    strat = GoldenCrossStrategy(short_window=5, long_window=20)
    prices = [100.0] * 25 + list(np.linspace(99, 70, 5))
    signal = strat.generate_signal("TEST", _make_history(prices))
    assert signal.type == SignalType.SELL
    assert "데드크로스" in signal.reason


def test_no_cross_returns_hold():
    """완전 횡보 → HOLD."""
    strat = GoldenCrossStrategy(short_window=5, long_window=20)
    prices = [100.0] * 30
    signal = strat.generate_signal("TEST", _make_history(prices))
    assert signal.type == SignalType.HOLD
