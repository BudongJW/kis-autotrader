"""R배수(R-Multiple) 추적 — Van Tharp 방식 거래 품질 측정.

R = (수익 또는 손실) / 초기 리스크(1R)
- 1R = 매수 시 설정한 손절 거리 (ATR×2.0 또는 고정 3%)
- R > 0: 수익 거래, R < 0: 손실 거래
- 추세추종 시스템은 평균 R이 2.0+ 이어야 양호
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.risk_manager import load_positions
from src.utils.logger import log

R_LOG_PATH = Path("logs/r_multiples.json")


def compute_r_multiple(symbol: str, sell_price: float) -> dict | None:
    """매도 시 R배수 계산.

    Returns:
        {"symbol": str, "r_multiple": float, "pnl_pct": float, ...} or None
    """
    positions = load_positions()
    pos = positions.get(symbol)
    if not pos:
        return None

    buy_price = pos.get("buy_price", 0)
    if buy_price <= 0:
        return None

    initial_risk = pos.get("initial_risk", 0)
    if initial_risk <= 0:
        initial_risk = buy_price * 0.03

    pnl = sell_price - buy_price
    r = pnl / initial_risk if initial_risk > 0 else 0

    return {
        "symbol": symbol,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "buy_price": buy_price,
        "sell_price": int(sell_price),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / buy_price, 4),
        "initial_risk": round(initial_risk, 2),
        "r_multiple": round(r, 2),
        "hold_days": pos.get("hold_days", 0),
        "pyramid_count": pos.get("pyramid_count", 0),
        "asset_type": pos.get("asset_type", "long"),
    }


def log_r_multiple(symbol: str, sell_price: float) -> float | None:
    """R배수를 계산하고 로그에 저장. R값 반환."""
    result = compute_r_multiple(symbol, sell_price)
    if not result:
        return None

    R_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"trades": [], "summary": {}}
    if R_LOG_PATH.exists():
        try:
            data = json.loads(R_LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    trades = data.get("trades", [])
    trades.append(result)
    if len(trades) > 500:
        trades = trades[-500:]
    data["trades"] = trades

    rs = [t["r_multiple"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    data["summary"] = {
        "total_trades": len(rs),
        "avg_r": round(sum(rs) / len(rs), 2) if rs else 0,
        "median_r": round(sorted(rs)[len(rs) // 2], 2) if rs else 0,
        "win_rate": round(len(wins) / len(rs), 3) if rs else 0,
        "avg_win_r": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss_r": round(sum(losses) / len(losses), 2) if losses else 0,
        "expectancy_r": round(
            (len(wins) / len(rs) * (sum(wins) / len(wins) if wins else 0)
             + len(losses) / len(rs) * (sum(losses) / len(losses) if losses else 0)),
            2
        ) if rs else 0,
        "best_r": round(max(rs), 2) if rs else 0,
        "worst_r": round(min(rs), 2) if rs else 0,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    R_LOG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    r_val = result["r_multiple"]
    log.info("r_multiple", symbol=symbol, r=r_val,
             pnl_pct=f"{result['pnl_pct']:+.2%}")
    return r_val


def get_r_summary() -> dict:
    """R배수 누적 요약 반환."""
    if not R_LOG_PATH.exists():
        return {}
    try:
        data = json.loads(R_LOG_PATH.read_text(encoding="utf-8"))
        return data.get("summary", {})
    except Exception:
        return {}
