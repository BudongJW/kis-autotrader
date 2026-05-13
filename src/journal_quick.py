"""포트폴리오 JSON 빠른 업데이트 (매 거래 실행 후).

journal.py의 전체 노트 생성과 달리, portfolio.json만 갱신.
autotrader 워크플로우에서 매 실행마다 호출.
시장 컨텍스트도 함께 기록하여 일지 생성 시 활용.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml

from src.kis_client import KISClient
from src.bot.single_run import (
    load_universe, load_strategy_params,
    get_all_holdings, get_available_cash, get_price,
)
from src.tracker import get_summary
from src.risk_manager import load_positions, get_strategy_expectancy, get_kelly_position_size


JOURNAL_DIR = Path("journal")
PORTFOLIO_PATH = JOURNAL_DIR / "_data" / "portfolio.json"
CONFIG_PATH = Path("configs/strategy.yaml")


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
    positions = load_positions()

    holdings = []
    holdings_value = 0
    for sym, qty in holdings_raw.items():
        cur_price = get_price(client, sym)
        value = cur_price * qty
        holdings_value += value
        name = next((s["name"] for s in universe if s["symbol"] == sym), sym)
        tag = "ETF" if sym in universe_syms else "급등주"

        h = {
            "symbol": sym, "name": name, "tag": tag,
            "qty": qty, "current_price": cur_price, "value": value,
        }

        # 포지션 손익 정보
        pos = positions.get(sym, {})
        if pos:
            buy_price = pos.get("buy_price", 0)
            h["buy_price"] = buy_price
            h["pnl_pct"] = round((cur_price - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0
            h["peak_price"] = pos.get("peak_price", buy_price)

        holdings.append(h)

    total_value = cash + holdings_value

    # 기존 데이터 로드
    existing = {}
    if PORTFOLIO_PATH.exists():
        try:
            with PORTFOLIO_PATH.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    daily_history = existing.get("daily_history", [])
    today_str = now.strftime("%Y-%m-%d")

    # 시장 컨텍스트 읽기
    regime_info = {}
    confidence = 0.5
    k_value = params.get("k", 0.5)
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        regime_info = cfg.get("market_regime", {})
        confidence = cfg.get("market_confidence", 0.5)
    except Exception:
        pass

    today_entry = {
        "date": today_str,
        "total_value": total_value,
        "cash": cash,
        "holdings_value": holdings_value,
        "day_pnl": total_value - (daily_history[-1]["total_value"]
                                   if daily_history and daily_history[-1]["date"] != today_str
                                   else 500000),
        "cumul_pnl": total_value - 500000,
        "regime": regime_info.get("trend", "unknown"),
        "hmm_state": regime_info.get("hmm_state", "unknown"),
        "confidence": confidence,
        "k_value": k_value,
    }

    if daily_history and daily_history[-1].get("date") == today_str:
        daily_history[-1] = today_entry
    else:
        daily_history.append(today_entry)

    # Kelly & 기대값
    kelly_combined = 0.10
    expectancy = {"etf": 0.6, "surge": 0.4}
    try:
        kelly_combined = get_kelly_position_size("combined")
        expectancy = get_strategy_expectancy()
    except Exception:
        pass

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
        "market_snapshot": {
            "regime": regime_info.get("trend", "unknown"),
            "hmm_state": regime_info.get("hmm_state", "unknown"),
            "confidence": confidence,
            "k_value": k_value,
            "kelly_f": round(kelly_combined, 4),
            "expectancy": expectancy,
        },
        "strategies": {
            "etf_breakout": {
                "name": "ETF 변동성 돌파",
                "params": {"k": params.get("k", 0.5), "trend_ma": params.get("trend_ma", 20)},
            },
            "surge_scalp": {
                "name": "급등주 단타",
            },
        },
    }

    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PORTFOLIO_PATH.open("w", encoding="utf-8") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)

    regime_tag = regime_info.get("trend", "?")
    hmm_tag = regime_info.get("hmm_state", "?")
    print(f"  [Journal] {total_value:,}원 | PnL: {summary['pnl']:+,}원 | "
          f"보유: {len(holdings)}종목 | 레짐: {regime_tag}/{hmm_tag} | "
          f"신뢰도: {confidence:.0%} | Kelly: {kelly_combined:.0%}")


if __name__ == "__main__":
    main()
