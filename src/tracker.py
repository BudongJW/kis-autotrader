"""성과 추적기 — 모든 거래 기록 + 누적 수익률 계산.

거래 기록을 CSV에 저장하고, 주간 성과 리포트를 생성한다.
GitHub Actions에서 artifact로 보관하거나 커밋으로 저장 가능.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

TRADE_LOG_PATH = Path("logs/trades.csv")
FIELDS = ["timestamp", "symbol", "name", "side", "qty", "price", "amount", "balance_after"]


def _ensure_file() -> None:
    TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TRADE_LOG_PATH.exists():
        with TRADE_LOG_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDS)


def log_trade(
    symbol: str,
    name: str,
    side: str,
    qty: int,
    price: float,
    balance_after: float = 0,
) -> None:
    """거래 1건을 CSV에 기록."""
    _ensure_file()
    with TRADE_LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now().isoformat(timespec="seconds"),
            symbol, name, side, qty, int(price),
            int(qty * price), int(balance_after),
        ])


def get_summary() -> dict:
    """누적 성과 요약 반환."""
    _ensure_file()
    buys = []
    sells = []
    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            amount = int(row["amount"])
            if row["side"] == "buy":
                buys.append(amount)
            elif row["side"] == "sell":
                sells.append(amount)

    total_invested = sum(buys)
    total_returned = sum(sells)
    total_trades = len(buys) + len(sells)
    pnl = total_returned - total_invested

    return {
        "total_trades": total_trades,
        "total_invested": total_invested,
        "total_returned": total_returned,
        "pnl": pnl,
        "pnl_pct": (pnl / total_invested * 100) if total_invested > 0 else 0,
    }
