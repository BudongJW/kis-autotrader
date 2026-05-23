"""포트폴리오 JSON 빠른 업데이트 (매 거래 실행 후).

journal.py의 전체 노트 생성과 달리, portfolio.json만 갱신.
autotrader 워크플로우에서 매 실행마다 호출.
시장 컨텍스트도 함께 기록하여 일지 생성 시 활용.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import yaml

from src.kis_client import KISClient
from src.bot.single_run import load_universe, load_strategy_params
from src.tracker import get_summary, TRADE_LOG_PATH
from src.risk_manager import (
    load_positions, get_kelly_position_size, get_drawdown_scale,
)
from src.strategies.signal_fusion import FUSION_WEIGHTS_PATH
from src.strategies.bear_strategy import (
    BEAR_STATE_PATH, get_regime_performance,
)
from src.bot.us_session import (
    load_us_config, load_us_positions, get_us_holdings, get_us_available_cash,
)
from src.experience import _load_experience
from src.strategies.r_multiple import get_r_summary, R_LOG_PATH
from src.utils.logger import log


JOURNAL_DIR = Path("journal")
PORTFOLIO_PATH = JOURNAL_DIR / "_data" / "portfolio.json"
CONFIG_PATH = Path("configs/strategy.yaml")


def _compute_today_summary(trades: list[dict], realized: list[dict]) -> dict:
    """오늘 매매 요약. 일일 PnL·수수료·세금·최고/최악 거래 등."""
    today = datetime.now().strftime("%Y-%m-%d")

    today_trades = [t for t in trades if t.get("date") == today]
    today_realized = [r for r in realized if r.get("sell_date") == today]

    buys = [t for t in today_trades if t.get("side") == "buy"]
    sells = [t for t in today_trades if t.get("side") == "sell"]

    # 수수료·세금 (한국 주식 기준)
    # 매수: 0.015% 수수료
    # 매도: 0.015% 수수료 + 0.20% 거래세
    fee_rate = 0.00015
    tax_rate = 0.0023  # 매도 시 (증권거래세 0.20% + 농어촌특별세 0.15% 일부 등)

    buy_notional = sum(t.get("amount", 0) for t in buys)
    sell_notional = sum(t.get("amount", 0) for t in sells)
    buy_fees = round(buy_notional * fee_rate)
    sell_fees = round(sell_notional * fee_rate)
    sell_taxes = round(sell_notional * tax_rate)

    today_realized_pnl = sum(r.get("pnl", 0) for r in today_realized)
    today_realized_pnl_net = today_realized_pnl - buy_fees - sell_fees - sell_taxes

    best = max(today_realized, key=lambda r: r.get("pnl", 0)) if today_realized else None
    worst = min(today_realized, key=lambda r: r.get("pnl", 0)) if today_realized else None

    return {
        "date": today,
        "buys": len(buys),
        "sells": len(sells),
        "completed_round_trips": len(today_realized),
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "fees": buy_fees + sell_fees,
        "taxes": sell_taxes,
        "total_cost": buy_fees + sell_fees + sell_taxes,
        "realized_pnl_gross": today_realized_pnl,
        "realized_pnl_net": today_realized_pnl_net,
        "best_trade": (
            {"symbol": best["symbol"], "name": best.get("name", ""),
             "pnl": best["pnl"], "pnl_pct": best.get("pnl_pct", 0)}
            if best else None
        ),
        "worst_trade": (
            {"symbol": worst["symbol"], "name": worst.get("name", ""),
             "pnl": worst["pnl"], "pnl_pct": worst.get("pnl_pct", 0)}
            if worst else None
        ),
    }


def _load_trades() -> list[dict]:
    """trades.csv를 시간순 list of dict로 로드."""
    if not TRADE_LOG_PATH.exists():
        return []
    out = []
    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("timestamp", "")
            try:
                qty = int(row.get("qty", 0) or 0)
                price = int(row.get("price", 0) or 0)
                amount = int(row.get("amount", 0) or 0)
            except (TypeError, ValueError):
                continue
            out.append({
                "timestamp": ts,
                "date": ts[:10],
                "time": ts[11:19],
                "symbol": row.get("symbol", ""),
                "name": row.get("name", ""),
                "side": row.get("side", ""),
                "qty": qty,
                "price": price,
                "amount": amount,
            })
    return out


def _compute_realized_trades(trades: list[dict]) -> list[dict]:
    """FIFO로 buy/sell을 매칭해서 실현 거래 list 반환."""
    queues: dict[str, list[dict]] = {}
    realized: list[dict] = []

    for t in trades:
        sym = t["symbol"]
        if t["side"] == "buy":
            queues.setdefault(sym, []).append(dict(t))
        elif t["side"] == "sell":
            remaining = t["qty"]
            q = queues.get(sym, [])
            while remaining > 0 and q:
                buy = q[0]
                take = min(buy["qty"], remaining)
                if buy["price"] > 0:
                    pnl = (t["price"] - buy["price"]) * take
                    pnl_pct = round((t["price"] - buy["price"]) / buy["price"] * 100, 2)
                else:
                    pnl, pnl_pct = 0, 0
                hold_days = 0
                try:
                    if buy["timestamp"] and t["timestamp"]:
                        hold_days = (datetime.fromisoformat(t["timestamp"]) -
                                     datetime.fromisoformat(buy["timestamp"])).days
                except ValueError:
                    pass
                realized.append({
                    "symbol": sym,
                    "name": t["name"] or buy["name"] or sym,
                    "buy_date": buy["date"],
                    "buy_time": buy["time"],
                    "buy_price": buy["price"],
                    "sell_date": t["date"],
                    "sell_time": t["time"],
                    "sell_price": t["price"],
                    "qty": take,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "hold_days": hold_days,
                })
                buy["qty"] -= take
                remaining -= take
                if buy["qty"] == 0:
                    q.pop(0)
    return realized


def _load_fusion_info() -> dict:
    """융합 가중치 및 메트릭 로드."""
    if not FUSION_WEIGHTS_PATH.exists():
        return {"trained": False}
    try:
        with FUSION_WEIGHTS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "trained": data.get("trained", False),
            "weights": data.get("weights", {}),
            "brier_score": data.get("metrics", {}).get("brier_score"),
            "n_samples": data.get("metrics", {}).get("n_samples", 0),
        }
    except Exception:
        return {"trained": False}


def main() -> None:
    if not JOURNAL_DIR.exists():
        print("  journal/ 디렉토리 없음. 스킵.")
        return

    now = datetime.now()
    client = KISClient()
    universe = load_universe()
    universe_syms = {s["symbol"] for s in universe}
    params = load_strategy_params()
    summary = get_summary()
    positions = load_positions()

    # KIS API 잔고 한 번 호출로 모든 정보 추출
    balance_resp = {}
    try:
        balance_resp = client.get_balance()
    except Exception as e:
        log.error("journal_balance_failed", error=str(e))

    # output1: 개별 종목 정보 (현재가, 평가금액, 매수평균가 등 KIS가 직접 계산)
    # output2: 계좌 요약 (총평가금액, 예수금 등)
    api_holdings = {}
    if balance_resp.get("rt_cd") == "0":
        for item in balance_resp.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty > 0:
                sym = item.get("pdno", "")
                api_holdings[sym] = {
                    "qty": qty,
                    "name": item.get("prdt_name", sym),
                    "current_price": int(item.get("prpr", 0)),
                    "buy_avg_price": int(float(item.get("pchs_avg_pric", 0))),
                    "evlu_amt": int(item.get("evlu_amt", 0)),        # 평가금액
                    "evlu_pfls_amt": int(item.get("evlu_pfls_amt", 0)),  # 평가손익
                    "evlu_pfls_rt": float(item.get("evlu_pfls_rt", 0)),  # 수익률%
                }

    # output2에서 계좌 총계 (KIS가 계산한 정확한 값)
    cash = 0
    total_value_api = 0
    holdings_value_api = 0
    output2 = balance_resp.get("output2", [{}])
    if output2 and balance_resp.get("rt_cd") == "0":
        o2 = output2[0] if isinstance(output2, list) else output2
        cash = int(o2.get("dnca_tot_amt", 0))          # 예수금 총액
        total_value_api = int(o2.get("tot_evlu_amt", 0))  # 총평가금액
        holdings_value_api = int(o2.get("scts_evlu_amt", 0))  # 유가증권 평가금액

        # tot_evlu_amt가 0이면 fallback
        if total_value_api <= 0:
            total_value_api = cash + holdings_value_api

    # 보유 종목 상세 구성
    holdings = []
    holdings_value = 0
    for sym, info in api_holdings.items():
        qty = info["qty"]
        cur_price = info["current_price"]
        evlu_amt = info["evlu_amt"]
        holdings_value += evlu_amt

        # 이름: KIS API 응답 > 유니버스 매핑 > 심볼 그대로
        api_name = info["name"]
        universe_name = next((s["name"] for s in universe if s["symbol"] == sym), None)
        name = universe_name or api_name or sym

        h = {
            "symbol": sym, "name": name,
            "qty": qty, "current_price": cur_price,
            "value": evlu_amt,
            "buy_price": info["buy_avg_price"],
            "pnl": info["evlu_pfls_amt"],
            "pnl_pct": round(info["evlu_pfls_rt"], 2),
        }

        # 봇이 관리하는 포지션 정보 (ATR, hold_days 등)
        pos = positions.get(sym, {})
        if pos:
            h["atr_at_buy"] = pos.get("atr_at_buy", 0)
            h["hold_days"] = pos.get("hold_days", 0)
            h["peak_price"] = pos.get("peak_price", info["buy_avg_price"])
            atr = pos.get("atr_at_buy", 0)
            buy_p = pos.get("buy_price", info["buy_avg_price"])
            if atr > 0 and buy_p > 0:
                h["stop_price"] = round(buy_p - atr * 1.5)
                h["trailing_activate"] = round(buy_p + atr * 2.0)
            elif buy_p > 0:
                h["stop_price"] = round(buy_p * 0.97)
        else:
            # 봇이 매수하지 않은 종목 (수동 보유분)
            h["manual"] = True
            buy_p = info["buy_avg_price"]
            if buy_p > 0:
                h["stop_price"] = round(buy_p * 0.97)

        holdings.append(h)

    # 총자산: KIS API 값 우선, 없으면 직접 계산
    total_value = total_value_api if total_value_api > 0 else (cash + holdings_value)

    # 기존 데이터 로드
    existing = {}
    if PORTFOLIO_PATH.exists():
        try:
            with PORTFOLIO_PATH.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    # 토큰 실패 등으로 total_value=0이면 이전 값 보존
    if total_value <= 0 and existing.get("total_value", 0) > 0:
        log.warning("journal_zero_value_guard",
                     msg="잔고 조회 실패로 total_value=0, 이전 값 보존")
        total_value = existing["total_value"]
        cash = existing.get("cash", 0)
        holdings_value = existing.get("holdings_value", 0)
        holdings = existing.get("holdings", [])

    daily_history = existing.get("daily_history", [])
    today_str = now.strftime("%Y-%m-%d")

    # 시장 컨텍스트 읽기
    regime_info = {}
    confidence = 0.5
    overnight_signal = {}
    strong_sectors = []
    k_value = params.get("k", 0.5)
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        regime_info = cfg.get("market_regime", {})
        confidence = cfg.get("market_confidence", 0.5)
        overnight_signal = cfg.get("overnight_signal", {})
        strong_sectors = cfg.get("strong_sectors", [])
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

    # Kelly & Drawdown
    kelly_combined = 0.10
    dd_scale, dd_reason = 1.0, "기본"
    try:
        kelly_combined = get_kelly_position_size("combined")
        dd_scale, dd_reason = get_drawdown_scale()
    except Exception:
        pass

    # 융합 가중치 정보
    fusion_info = _load_fusion_info()

    # 하락장 전략 상태
    bear_info = {"regime": "BULL", "enabled": False}
    try:
        if BEAR_STATE_PATH.exists():
            with BEAR_STATE_PATH.open("r", encoding="utf-8") as f:
                bs = json.load(f)
            bear_info = {
                "enabled": True,
                "regime": bs.get("regime", "BULL"),
                "confidence": bs.get("confidence", 0),
                "sma_ratio": bs.get("sma_ratio", 0),
                "canary_bad": bs.get("canary_bad", 0),
                "canary_scores": bs.get("canary_scores", {}),
            }
            # 레짐별 성과 통계
            for r_name in ("BEAR", "CAUTION"):
                perf = get_regime_performance(r_name)
                if perf.get("sufficient_data"):
                    bear_info[f"{r_name.lower()}_stats"] = perf.get("stats", {})
    except Exception:
        pass

    # 미국장 정보
    us_info = {"enabled": False}
    try:
        us_cfg = load_us_config()
        if us_cfg.get("enabled", False):
            us_positions = load_us_positions()
            us_holdings_api = get_us_holdings(client)
            us_cash = get_us_available_cash(client)

            us_holdings_list = []
            for sym, pos in us_positions.items():
                api_data = us_holdings_api.get(sym, {})
                cur_price = api_data.get("current_price", pos.get("buy_price", 0))
                buy_price = pos.get("buy_price", 0)
                qty = api_data.get("qty", pos.get("qty", 0))
                pnl_pct = ((cur_price - buy_price) / buy_price * 100) if buy_price > 0 else 0

                us_holdings_list.append({
                    "symbol": sym,
                    "qty": qty,
                    "buy_price": buy_price,
                    "current_price": cur_price,
                    "pnl_pct": round(pnl_pct, 2),
                    "exchange": pos.get("exchange", "NASD"),
                    "asset_type": pos.get("asset_type", "us_long"),
                })

            us_info = {
                "enabled": True,
                "cash_usd": us_cash,
                "positions": len(us_positions),
                "holdings": us_holdings_list,
                "max_positions": us_cfg.get("max_positions", 2),
                "budget_pct": us_cfg.get("budget_pct", 0.40),
                "regime_linked": us_cfg.get("regime_linked", True),
            }
    except Exception:
        pass

    # 유니버스 정보
    universe_list = [{"symbol": s["symbol"], "name": s["name"]} for s in universe]

    # 오늘의 전략 결정 로그 (최근 20건)
    today_decisions = []
    try:
        all_exp = _load_experience()
        for r in reversed(all_exp):
            if r.get("date") != today_str:
                continue
            decision = {
                "time": r.get("timestamp", "")[-8:],  # HH:MM:SS
                "symbol": r.get("symbol", ""),
                "name": r.get("name", ""),
                "action": r.get("action", ""),
                "reason": r.get("reason", ""),
                "price": r.get("price", 0),
            }
            # 매수 결정에는 추가 정보
            if r.get("fusion_prob") is not None:
                decision["fusion_prob"] = r["fusion_prob"]
            if r.get("fusion_signal"):
                decision["fusion_signal"] = r["fusion_signal"]
            if r.get("ta_scores", {}).get("total") is not None:
                decision["ta_total"] = r["ta_scores"]["total"]
            if r.get("lgbm_prob") is not None:
                decision["lgbm_prob"] = r["lgbm_prob"]
            if r.get("breakout_signal") is not None:
                decision["breakout"] = r["breakout_signal"]
            if r.get("sizing_ratio") is not None:
                decision["sizing_ratio"] = r["sizing_ratio"]
            if r.get("atr_at_buy") is not None:
                decision["atr_at_buy"] = r["atr_at_buy"]
            if r.get("qty", 0) > 0:
                decision["qty"] = r["qty"]

            today_decisions.append(decision)
            if len(today_decisions) >= 20:
                break
    except Exception:
        pass

    # 실제 거래 기록 로드 + 실현 거래 페어링
    all_trades = _load_trades()
    realized = _compute_realized_trades(all_trades)
    wins = sum(1 for r in realized if r["pnl"] > 0)
    losses = sum(1 for r in realized if r["pnl"] < 0)
    win_rate = round(wins / len(realized) * 100, 1) if realized else 0.0
    realized_pnl = sum(r["pnl"] for r in realized)
    unrealized_pnl = sum(h.get("pnl", 0) for h in holdings)

    # 오늘 요약 (A2: 일일 자동 요약)
    today_summary = _compute_today_summary(all_trades, realized)

    # 누적 비용·세금 (B1+B2)
    fee_rate, tax_rate = 0.00015, 0.0023
    total_buy_notional = sum(t["amount"] for t in all_trades if t["side"] == "buy")
    total_sell_notional = sum(t["amount"] for t in all_trades if t["side"] == "sell")
    total_fees = round((total_buy_notional + total_sell_notional) * fee_rate)
    total_taxes = round(total_sell_notional * tax_rate)
    costs_summary = {
        "total_fees": total_fees,
        "total_taxes": total_taxes,
        "total_cost": total_fees + total_taxes,
        "realized_pnl_gross": realized_pnl,
        "realized_pnl_net": realized_pnl - total_fees - total_taxes,
        # 미국 ETF 양도소득세 250만원 공제 추적 (실제 한 해 누적 양도차익 필요)
        # 여기선 단순 추적용 placeholder. 정확한 추적은 매도 시 USD/KRW 환율 필요.
        "tax_exemption_remaining_krw": 2_500_000,  # 매년 1.1 리셋
    }

    # Performance metrics (computed from daily_history + realized)
    perf_metrics = {}
    if len(daily_history) >= 5:
        values = [d["total_value"] for d in daily_history]
        returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values)) if values[i-1] > 0]
        if returns:
            import numpy as np
            avg_ret = np.mean(returns)
            std_ret = np.std(returns) if len(returns) > 1 else 0.001
            sharpe = round(float(avg_ret / std_ret * np.sqrt(252)), 2) if std_ret > 0 else 0
            # Max drawdown
            peak = values[0]
            max_dd = 0
            for v in values:
                if v > peak:
                    peak = v
                dd = (v - peak) / peak
                if dd < max_dd:
                    max_dd = dd
            # Profit factor
            winning_pnl = sum(r["pnl"] for r in realized if r["pnl"] > 0)
            losing_pnl = abs(sum(r["pnl"] for r in realized if r["pnl"] < 0))
            profit_factor = round(winning_pnl / losing_pnl, 2) if losing_pnl > 0 else 0
            # Avg win/loss
            avg_win = round(np.mean([r["pnl_pct"] for r in realized if r["pnl"] > 0]), 2) if wins > 0 else 0
            avg_loss = round(np.mean([r["pnl_pct"] for r in realized if r["pnl"] < 0]), 2) if losses > 0 else 0
            perf_metrics = {
                "sharpe_ratio": sharpe,
                "max_drawdown_pct": round(max_dd * 100, 2),
                "profit_factor": profit_factor,
                "avg_win_pct": avg_win,
                "avg_loss_pct": avg_loss,
                "total_return_pct": round((values[-1] / values[0] - 1) * 100, 2) if values[0] > 0 else 0,
            }
    # R-multiple summary
    r_summary = get_r_summary()

    # SQLite 원장: 포지션 스냅샷 + 정합성 점검 (KIS 잔고 vs 원장)
    try:
        from src.safety.ledger import snapshot_positions, reconcile
        # 보유 종목을 ledger에 스냅샷
        if holdings:
            snapshot_positions(holdings, market="KR")
        # 정합성 체크
        broker_qty = {h["symbol"]: int(h.get("qty", 0)) for h in holdings}
        recon = reconcile(broker_qty, market="KR")
        recon_summary = {
            "matched": len(recon.get("matched", [])),
            "broker_only": len(recon.get("broker_only", [])),
            "ledger_only": len(recon.get("ledger_only", [])),
            "qty_mismatch": len(recon.get("qty_mismatch", [])),
        }
    except Exception as e:
        log.warning("ledger_reconcile_failed", error=str(e))
        recon_summary = {}

    # Killswitch 상태 (사이트가 표시할 수 있도록)
    try:
        from src.safety.killswitch import get_status as get_killswitch_status
        ks_status = get_killswitch_status()
    except Exception:
        ks_status = {"active": False, "mode": "off"}

    portfolio = {
        "updated_at": now.isoformat(),
        "initial_capital": 500000,
        "cash": cash,
        "holdings": holdings,
        "reconcile": recon_summary,
        "killswitch": ks_status,
        "holdings_value": holdings_value,
        "total_value": total_value,
        "total_pnl": summary["pnl"],
        "total_pnl_pct": round(summary["pnl_pct"], 2),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_trades": len(all_trades),
        "completed_trades": len(realized),
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": win_rate,
        "trades": all_trades[-50:],
        "realized": realized[-30:],
        "today_summary": today_summary,
        "costs": costs_summary,
        "performance": perf_metrics,
        "r_summary": r_summary,
        "daily_history": daily_history,

        # 시장 상태
        "market": {
            "regime": regime_info.get("trend", "unknown"),
            "hmm_state": regime_info.get("hmm_state", "unknown"),
            "confidence": round(confidence, 3),
            "volatility": regime_info.get("volatility", "unknown"),
            "vol_percentile": regime_info.get("vol_percentile", 0),
            "overnight_gap": {
                "direction": overnight_signal.get("direction", "neutral"),
                "strength": overnight_signal.get("strength", 0),
                "action": overnight_signal.get("recommended_action", "normal"),
            },
            "strong_sectors": strong_sectors,
        },

        # 전략 설정
        "strategy": {
            "name": "ETF 변동성 돌파 + 신호 융합",
            "params": {
                "k": params.get("k", 0.5),
                "trend_ma": params.get("trend_ma", 20),
            },
            "universe_count": len(universe),
            "universe": universe_list,
        },

        # 리스크 관리
        "risk": {
            "kelly_f": round(kelly_combined, 4),
            "drawdown_scale": dd_scale,
            "drawdown_reason": dd_reason,
            "stop_type": "ATR×1.5 동적",
            "trailing_type": "ATR×2.0 활성 → ATR×1.0 이탈",
        },

        # 신호 융합
        "fusion": fusion_info,

        # 하락장 전략
        "bear_strategy": bear_info,

        # 미국장 야간 매매
        "us_session": us_info,

        # 오늘의 전략 결정 로그
        "decisions": today_decisions,
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
