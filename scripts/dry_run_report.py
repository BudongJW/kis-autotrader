"""dry-run 검증 리포트 — 라이브 주문 없이 '봇이 지금 무엇을, 왜 할지'를 보여준다.

봇의 실제 결정 함수(evaluate_regime / compute_current_day_plan / cost_gate 등)를
그대로 재사용하므로 라이브 로직과 드리프트가 없다. **주문 함수는 절대 호출하지
않는다(읽기 전용).** 변경 전후로 돌려 행동 차이를 검증하는 용도.

사용: python scripts/dry_run_report.py
출력: 콘솔 요약 + logs/dry_run_report.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

REPORT_PATH = Path("logs/dry_run_report.json")


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return {"_error": str(e)} if default is None else default


def build_report() -> dict:
    from src.kis_client import KISClient
    from src.bot.single_run import (
        evaluate_regime, compute_current_day_plan, day_plan_blocks_buy,
        load_leveraged_config, get_all_holdings, get_available_cash, get_price,
        load_universe, load_inverse_universe, load_config, compute_ta_score,
    )
    from src.bot.runner import fetch_recent_history
    from src.risk_manager import check_stop_loss
    from src.strategies.bear_strategy import inverse_breakout_signal, leveraged_entry_allowed
    from src.strategies.cost_gate import edge_clears_cost, atr_pct

    client = KISClient()
    cfg = load_config()
    rep: dict = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                 "mode": "DRY-RUN (읽기 전용, 주문 없음)"}

    # ── 시장 인식 ──
    rr, alloc, bear_on = _safe(lambda: evaluate_regime(client), (None, None, False))
    dp = _safe(compute_current_day_plan, {}) or {}
    blocks = day_plan_blocks_buy(dp)
    rep["market"] = {
        "regime": getattr(rr, "regime", "?") if rr else "?",
        "regime_blind": getattr(rr, "blind", False) if rr else None,
        "regime_detail": getattr(rr, "detail", "") if rr else "",
        "day_stance": dp.get("stance"),
        "day_stance_forced": dp.get("forced"),
        "briefing": dp.get("briefing"),
        "blocks_new_buys": blocks,
    }

    # ── 게이트 ──
    lc = load_leveraged_config()
    mr = cfg.get("market_regime", {}) or {}
    lev_ok, lev_reason = _safe(lambda: leveraged_entry_allowed(
        getattr(rr, "regime", "BULL") if rr else "BULL",
        mr.get("rapid_level", "NONE"), mr.get("hmm_state", "unknown"),
        float(mr.get("hmm_confidence", 0) or 0), {"leveraged": lc}), (False, "평가실패"))
    rep["gates"] = {
        "leverage_dry_run": bool(lc.get("dry_run", True)),
        "leverage_gate_allowed": bool(lev_ok),
        "leverage_gate_reason": lev_reason,
    }

    # ── 보유분 리스크(매도 판단, 실제 매도 안 함) ──
    holdings = get_all_holdings(client)
    cash = _safe(lambda: get_available_cash(client), 0)
    rep["account"] = {"cash": cash, "holdings_readable": holdings if holdings else "없음/조회실패"}
    risk = []
    for sym, qty in (holdings or {}).items():
        price = _safe(lambda: get_price(client, sym), 0)
        should_sell, reason = _safe(lambda: check_stop_loss(sym, price), (False, "평가실패"))
        risk.append({"symbol": sym, "qty": qty, "price": price,
                     "would_sell": bool(should_sell), "reason": reason})
    rep["holdings_risk"] = risk

    # ── 신규 진입 후보 (롱 ETF + 인버스): 돌파·TA·수수료게이트 ──
    params = cfg.get("strategies", {}).get("volatility_breakout", {})
    k = float(params.get("k", 0.5) or 0.5)
    ma = int(params.get("trend_ma", 20) or 20)
    candidates = []
    universe = (load_universe() or []) + (load_inverse_universe() or [])
    for stock in universe:
        sym = stock.get("symbol")
        is_inv = "inverse" in str(stock.get("type", ""))
        def _eval(sym=sym, is_inv=is_inv):
            base = {"symbol": sym, "type": "인버스" if is_inv else "롱"}
            try:
                hist = fetch_recent_history(client, sym, days=70)
                if hist is None or len(hist) < 22:
                    n = 0 if hist is None else len(hist)
                    return {**base, "decision": "스킵",
                            "why_skip": f"데이터 부족({n}봉<22) — 평가 불가"}
                sig = inverse_breakout_signal(hist, k=k, trend_ma=ma)
                ta = compute_ta_score(hist)
                price = int(sig.get("price", 0) or 0)
                h = hist.tail(15)
                em = atr_pct(float((h["high"] - h["low"]).mean()), price)
                fee_ok, _ = edge_clears_cost(em, "KR")
                ta_floor = 10 if is_inv else 0
                decision = "진입가능" if (sig.get("breakout") and ta.total >= ta_floor and fee_ok) else "스킵"
                why = []
                if not sig.get("breakout"):
                    why.append("미돌파")
                if ta.total < ta_floor:
                    why.append(f"TA부족({ta.total:+.0f})")
                if not fee_ok:
                    why.append("수수료게이트")
                return {**base, "decision": decision, "breakout": bool(sig.get("breakout")),
                        "ta": round(ta.total, 1), "atr_pct": round(em * 100, 2),
                        "fee_gate_ok": fee_ok, "why_skip": " · ".join(why) or "-"}
            except Exception as e:
                return {**base, "decision": "평가실패", "why_skip": f"오류: {e}"}
        candidates.append(_eval())
    rep["entry_candidates"] = candidates

    # ── 갭업 회복 진입 평가 (개장 윈도 한정, 라이브는 enabled 플래그로 통제) ──
    from src.bot.single_run import get_quote, _now
    from src.strategies.gap_recovery import gap_recovery_signal
    gr_cfg = cfg.get("gap_recovery", {}) or {}
    overnight = cfg.get("overnight_signal", {}) or {}
    gr_action = overnight.get("recommended_action", "normal")
    now_hhmm = _safe(lambda: _now().strftime("%H:%M"), "?")
    gr_regime = getattr(rr, "regime", "?") if rr else "?"
    gr_blind = bool(getattr(rr, "blind", False)) if rr else False
    gr_eval = []
    for stock in (load_universe() or []):
        sym = stock.get("symbol"); nm = stock.get("name", sym)
        q = _safe(lambda sym=sym: get_quote(client, sym), {}) or {}
        sig = _safe(lambda q=q: gap_recovery_signal(
            prev_close=q.get("prev_close", 0), today_open=q.get("open", 0),
            cur_price=q.get("price", 0), now_hhmm=now_hhmm,
            overnight_action=gr_action, regime=gr_regime, blind=gr_blind, cfg=gr_cfg))
        if sig is None or isinstance(sig, dict):
            gr_eval.append({"symbol": sym, "name": nm, "decision": "평가실패",
                            "reason": (sig or {}).get("_error", "평가불가")})
            continue
        gr_eval.append({
            "symbol": sym, "name": nm,
            "decision": "진입가능" if sig.is_buy else "스킵",
            "gap_open_pct": round(sig.gap_open_pct, 2),
            "intraday_pct": round(sig.intraday_pct, 2),
            "in_window": sig.in_window, "reason": sig.reason})
    rep["gap_recovery"] = {
        "enabled_live": bool(gr_cfg.get("enabled", False)),
        "now_kst": now_hhmm,
        "window": f"{gr_cfg.get('window_start_kst', '09:00')}~{gr_cfg.get('window_end_kst', '09:20')}",
        "candidates": gr_eval,
    }

    # ── US 진입 후보 (돌파·TA·수수료게이트·재진입쿨다운) ──
    us_candidates = []
    try:
        from src.bot.us_session import load_us_config, load_us_positions, fetch_us_history
        from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
        from src.strategies.cost_gate import recently_force_closed
        from src.merge_trades import _read as _read_trades
        from datetime import datetime as _dt
        ucfg = load_us_config()
        us_pos = load_us_positions()
        sc = ucfg.get("strategy", {})
        uk, uma = sc.get("k", 0.5), sc.get("trend_ma", 20)
        ta_min = sc.get("ta_min_score", 15)
        cooldown = int(sc.get("reentry_cooldown_days", 2) or 0)
        sells = [t for t in _read_trades("logs/trades.csv") if t.get("side") == "sell"]
        today = _dt.now().strftime("%Y-%m-%d")
        ustrat = VolatilityBreakoutStrategy(k=uk, trend_ma=uma)
        for stock in (ucfg.get("universe") or []):
            sym = stock.get("symbol"); exch = stock.get("exchange", "NASD")
            base = {"symbol": sym, "type": stock.get("type", "long")}
            try:
                if sym in us_pos:
                    us_candidates.append({**base, "decision": "보유중", "why_skip": "-"})
                    continue
                if cooldown and recently_force_closed(sym, sells, today, cooldown):
                    us_candidates.append({**base, "decision": "스킵",
                                          "why_skip": f"재진입쿨다운({cooldown}일, churn방지)"})
                    continue
                hist = fetch_us_history(client, sym, exchange=exch)
                sig = ustrat.generate_signal(sym, hist)
                ta = compute_ta_score(hist)
                price = float(sig.price)
                h = hist.tail(15)
                em = atr_pct(float((h["high"] - h["low"]).mean()), price)
                fee_ok, _ = edge_clears_cost(em, "US")
                bo = (sig.type.value == "BUY")
                decision = "진입가능" if (bo and ta.total >= ta_min and fee_ok) else "스킵"
                why = []
                if not bo: why.append("미돌파")
                if ta.total < ta_min: why.append(f"TA부족({ta.total:+.0f})")
                if not fee_ok: why.append("수수료게이트")
                us_candidates.append({**base, "decision": decision, "breakout": bo,
                                      "ta": round(ta.total, 1), "atr_pct": round(em * 100, 2),
                                      "why_skip": " · ".join(why) or "-"})
            except Exception as e:
                us_candidates.append({**base, "decision": "평가실패", "why_skip": f"오류: {e}"})
    except Exception as e:
        us_candidates = [{"symbol": "(US 평가 불가)", "decision": "평가실패", "why_skip": str(e)}]
    rep["us_entry_candidates"] = us_candidates

    # ── 요약 ──
    would_buy = [c["symbol"] for c in candidates if c.get("decision") == "진입가능"]
    would_sell = [r["symbol"] for r in risk if r.get("would_sell")]
    us_would_buy = [c["symbol"] for c in us_candidates if c.get("decision") == "진입가능"]
    rep["us_summary"] = {"would_buy": us_would_buy}
    rep["summary"] = {
        "would_buy": would_buy,
        "would_sell": would_sell,
        "new_buys_blocked_by_stance": blocks,
        "verdict": ("신규매수 차단(방어)" if blocks else
                    (f"진입가능 {len(would_buy)}종목" if would_buy else "진입 조건 미충족 — 현금/방어 유지")),
    }
    return rep


def print_report(rep: dict) -> None:
    m, g, s = rep.get("market", {}), rep.get("gates", {}), rep.get("summary", {})
    print(f"\n{'='*60}\n  DRY-RUN 검증 리포트 ({rep.get('generated_at')})  [주문 없음]\n{'='*60}")
    print(f"레짐: {m.get('regime')} (blind={m.get('regime_blind')}) | "
          f"당일스탠스: {m.get('day_stance')} ({'자율' if not m.get('day_stance_forced') else '수동'})")
    print(f"브리핑: {m.get('briefing')}")
    print(f"신규매수 차단: {m.get('blocks_new_buys')} | "
          f"레버리지: {'dry-run' if g.get('leverage_dry_run') else 'LIVE'} / "
          f"{'허용' if g.get('leverage_gate_allowed') else '차단'} ({g.get('leverage_gate_reason')})")
    print(f"예수금: {rep.get('account',{}).get('cash'):,} | 보유: {rep.get('account',{}).get('holdings_readable')}")
    print("\n[보유분 리스크 판단]")
    for r in rep.get("holdings_risk", []) or [["없음"]]:
        if isinstance(r, dict):
            print(f"  {r['symbol']} {r.get('qty')}주 @{r.get('price'):,} → "
                  f"{'매도' if r.get('would_sell') else '보유'} ({r.get('reason')})")
    print("\n[신규 진입 후보 — KR]")
    for c in rep.get("entry_candidates", []):
        if isinstance(c, dict):
            print(f"  {c.get('symbol')} [{c.get('type')}] {c.get('decision')} "
                  f"(돌파={c.get('breakout')}, TA={c.get('ta')}, ATR%={c.get('atr_pct')}) "
                  f"{c.get('why_skip')}")
    print("\n[신규 진입 후보 — US (다음 야간 세션)]")
    for c in rep.get("us_entry_candidates", []):
        if isinstance(c, dict):
            print(f"  {c.get('symbol')} [{c.get('type')}] {c.get('decision')} "
                  f"(돌파={c.get('breakout')}, TA={c.get('ta')}, ATR%={c.get('atr_pct')}) "
                  f"{c.get('why_skip')}")
    gr = rep.get("gap_recovery", {})
    if gr:
        print(f"\n[갭업 회복 진입 — 개장윈도 {gr.get('window')} 한정 | "
              f"라이브:{'ON' if gr.get('enabled_live') else 'OFF(검증중)'} | 현재 {gr.get('now_kst')}]")
        for c in gr.get("candidates", []):
            if isinstance(c, dict):
                print(f"  {c.get('symbol')} {c.get('name','')} {c.get('decision')} "
                      f"(시가갭={c.get('gap_open_pct')}%, 시가대비={c.get('intraday_pct')}%, "
                      f"윈도내={c.get('in_window')}) {c.get('reason')}")
    print(f"\n▶ 판정: {s.get('verdict')}")
    print(f"  매수예정: {s.get('would_buy') or '없음'} | 매도예정: {s.get('would_sell') or '없음'}")
    print(f"{'='*60}\n")


def main() -> None:
    rep = build_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(rep)
    print(f"리포트 저장: {REPORT_PATH}")


if __name__ == "__main__":
    main()
