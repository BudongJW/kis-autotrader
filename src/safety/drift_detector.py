"""백테스트 vs 실전 결과 drift 감지 — 모델 무력화 자동 감지.

매주 일요일 실행:
  1. 최근 14일 봇 실거래 PnL 집계 (SQLite ledger 활용)
  2. 동일 기간 + 동일 universe로 백테스트 재실행
  3. 두 결과 비교: 실전 Sharpe < 백테스트 Sharpe * 0.7 면 drift 의심
  4. ledger event + portfolio.json drift_alert 필드에 기록

이게 알려주는 것:
  - 모델이 시장 변화로 무력화되는 시점
  - 슬리피지·수수료가 backtest 기대치 대비 큰 경우
  - bot 코드 버그로 실제 매매가 backtest와 다른 경우
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

from src.safety.ledger import LEDGER_PATH
from src.utils.logger import log


WINDOW_DAYS = 14
DRIFT_THRESHOLD = 0.30           # 30% 이상 괴리 = drift
MIN_TRADES_FOR_ANALYSIS = 5      # 최소 거래 수


def _fetch_actual_trades(window_days: int = WINDOW_DAYS) -> list[dict]:
    """SQLite ledger에서 최근 N일 체결 데이터."""
    if not LEDGER_PATH.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
    try:
        conn = sqlite3.connect(LEDGER_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM executions
               WHERE executed_at >= ?
               ORDER BY executed_at""",
            (cutoff,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("drift_fetch_failed", error=str(e))
        return []


def _pair_actual_round_trips(trades: list[dict]) -> list[dict]:
    """FIFO로 매수-매도 페어링. PnL list 반환."""
    queues: dict[str, list[dict]] = {}
    realized = []
    for t in trades:
        sym = t.get("symbol", "")
        if t.get("side") == "buy":
            queues.setdefault(sym, []).append(dict(t))
        elif t.get("side") == "sell":
            remaining = int(t.get("qty", 0))
            q = queues.get(sym, [])
            while remaining > 0 and q:
                buy = q[0]
                take = min(int(buy["qty"]), remaining)
                pnl = (float(t["price"]) - float(buy["price"])) * take
                realized.append({
                    "symbol": sym,
                    "buy_price": float(buy["price"]),
                    "sell_price": float(t["price"]),
                    "qty": take,
                    "pnl": pnl,
                    "pnl_pct": (
                        (float(t["price"]) - float(buy["price"])) / float(buy["price"]) * 100
                        if float(buy["price"]) > 0 else 0
                    ),
                })
                buy["qty"] = int(buy["qty"]) - take
                remaining -= take
                if int(buy["qty"]) == 0:
                    q.pop(0)
    return realized


def _compute_sharpe(returns: list[float]) -> float:
    """간단 Sharpe (일별 수익률 std 가정)."""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    if arr.std() == 0:
        return 0.0
    return float(np.sqrt(252) * arr.mean() / arr.std())


def _compute_actual_metrics(realized: list[dict]) -> dict:
    """실전 거래의 Sharpe·수익률·승률."""
    if len(realized) < MIN_TRADES_FOR_ANALYSIS:
        return {"sharpe": 0.0, "total_return": 0.0, "win_rate": 0.0,
                "num_trades": len(realized), "insufficient_data": True}
    returns = [r["pnl_pct"] / 100 for r in realized]
    wins = sum(1 for r in realized if r["pnl"] > 0)
    return {
        "sharpe": _compute_sharpe(returns),
        "total_return": sum(returns),
        "win_rate": wins / len(realized),
        "num_trades": len(realized),
        "insufficient_data": False,
    }


def _compute_backtest_metrics(window_days: int = WINDOW_DAYS) -> Optional[dict]:
    """동일 기간 백테스트 결과 (현재 전략·유니버스 기준)."""
    try:
        import yaml
        from src.backtest.runner import load_history, run_backtest
        from src.strategies.volatility_breakout import VolatilityBreakoutStrategy

        with open("configs/strategy.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        params = cfg.get("strategies", {}).get("volatility_breakout", {})
        k = params.get("k", 0.5)
        ma = params.get("trend_ma", 20)
        universe = cfg.get("universe", {}).get("default", [])
        if not universe:
            return None

        end = datetime.now()
        start = end - timedelta(days=window_days + 30)  # MA 워밍업 여유
        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")

        sharpes, returns_list, win_rates, trades_list = [], [], [], []
        for stock in universe[:5]:  # 상위 5개만
            try:
                hist = load_history(stock["symbol"], start_str, end_str)
                if len(hist) < ma + 5:
                    continue
                strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)
                result = run_backtest(strategy, hist, initial_capital=10_000_000)
                if result.num_trades < 1:
                    continue
                sharpes.append(result.sharpe)
                returns_list.append(result.total_return)
                win_rates.append(result.win_rate)
                trades_list.append(result.num_trades)
            except Exception:
                continue

        if not sharpes:
            return None

        return {
            "sharpe": float(np.mean(sharpes)),
            "total_return": float(np.mean(returns_list)),
            "win_rate": float(np.mean(win_rates)),
            "num_trades_avg": float(np.mean(trades_list)),
            "universe_count": len(sharpes),
        }
    except Exception as e:
        log.warning("drift_backtest_failed", error=str(e))
        return None


def detect_drift(window_days: int = WINDOW_DAYS) -> dict:
    """실전 vs 백테스트 drift 분석 + ledger 이벤트 기록."""
    actual_trades = _fetch_actual_trades(window_days)
    actual_realized = _pair_actual_round_trips(actual_trades)
    actual = _compute_actual_metrics(actual_realized)

    if actual.get("insufficient_data"):
        result = {
            "status": "insufficient_data",
            "actual_trades": actual["num_trades"],
            "window_days": window_days,
            "message": f"실전 거래 부족 ({actual['num_trades']} < {MIN_TRADES_FOR_ANALYSIS})",
        }
        _log_event(result, "info")
        return result

    backtest = _compute_backtest_metrics(window_days)
    if not backtest:
        result = {
            "status": "backtest_failed",
            "actual": actual,
            "message": "백테스트 비교 실패",
        }
        _log_event(result, "warning")
        return result

    # 비교: Sharpe 괴리
    bt_sharpe = backtest["sharpe"]
    actual_sharpe = actual["sharpe"]

    if bt_sharpe > 0.1:  # 백테스트가 의미 있는 양수일 때만
        drift_pct = (actual_sharpe - bt_sharpe) / abs(bt_sharpe)
    else:
        drift_pct = 0.0

    is_drift = abs(drift_pct) > DRIFT_THRESHOLD

    result = {
        "status": "drift_detected" if is_drift else "ok",
        "actual": actual,
        "backtest": backtest,
        "drift_pct": round(drift_pct, 3),
        "threshold": DRIFT_THRESHOLD,
        "window_days": window_days,
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
    }
    _log_event(result, "warning" if is_drift else "info")
    return result


def _log_event(result: dict, severity: str) -> None:
    try:
        from src.safety.ledger import log_event
        log_event("drift_check", severity, result)
    except Exception:
        pass


def main() -> None:
    """CLI 진입점 — workflow에서 호출."""
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Drift 감지 시작 (창 {WINDOW_DAYS}일)")
    result = detect_drift()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["status"] == "drift_detected":
        print(f"\n⚠️  DRIFT 감지: 실전 Sharpe {result['actual']['sharpe']:.2f} "
              f"vs 백테스트 {result['backtest']['sharpe']:.2f} "
              f"(괴리 {result['drift_pct']:+.0%})")
        # 텔레그램 알람
        try:
            from src.safety.notifier import notify_error
            notify_error(
                f"백테스트 vs 실전 drift 감지",
                context=f"실전 Sharpe={result['actual']['sharpe']:.2f}, "
                        f"백테스트 Sharpe={result['backtest']['sharpe']:.2f}, "
                        f"괴리 {result['drift_pct']:+.0%}",
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
