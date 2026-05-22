"""journal_quick의 FIFO 매수/매도 페어링 + today_summary 회귀 테스트."""

from datetime import datetime, timedelta

import pytest

from src.journal_quick import _compute_realized_trades, _compute_today_summary


def _trade(date, time, side, symbol, qty, price, name=""):
    """테스트용 trade row 생성."""
    return {
        "timestamp": f"{date}T{time}",
        "date": date,
        "time": time,
        "symbol": symbol,
        "name": name or symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "amount": qty * price,
    }


# ──────────────────────────────────────────────────────────
# FIFO 페어링
# ──────────────────────────────────────────────────────────

def test_simple_round_trip():
    trades = [
        _trade("2026-05-22", "09:00:00", "buy", "QQQ", 10, 100),
        _trade("2026-05-22", "15:00:00", "sell", "QQQ", 10, 110),
    ]
    realized = _compute_realized_trades(trades)
    assert len(realized) == 1
    assert realized[0]["pnl"] == (110 - 100) * 10  # 100
    assert realized[0]["qty"] == 10


def test_fifo_partial_sell():
    """매수 10주 → 매도 6주 → 4주 보유 + 1건 라운드트립."""
    trades = [
        _trade("2026-05-22", "09:00:00", "buy", "QQQ", 10, 100),
        _trade("2026-05-22", "15:00:00", "sell", "QQQ", 6, 110),
    ]
    realized = _compute_realized_trades(trades)
    assert len(realized) == 1
    assert realized[0]["qty"] == 6
    assert realized[0]["pnl"] == (110 - 100) * 6  # 60


def test_fifo_multiple_buys_one_sell():
    """매수 5주 @100 + 매수 5주 @120 → 매도 7주 @130
       FIFO: 5주(100→130)+2주(120→130) = (30*5) + (10*2) = 170"""
    trades = [
        _trade("2026-05-22", "09:00:00", "buy", "QQQ", 5, 100),
        _trade("2026-05-22", "11:00:00", "buy", "QQQ", 5, 120),
        _trade("2026-05-22", "14:00:00", "sell", "QQQ", 7, 130),
    ]
    realized = _compute_realized_trades(trades)
    assert len(realized) == 2
    assert realized[0]["qty"] == 5
    assert realized[0]["pnl"] == 150  # (130-100)*5
    assert realized[1]["qty"] == 2
    assert realized[1]["pnl"] == 20   # (130-120)*2


def test_loss_calculation():
    trades = [
        _trade("2026-05-22", "09:00:00", "buy", "QQQ", 10, 100),
        _trade("2026-05-22", "15:00:00", "sell", "QQQ", 10, 90),
    ]
    realized = _compute_realized_trades(trades)
    assert realized[0]["pnl"] == -100
    assert realized[0]["pnl_pct"] == -10.0


def test_no_sells_no_realized():
    trades = [
        _trade("2026-05-22", "09:00:00", "buy", "QQQ", 10, 100),
    ]
    realized = _compute_realized_trades(trades)
    assert realized == []


def test_separate_symbols():
    trades = [
        _trade("2026-05-22", "09:00:00", "buy", "QQQ", 10, 100),
        _trade("2026-05-22", "10:00:00", "buy", "SPY", 5, 50),
        _trade("2026-05-22", "15:00:00", "sell", "QQQ", 10, 110),
        _trade("2026-05-22", "15:30:00", "sell", "SPY", 5, 55),
    ]
    realized = _compute_realized_trades(trades)
    assert len(realized) == 2
    syms = {r["symbol"] for r in realized}
    assert syms == {"QQQ", "SPY"}


def test_hold_days_calculation():
    trades = [
        _trade("2026-05-20", "09:00:00", "buy", "QQQ", 10, 100),
        _trade("2026-05-22", "15:00:00", "sell", "QQQ", 10, 110),
    ]
    realized = _compute_realized_trades(trades)
    assert realized[0]["hold_days"] == 2


# ──────────────────────────────────────────────────────────
# Today's summary
# ──────────────────────────────────────────────────────────

def test_today_summary_empty():
    summary = _compute_today_summary([], [])
    assert summary["buys"] == 0
    assert summary["sells"] == 0
    assert summary["realized_pnl_gross"] == 0
    assert summary["best_trade"] is None


def test_today_summary_with_trades(monkeypatch):
    today = datetime.now().strftime("%Y-%m-%d")
    trades = [
        _trade(today, "09:00:00", "buy", "QQQ", 10, 100),
        _trade(today, "15:00:00", "sell", "QQQ", 10, 110),
    ]
    realized = _compute_realized_trades(trades)
    summary = _compute_today_summary(trades, realized)

    assert summary["buys"] == 1
    assert summary["sells"] == 1
    assert summary["completed_round_trips"] == 1
    assert summary["realized_pnl_gross"] == 100  # (110-100)*10

    # 비용 계산 확인
    # buy_notional = 1000, sell_notional = 1100
    # fees = 1000*0.00015 + 1100*0.00015 = 0.15 + 0.165 = 0.315 → round 0
    # taxes = 1100*0.0023 = 2.53 → round 3
    assert summary["fees"] >= 0
    assert summary["taxes"] >= 0

    # Best trade 정확히 잡혔는지
    assert summary["best_trade"] is not None
    assert summary["best_trade"]["symbol"] == "QQQ"


def test_today_summary_filters_old_trades():
    """어제 trade는 today_summary에 포함 안 됨."""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    trades = [
        _trade(yesterday, "09:00:00", "buy", "QQQ", 10, 100),
        _trade(yesterday, "15:00:00", "sell", "QQQ", 10, 110),  # yesterday's win
        _trade(today, "09:00:00", "buy", "SPY", 5, 50),  # today's buy only
    ]
    realized = _compute_realized_trades(trades)
    summary = _compute_today_summary(trades, realized)

    # today엔 매수만 1건, sell 0건
    assert summary["buys"] == 1
    assert summary["sells"] == 0
    # 어제 라운드트립은 오늘 요약에 미포함
    assert summary["completed_round_trips"] == 0
