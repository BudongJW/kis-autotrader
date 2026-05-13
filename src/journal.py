"""투자 일지 생성기 — 시장 상황 대응 역량 강화를 위한 상세 기록.

매일 장 마감 후 실행. 모델이 시장 상황별 의사결정을 학습할 수 있도록
가능한 모든 컨텍스트를 기록한다:

  1. 시장 환경 (레짐, HMM 상태, 터뷸런스, 변동성)
  2. 섹터 동향 (모멘텀, 거래량 이상)
  3. 전략 판단 근거 (TA 점수, LGBM 예측, 돌파 신호)
  4. 실행 품질 (TWAP 슬리피지, 체결 가격)
  5. 리스크 관리 이벤트 (손절, 추적손절, ROI 청산)
  6. 포지션 사이징 (Kelly, 기대값, 신뢰도 조정)
  7. 자기 평가 (예측 vs 결과, 지표 적중률)
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import yaml

from src.config import settings
from src.kis_client import KISClient
from src.bot.runner import fetch_recent_history
from src.bot.single_run import (
    load_universe, load_strategy_params, load_config as load_strategy_config,
    get_all_holdings, get_available_cash, get_price,
)
from src.tracker import get_summary, TRADE_LOG_PATH
from src.risk_manager import load_positions, get_strategy_expectancy, get_kelly_position_size
from src.market_learner import (
    load_config, load_market_log, load_ta_accuracy,
    analyze_sector_momentum, analyze_market_breadth,
)
from src.strategies.ta_composite import compute_ta_score, DEFAULT_WEIGHTS
from src.utils.logger import log


JOURNAL_DIR = Path("journal")
PORTFOLIO_PATH = JOURNAL_DIR / "_data" / "portfolio.json"
POSTS_DIR = JOURNAL_DIR / "_posts"
SLIPPAGE_PATH = Path("logs/slippage.json")
LGBM_FEATURES_PATH = Path("logs/lgbm_features.json")


def get_todays_trades() -> list[dict]:
    """오늘 날짜의 거래 내역을 반환."""
    today = datetime.now().strftime("%Y-%m-%d")
    trades = []
    if not TRADE_LOG_PATH.exists():
        return trades
    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("timestamp", "").startswith(today):
                trades.append(row)
    return trades


def get_todays_slippage() -> list[dict]:
    """오늘의 TWAP 슬리피지 데이터."""
    today = datetime.now().strftime("%Y-%m-%d")
    if not SLIPPAGE_PATH.exists():
        return []
    try:
        with SLIPPAGE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return [d for d in data if d.get("timestamp", "").startswith(today)]
    except Exception:
        return []


def _collect_market_context(client: KISClient) -> dict:
    """현재 시장 상황의 모든 컨텍스트를 수집."""
    ctx = {}

    # strategy.yaml 에서 시장 레짐 정보
    try:
        cfg = load_config()
        regime = cfg.get("market_regime", {})
        ctx["regime"] = {
            "trend": regime.get("trend", "unknown"),
            "volatility": regime.get("volatility", "unknown"),
            "trend_score": regime.get("trend_score", 0),
            "vol_percentile": regime.get("vol_percentile", 0),
            "hmm_state": regime.get("hmm_state", "unknown"),
            "hmm_confidence": regime.get("hmm_confidence", 0),
            "hmm_transition": regime.get("hmm_transition", {}),
        }
        ctx["confidence"] = cfg.get("market_confidence", 0.5)
        ctx["strong_sectors"] = cfg.get("strong_sectors", [])
        ctx["k_value"] = cfg.get("strategies", {}).get("volatility_breakout", {}).get("k", 0.5)
        ctx["ta_weights"] = cfg.get("strategies", {}).get("ta_weights", DEFAULT_WEIGHTS)
    except Exception as e:
        ctx["regime"] = {"error": str(e)}
        ctx["confidence"] = 0.5

    # 섹터 모멘텀
    try:
        ctx["sectors"] = analyze_sector_momentum(client)
    except Exception:
        ctx["sectors"] = {}

    # 시장 건강도
    try:
        ctx["breadth"] = analyze_market_breadth(client)
    except Exception:
        ctx["breadth"] = {}

    # 터뷸런스
    try:
        from src.risk_manager import check_turbulence
        is_turb, turb_reason = check_turbulence(client)
        ctx["turbulence"] = {"is_turbulent": is_turb, "reason": turb_reason}
    except Exception:
        ctx["turbulence"] = {"is_turbulent": False, "reason": "확인 불가"}

    # Kelly & 기대값
    try:
        ctx["kelly"] = {
            "combined": round(get_kelly_position_size("combined"), 4),
            "etf": round(get_kelly_position_size("etf"), 4),
            "surge": round(get_kelly_position_size("surge"), 4),
        }
        ctx["expectancy"] = get_strategy_expectancy()
    except Exception:
        ctx["kelly"] = {}
        ctx["expectancy"] = {}

    # LGBM 모델 상태
    if LGBM_FEATURES_PATH.exists():
        try:
            with LGBM_FEATURES_PATH.open("r", encoding="utf-8") as f:
                ctx["lgbm"] = json.load(f)
        except Exception:
            ctx["lgbm"] = {}
    else:
        ctx["lgbm"] = {}

    # TA 적중률
    ctx["ta_accuracy"] = load_ta_accuracy()

    # 시장 학습 로그 (최근 5일)
    try:
        market_log = load_market_log()
        ctx["market_history_recent"] = market_log[-5:] if market_log else []
    except Exception:
        ctx["market_history_recent"] = []

    return ctx


def _collect_holding_analysis(client: KISClient, holdings_raw: dict,
                              universe: list[dict]) -> list[dict]:
    """보유 종목별 상세 TA 분석."""
    universe_syms = {s["symbol"] for s in universe}
    results = []
    positions = load_positions()

    for sym, qty in holdings_raw.items():
        cur_price = get_price(client, sym)
        name = next((s["name"] for s in universe if s["symbol"] == sym), sym)
        tag = "ETF" if sym in universe_syms else "급등주"

        entry = {
            "symbol": sym,
            "name": name,
            "tag": tag,
            "qty": qty,
            "current_price": cur_price,
            "value": cur_price * qty,
        }

        # 포지션 정보
        pos = positions.get(sym, {})
        if pos:
            buy_price = pos.get("buy_price", 0)
            entry["buy_price"] = buy_price
            entry["peak_price"] = pos.get("peak_price", buy_price)
            entry["pnl_pct"] = round((cur_price - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0
            entry["buy_time"] = pos.get("buy_time", "")

        # TA 분석
        try:
            hist = fetch_recent_history(client, sym, days=70)
            ta = compute_ta_score(hist)
            entry["ta_score"] = ta.total
            entry["ta_signal"] = ta.signal
            entry["ta_detail"] = ta.detail
        except Exception:
            entry["ta_score"] = 0
            entry["ta_signal"] = "N/A"

        results.append(entry)

    return results


def build_portfolio_json(client: KISClient) -> dict:
    """포트폴리오 상태 + 시장 컨텍스트를 JSON으로 생성."""
    universe = load_universe()
    universe_syms = {s["symbol"] for s in universe}
    holdings_raw = get_all_holdings(client)
    cash = get_available_cash(client)
    params = load_strategy_params()
    summary = get_summary()

    # 보유 종목 상세
    holdings = _collect_holding_analysis(client, holdings_raw, universe)
    holdings_value = sum(h["value"] for h in holdings)
    total_value = cash + holdings_value

    # 기존 히스토리 로드
    existing = {}
    if PORTFOLIO_PATH.exists():
        try:
            with PORTFOLIO_PATH.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    daily_history = existing.get("daily_history", [])
    today_str = datetime.now().strftime("%Y-%m-%d")
    prev_value = daily_history[-1]["total_value"] if daily_history else 500000
    day_pnl = total_value - prev_value
    cumul_pnl = total_value - 500000

    # 시장 컨텍스트 수집
    market_ctx = _collect_market_context(client)

    today_entry = {
        "date": today_str,
        "total_value": total_value,
        "cash": cash,
        "holdings_value": holdings_value,
        "day_pnl": day_pnl,
        "cumul_pnl": cumul_pnl,
        "market_regime": market_ctx.get("regime", {}).get("trend", "unknown"),
        "hmm_state": market_ctx.get("regime", {}).get("hmm_state", "unknown"),
        "confidence": market_ctx.get("confidence", 0.5),
        "k_value": market_ctx.get("k_value", 0.5),
    }

    if not daily_history or daily_history[-1].get("date") != today_str:
        daily_history.append(today_entry)
    else:
        daily_history[-1] = today_entry

    return {
        "updated_at": datetime.now().isoformat(),
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
        "market_context": market_ctx,
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


def generate_daily_note(portfolio: dict) -> str:
    """시장 대응 학습용 상세 일일 투자 노트 생성."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    trades = get_todays_trades()
    slippage = get_todays_slippage()
    history = portfolio.get("daily_history", [])
    today_entry = history[-1] if history else {}
    market_ctx = portfolio.get("market_context", {})

    day_pnl = today_entry.get("day_pnl", 0)
    total_value = portfolio["total_value"]
    pnl_pct = round(day_pnl / (total_value - day_pnl) * 100, 2) if (total_value - day_pnl) > 0 else 0

    regime = market_ctx.get("regime", {})
    confidence = market_ctx.get("confidence", 0.5)

    lines = []

    # ── Front Matter ──
    lines.extend([
        "---",
        f'title: "{today} 투자 일지"',
        f"date: {now.strftime('%Y-%m-%d %H:%M:%S')} +0900",
        f"pnl: {pnl_pct}",
        f"regime: {regime.get('trend', 'unknown')}",
        f"hmm_state: {regime.get('hmm_state', 'unknown')}",
        f"confidence: {confidence}",
        f"trades_count: {len(trades)}",
        "---",
        "",
    ])

    # ── 1. 시장 환경 분석 ──
    lines.append("## 1. 시장 환경")
    lines.append("")

    # 레짐 분석
    lines.append("### 레짐 분석")
    lines.append("")
    lines.append(f"| 지표 | 값 | 해석 |")
    lines.append("|---|---|---|")
    lines.append(f"| 선형 추세 | {regime.get('trend', '?')} | 추세 점수 {regime.get('trend_score', 0):+.3f} |")
    lines.append(f"| 변동성 | {regime.get('volatility', '?')} | 백분위 {regime.get('vol_percentile', 0):.1f}% |")
    lines.append(f"| HMM 상태 | {regime.get('hmm_state', '?')} | 신뢰도 {regime.get('hmm_confidence', 0):.0%} |")
    lines.append(f"| 시장 신뢰도 | {confidence:.0%} | 매수 수량 조절 기준 |")
    lines.append(f"| 적용 K값 | {market_ctx.get('k_value', 0.5)} | 돌파 진입 기준 |")
    lines.append("")

    # HMM 전환 확률
    hmm_trans = regime.get("hmm_transition", {})
    if hmm_trans:
        lines.append("**HMM 전환 확률 (현재 상태 →)**")
        lines.append("")
        for state, prob in hmm_trans.items():
            bar = "█" * int(prob * 20) + "░" * (20 - int(prob * 20))
            lines.append(f"- {state}: {bar} {prob:.0%}")
        lines.append("")

    # 터뷸런스
    turb = market_ctx.get("turbulence", {})
    if turb.get("is_turbulent"):
        lines.append(f"> **터뷸런스 감지**: {turb.get('reason', '')}  ")
        lines.append(f"> 신규 매수가 차단되었습니다.")
        lines.append("")
    else:
        lines.append(f"터뷸런스: {turb.get('reason', '정상')}")
        lines.append("")

    # ── 2. 섹터 동향 ──
    lines.append("### 섹터 모멘텀")
    lines.append("")
    sectors = market_ctx.get("sectors", {})
    if sectors:
        lines.append("| 섹터 | 5일 수익 | 20일 수익 | 거래량비 | 모멘텀 |")
        lines.append("|---|---|---|---|---|")
        for name, data in sorted(sectors.items(), key=lambda x: x[1].get("ret_5d", 0), reverse=True):
            m = data.get("momentum", "?")
            icon = {"strong": "🔥", "positive": "📈", "weak": "📉", "negative": "⚠️"}.get(m, "")
            lines.append(
                f"| {name} | {data.get('ret_5d', 0):+.1f}% | {data.get('ret_20d', 0):+.1f}% "
                f"| {data.get('vol_ratio', 1.0):.1f}x | {icon} {m} |"
            )
        lines.append("")

    strong = market_ctx.get("strong_sectors", [])
    if strong:
        lines.append(f"**강세 섹터**: {', '.join(strong)}")
        lines.append("")

    # 시장 건강도
    breadth = market_ctx.get("breadth", {})
    if breadth:
        kospi = breadth.get("kospi_5d")
        kosdaq = breadth.get("kosdaq_5d")
        lines.append("### 시장 건강도")
        lines.append("")
        if isinstance(kospi, (int, float)):
            lines.append(f"- KOSPI 5일: {kospi:+.1f}%")
        if isinstance(kosdaq, (int, float)):
            lines.append(f"- KOSDAQ 5일: {kosdaq:+.1f}%")
        lines.append(f"- 건강도 판정: **{breadth.get('health', '?')}**")
        spread = breadth.get("spread")
        if isinstance(spread, (int, float)):
            lines.append(f"- KOSPI-KOSDAQ 괴리: {spread:.1f}%p")
        lines.append("")

    # ── 3. 포지션 사이징 판단 ──
    lines.append("## 2. 포지션 사이징")
    lines.append("")
    kelly = market_ctx.get("kelly", {})
    expectancy = market_ctx.get("expectancy", {})
    if kelly:
        lines.append("| 항목 | 값 |")
        lines.append("|---|---|")
        lines.append(f"| Kelly (전체) | {kelly.get('combined', 0):.1%} |")
        lines.append(f"| Kelly (ETF) | {kelly.get('etf', 0):.1%} |")
        lines.append(f"| Kelly (급등주) | {kelly.get('surge', 0):.1%} |")
        lines.append(f"| 기대값 배분 (ETF) | {expectancy.get('etf', 0.6):.0%} |")
        lines.append(f"| 기대값 배분 (급등주) | {expectancy.get('surge', 0.4):.0%} |")
        lines.append(f"| 신뢰도 축소 | x{max(0.3, confidence):.0%} |")
        lines.append("")

    # ── 4. 거래 내역 ──
    lines.append("## 3. 거래 내역")
    lines.append("")
    if trades:
        lines.append("| 시각 | 종목 | 매매 | 수량 | 가격 | 금액 |")
        lines.append("|---|---|---|---|---|---|")
        for t in trades:
            ts = t.get("timestamp", "")[-8:]
            side_kr = "매수" if t["side"] == "buy" else "매도"
            lines.append(
                f"| {ts} | {t.get('name', t['symbol'])} | {side_kr} "
                f"| {t['qty']}주 | {int(t['price']):,}원 | {int(t['amount']):,}원 |"
            )
        lines.append("")

        buy_trades = [t for t in trades if t["side"] == "buy"]
        sell_trades = [t for t in trades if t["side"] == "sell"]
        lines.append(f"매수 {len(buy_trades)}건 / 매도 {len(sell_trades)}건")
        lines.append("")
    else:
        lines.append("거래 없음 (돌파/급등 신호 미발생 또는 장 휴일)")
        lines.append("")

    # ── 5. TWAP 실행 품질 ──
    if slippage:
        lines.append("### TWAP 실행 품질")
        lines.append("")
        lines.append("| 종목 | 방향 | 수량 | 시그널가 | 평균체결가 | 슬리피지 | 트랜치 |")
        lines.append("|---|---|---|---|---|---|---|")
        for s in slippage:
            slip_bps = s.get("slippage_bps", 0)
            quality = "양호" if abs(slip_bps) < 5 else ("주의" if abs(slip_bps) < 15 else "불량")
            lines.append(
                f"| {s.get('name', s.get('symbol', '?'))} "
                f"| {s.get('side', '?')} "
                f"| {s.get('total_qty', 0)}주 "
                f"| {s.get('signal_price', 0):,}원 "
                f"| {s.get('avg_fill_price', 0):,}원 "
                f"| {slip_bps:+.1f}bps ({quality}) "
                f"| {s.get('num_tranches', 1)}분할 |"
            )
        lines.append("")

        avg_slip = sum(s.get("slippage_bps", 0) for s in slippage) / len(slippage)
        lines.append(f"평균 슬리피지: **{avg_slip:+.1f}bps**")
        lines.append("")

    # ── 6. 보유 종목 분석 ──
    holdings = portfolio.get("holdings", [])
    if holdings:
        lines.append("## 4. 보유 종목 분석")
        lines.append("")
        for h in holdings:
            lines.append(f"### {h['name']} ({h['tag']})")
            lines.append("")
            lines.append(f"- 수량: {h['qty']}주 | 현재가: {h['current_price']:,}원 | 평가: {h['value']:,}원")
            if "buy_price" in h:
                lines.append(f"- 매수가: {h['buy_price']:,}원 | 손익: **{h.get('pnl_pct', 0):+.2f}%**")
                lines.append(f"- 최고가: {h.get('peak_price', 0):,}원 | 매수 시각: {h.get('buy_time', '?')}")
            if "ta_detail" in h:
                lines.append(f"- TA: {h['ta_detail']}")
                lines.append(f"- TA 신호: **{h.get('ta_signal', '?')}** (점수 {h.get('ta_score', 0):+.0f})")
            lines.append("")

    # ── 7. LGBM 예측 모델 상태 ──
    lgbm = market_ctx.get("lgbm", {})
    if lgbm and lgbm.get("accuracy"):
        lines.append("## 5. ML 모델 상태")
        lines.append("")
        lines.append(f"- LightGBM 정확도: {lgbm.get('accuracy', 0):.1%}")
        lines.append(f"- AUC: {lgbm.get('auc', 0):.3f}")
        lines.append(f"- 학습 샘플: {lgbm.get('n_samples', 0)}건")
        lines.append(f"- 최종 학습: {lgbm.get('trained_at', '?')}")
        fi = lgbm.get("feature_importance", {})
        if fi:
            lines.append(f"- 상위 피처: " +
                          ", ".join(f"{k}({v:.0f})" for k, v in list(fi.items())[:5]))
        lines.append("")

    # ── 8. TA 지표 적중률 ──
    ta_acc = market_ctx.get("ta_accuracy", {})
    total_evals = sum(v.get("total", 0) for v in ta_acc.values())
    if total_evals > 0:
        lines.append("## 6. TA 지표 적중률")
        lines.append("")
        lines.append("| 지표 | 적중률 | 평가 건수 | 가중치 |")
        lines.append("|---|---|---|---|")
        ta_weights = market_ctx.get("ta_weights", DEFAULT_WEIGHTS)
        for ind in ["rsi", "macd", "bb", "stoch", "adx", "ma", "obv", "mfi", "atr"]:
            stats = ta_acc.get(ind, {"correct": 0, "total": 0})
            rate = stats["correct"] / stats["total"] * 100 if stats.get("total", 0) > 0 else 0
            w = ta_weights.get(ind, 0)
            bar = "█" * int(rate / 10) + "░" * (10 - int(rate / 10)) if stats.get("total", 0) > 0 else "N/A"
            lines.append(f"| {ind.upper()} | {bar} {rate:.0f}% | {stats.get('total', 0)}건 | {w:.3f} |")
        lines.append("")

    # ── 9. 시장 환경 추이 (최근 5일) ──
    recent_history = market_ctx.get("market_history_recent", [])
    if len(recent_history) >= 2:
        lines.append("## 7. 최근 시장 환경 추이")
        lines.append("")
        lines.append("| 날짜 | 추세 | HMM | 신뢰도 | KOSPI 5일 | 건강도 | K값 |")
        lines.append("|---|---|---|---|---|---|---|")
        for h in recent_history:
            lines.append(
                f"| {h.get('date', '?')} "
                f"| {h.get('regime_trend', '?')} "
                f"| {h.get('hmm_state', '?')} "
                f"| {h.get('confidence', 0):.0%} "
                f"| {h.get('kospi_5d', 0):+.1f}% "
                f"| {h.get('breadth', '?')} "
                f"| {h.get('k_value', 0.5)} |"
            )
        lines.append("")

        # 추세 연속성 분석
        trends = [h.get("regime_trend") for h in recent_history]
        if trends and all(t == trends[-1] for t in trends):
            lines.append(f"> {len(trends)}일 연속 **{trends[-1]}** 추세 유지 중")
            lines.append("")
        confidences = [h.get("confidence", 0.5) for h in recent_history]
        if len(confidences) >= 3:
            trend_dir = "상승" if confidences[-1] > confidences[-3] + 0.05 else \
                        "하락" if confidences[-1] < confidences[-3] - 0.05 else "횡보"
            lines.append(f"> 신뢰도 추이: {' → '.join(f'{c:.0%}' for c in confidences)} ({trend_dir})")
            lines.append("")

    # ── 10. 포트폴리오 요약 ──
    lines.append("## 8. 포트폴리오 요약")
    lines.append("")
    lines.append(f"- 총 자산: **{total_value:,}원**")
    lines.append(f"- 현금: {portfolio['cash']:,}원 ({portfolio['cash']/total_value*100:.0f}%)")
    lines.append(f"- 보유 평가: {portfolio['holdings_value']:,}원")
    lines.append(f"- 당일 손익: **{day_pnl:+,}원** ({pnl_pct:+.2f}%)")
    lines.append(f"- 누적 손익: {portfolio['total_pnl']:+,}원 ({portfolio['total_pnl_pct']:+.2f}%)")
    lines.append(f"- 총 거래 수: {portfolio['total_trades']}건")
    lines.append("")

    # ── 11. 의사결정 분석 ──
    lines.append("## 9. 의사결정 분석")
    lines.append("")

    if not trades:
        lines.append("### 미거래 사유 분석")
        lines.append("")
        reasons = []
        if turb.get("is_turbulent"):
            reasons.append("터뷸런스 감지로 매수 차단")
        if confidence < 0.4:
            reasons.append(f"시장 신뢰도 낮음 ({confidence:.0%})")
        if regime.get("hmm_state") == "bear" and regime.get("hmm_confidence", 0) > 0.7:
            reasons.append("HMM 약세장 판단 — 매수 억제")
        if not reasons:
            reasons.append("돌파 신호 미발생 (가격이 K×레인지를 돌파하지 못함)")
            reasons.append("또는 TA 복합 점수가 매수 기준 미달")
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")
        lines.append("**판단**: 현금 보유 유지는 시장 불확실성 하에서 유효한 전략입니다.")
        lines.append("")
    else:
        lines.append("### 거래 판단 근거")
        lines.append("")
        buy_trades = [t for t in trades if t["side"] == "buy"]
        sell_trades = [t for t in trades if t["side"] == "sell"]

        if buy_trades:
            lines.append("**매수 판단:**")
            lines.append(f"- 시장 레짐: {regime.get('trend', '?')} (HMM: {regime.get('hmm_state', '?')})")
            lines.append(f"- 시장 신뢰도: {confidence:.0%} → 투입 비율 x{max(0.3, confidence):.0%}")
            if kelly:
                lines.append(f"- Kelly 최적 비율: {kelly.get('combined', 0):.1%}")
            lines.append("")

        if sell_trades:
            lines.append("**매도 판단:**")
            for t in sell_trades:
                ts = t.get("timestamp", "")[-8:]
                lines.append(f"- {t.get('name', t['symbol'])} @ {ts} — "
                             f"{int(t['price']):,}원 x {t['qty']}주")
            lines.append("")

        if day_pnl > 0:
            lines.append(f"**결과**: 당일 수익 **{day_pnl:+,}원** 실현.")
        elif day_pnl < 0:
            lines.append(f"**결과**: 당일 손실 **{day_pnl:+,}원**. 리스크 관리 점검 필요.")
        lines.append("")

    # ── 12. 전략 파라미터 기록 ──
    lines.append("## 10. 전략 파라미터 스냅샷")
    lines.append("")
    params = portfolio.get("strategies", {}).get("etf_breakout", {}).get("params", {})
    lines.append(f"- 변동성 돌파 K: {params.get('k', 0.5)}")
    lines.append(f"- 추세 MA: {params.get('trend_ma', 20)}")
    lines.append(f"- 손절 기준: -3%")
    lines.append(f"- 추적 손절: +1.5% 활성화, 고점 대비 -1%")
    lines.append(f"- TWAP 분할: 3~5 트랜치")
    lines.append(f"- Kelly Half: {kelly.get('combined', 0.10):.1%} (최대 25%)")
    lines.append("")

    # TA 가중치 기록
    ta_weights = market_ctx.get("ta_weights", DEFAULT_WEIGHTS)
    lines.append("**TA 가중치:**")
    lines.append(f"```")
    for ind, w in ta_weights.items():
        lines.append(f"  {ind:<6}: {w:.3f}")
    lines.append(f"```")
    lines.append("")

    # ── 13. 학습 메모 (모델 피드백용) ──
    lines.append("## 11. 학습 메모")
    lines.append("")
    lines.append("<!-- 이 섹션은 모델이 시장 대응 패턴을 학습하는 데 활용됩니다 -->")
    lines.append("")

    # 자동 생성 인사이트
    insights = []

    # 레짐 vs 결과 매칭
    if trades and regime.get("hmm_state") == "bull" and day_pnl > 0:
        insights.append("강세장 판단 하에 매수 → 수익 실현. HMM 신호 유효.")
    elif trades and regime.get("hmm_state") == "bear" and day_pnl < 0:
        insights.append("약세장에서 매수 진행 → 손실. 향후 약세장 매수 억제 강화 필요.")
    elif not trades and regime.get("hmm_state") == "bear":
        insights.append("약세장 대기 판단 — 적절한 현금 보유.")

    # 터뷸런스 vs 결과
    if turb.get("is_turbulent") and not trades:
        insights.append("터뷸런스 감지 → 매수 차단. 변동성 필터 작동 확인.")

    # 신뢰도 vs 결과
    if confidence < 0.4 and not trades:
        insights.append(f"저신뢰도({confidence:.0%}) 환경에서 현금 보유. 보수적 판단 유효.")
    elif confidence > 0.7 and trades and day_pnl > 0:
        insights.append(f"고신뢰도({confidence:.0%}) 환경 매수 → 수익. 신뢰도 지표 유효.")

    # 섹터 인사이트
    if sectors:
        strong_count = sum(1 for s in sectors.values() if s.get("momentum") in ("strong", "positive"))
        weak_count = sum(1 for s in sectors.values() if s.get("momentum") in ("weak", "negative"))
        if strong_count > weak_count * 2:
            insights.append(f"대다수 섹터 강세({strong_count}/{len(sectors)}). 시장 전반 상승 추세.")
        elif weak_count > strong_count * 2:
            insights.append(f"대다수 섹터 약세({weak_count}/{len(sectors)}). 방어적 자세 유지.")

    if insights:
        for ins in insights:
            lines.append(f"- {ins}")
    else:
        lines.append("- 특이 패턴 없음. 시장 기본 흐름 유지.")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    print(f"[{now:%Y-%m-%d %H:%M}] 투자 일지 생성 (상세 버전)")

    client = KISClient()

    # 1. 포트폴리오 JSON
    portfolio = build_portfolio_json(client)
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PORTFOLIO_PATH.open("w", encoding="utf-8") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)
    print(f"  portfolio.json 업데이트 완료")

    # 2. 일일 노트 생성
    note = generate_daily_note(portfolio)
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    note_path = POSTS_DIR / f"{today}-daily-note.md"
    with note_path.open("w", encoding="utf-8") as f:
        f.write(note)
    print(f"  {note_path.name} 생성 완료")

    # 3. 요약
    day_pnl = portfolio["daily_history"][-1].get("day_pnl", 0) if portfolio["daily_history"] else 0
    regime = portfolio.get("market_context", {}).get("regime", {})
    print(f"  총 자산: {portfolio['total_value']:,}원")
    print(f"  당일 PnL: {day_pnl:+,}원")
    print(f"  누적 PnL: {portfolio['total_pnl']:+,}원 ({portfolio['total_pnl_pct']:+.2f}%)")
    print(f"  레짐: {regime.get('trend', '?')} | HMM: {regime.get('hmm_state', '?')}")


if __name__ == "__main__":
    main()
