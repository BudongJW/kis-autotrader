"""포트폴리오 JSON만 빠르게 업데이트 (매 거래 실행 후).

journal.py의 전체 노트 생성과 달리, portfolio.json만 갱신.
autotrader 워크플로우에서 매 실행마다 호출.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.kis_client import KISClient
from src.bot.single_run import (
    load_universe, load_strategy_params,
    get_all_holdings, get_available_cash, get_price,
)
from src.tracker import get_summary


JOURNAL_DIR = Path("journal")
PORTFOLIO_PATH = JOURNAL_DIR / "_data" / "portfolio.json"


def main() -> None:
    if not JOURNAL_DIR.exists():
        print("  journal/ 디렉토리 없음. 스킵.")
        return

    now = datetime.now()
    client = KISClient()
    universe = load_universe()
    universe_syms = {s["symbol"] for s in universe}
    holdings_raw = get_all_holdings(client)
    cash = get_available_cash(client)
    params = load_strategy_params()
    summary = get_summary()

    holdings = []
    holdings_value = 0
    for sym, qty in holdings_raw.items():
        cur_price = get_price(client, sym)
        value = cur_price * qty
        holdings_value += value
        name = next((s["name"] for s in universe if s["symbol"] == sym), sym)
        tag = "ETF" if sym in universe_syms else "급등주"
        holdings.append({
            "symbol": sym, "name": name, "tag": tag,
            "qty": qty, "current_price": cur_price, "value": value,
        })

    total_value = cash + holdings_value

    # 기존 데이터 로드
    existing = {}
    if PORTFOLIO_PATH.exists():
        with PORTFOLIO_PATH.open("r", encoding="utf-8") as f:
            existing = json.load(f)

    # 기존 히스토리 유지, 오늘 항목만 갱신
    daily_history = existing.get("daily_history", [])
    today_str = now.strftime("%Y-%m-%d")
    today_entry = {
        "date": today_str,
        "total_value": total_value,
        "cash": cash,
        "holdings_value": holdings_value,
        "day_pnl": total_value - (daily_history[-1]["total_value"] if daily_history and daily_history[-1]["date"] != today_str else 500000),
        "cumul_pnl": total_value - 500000,
    }

    if daily_history and daily_history[-1].get("date") == today_str:
        daily_history[-1] = today_entry
    else:
        daily_history.append(today_entry)

    portfolio = {
        "updated_at": now.isoformat(),
        "initial_capital": 500000,
        "cash": cash,
        "holdings": holdings,
        "holdings_value": holdings_value,
        "total_value": total_value,
        "total_pnl": summary["pnl"],
        "total_pnl_pct": round(summary["pnl_pct"], 2),
        "total_trades": summary["total_trades"],
        "winning_trades": existing.get("winning_trades", 0),
        "losing_trades": existing.get("losing_trades", 0),
        "win_rate": existing.get("win_rate", 0),
        "daily_history": daily_history,
        "strategies": {
            "etf_breakout": {
                "name": "ETF 변동성 돌파",
                "allocation": "60%",
                "params": {"k": params.get("k", 0.5), "trend_ma": params.get("trend_ma", 20)},
                "trades": existing.get("strategies", {}).get("etf_breakout", {}).get("trades", 0),
                "pnl": existing.get("strategies", {}).get("etf_breakout", {}).get("pnl", 0),
            },
            "surge_scalp": {
                "name": "급등주 단타",
                "allocation": "40%",
                "trades": existing.get("strategies", {}).get("surge_scalp", {}).get("trades", 0),
                "pnl": existing.get("strategies", {}).get("surge_scalp", {}).get("pnl", 0),
            },
        },
    }

    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PORTFOLIO_PATH.open("w", encoding="utf-8") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)

    print(f"  [Journal] {total_value:,}원 | PnL: {summary['pnl']:+,}원 | 보유: {len(holdings)}종목")


if __name__ == "__main__":
    main()
