"""적응형 학습 — 오버나이트갭·보유기간·섹터모멘텀 성과 추적 및 파라미터 적응.

새 기능들의 효과를 실측 데이터로 검증하고,
매일 장 후 학습에서 파라미터를 자동 튜닝한다.

데이터 파일:
    logs/gap_accuracy.json    — 갭 신호 vs 실제 한국장 결과
    logs/hold_outcomes.json   — 보유 연장 vs 당일 매도 비교
    logs/sector_accuracy.json — 섹터 우선순위 vs 실제 수익률
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.utils.logger import log

GAP_ACCURACY_PATH = Path("logs/gap_accuracy.json")
HOLD_OUTCOMES_PATH = Path("logs/hold_outcomes.json")
SECTOR_ACCURACY_PATH = Path("logs/sector_accuracy.json")


# ──────────────────────────────────────────────────────────
# 1. 오버나이트 갭 적중률 추적
# ──────────────────────────────────────────────────────────

def _load_json(path: Path) -> list[dict]:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_json(path: Path, data: list[dict], max_records: int = 180) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = data[-max_records:]
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_gap_signal(gap_signal: dict, kospi_open_change: float) -> None:
    """장 후: 오늘 갭 신호가 한국장 방향을 맞혔는지 기록.

    Args:
        gap_signal: strategy.yaml의 overnight_signal (direction, recommended_action 등)
        kospi_open_change: KOSPI 시가 대비 전종가 갭 (%) — 양수면 갭업
    """
    records = _load_json(GAP_ACCURACY_PATH)

    direction = gap_signal.get("direction", "neutral")
    action = gap_signal.get("recommended_action", "normal")

    # 예측 방향 vs 실제 방향 일치 여부
    if direction == "bullish":
        correct = kospi_open_change > 0
    elif direction == "bearish":
        correct = kospi_open_change < 0
    else:
        correct = abs(kospi_open_change) < 0.5  # 중립 예측 + 실제 보합 → 정답

    records.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "nasdaq_change": gap_signal.get("nasdaq_change", 0),
        "sp500_change": gap_signal.get("sp500_change", 0),
        "direction": direction,
        "action": action,
        "kospi_gap": round(kospi_open_change, 2),
        "correct": correct,
    })

    _save_json(GAP_ACCURACY_PATH, records)
    log.info("gap_accuracy_recorded", correct=correct, direction=direction,
             kospi_gap=f"{kospi_open_change:+.2f}%")


def get_gap_accuracy() -> dict:
    """최근 갭 신호 적중률 통계.

    Returns:
        {"total": int, "correct": int, "accuracy": float,
         "bullish_accuracy": float, "bearish_accuracy": float,
         "adaptive_thresholds": dict}
    """
    records = _load_json(GAP_ACCURACY_PATH)

    if len(records) < 5:
        return {"total": len(records), "sufficient": False}

    total = len(records)
    correct = sum(1 for r in records if r.get("correct"))

    # 방향별 적중률
    bullish = [r for r in records if r.get("direction") == "bullish"]
    bearish = [r for r in records if r.get("direction") == "bearish"]
    bull_acc = sum(1 for r in bullish if r.get("correct")) / len(bullish) if bullish else 0
    bear_acc = sum(1 for r in bearish if r.get("correct")) / len(bearish) if bearish else 0

    # 적응적 임계값: 적중률이 낮으면 더 큰 변동에서만 행동
    # 기본 임계값: bullish >1.5%, bearish <-1.5%
    overall_acc = correct / total
    if overall_acc < 0.45:
        # 예측력 약함 → 임계값 높여서 극단적 경우만 행동
        bullish_threshold = 2.0
        bearish_threshold = -2.0
    elif overall_acc > 0.65:
        # 예측력 강함 → 임계값 낮춰서 더 자주 활용
        bullish_threshold = 1.0
        bearish_threshold = -1.0
    else:
        bullish_threshold = 1.5
        bearish_threshold = -1.5

    return {
        "total": total,
        "correct": correct,
        "accuracy": round(overall_acc, 3),
        "bullish_accuracy": round(bull_acc, 3),
        "bearish_accuracy": round(bear_acc, 3),
        "sufficient": True,
        "adaptive_thresholds": {
            "bullish": bullish_threshold,
            "bearish": bearish_threshold,
        },
    }


# ──────────────────────────────────────────────────────────
# 2. 보유 기간 결과 추적
# ──────────────────────────────────────────────────────────

def record_hold_outcome(
    symbol: str,
    hold_days: int,
    action_taken: str,    # "held" / "sold_at_open"
    buy_price: int,
    exit_price: int,
    pnl_pct: float,
) -> None:
    """보유 연장 또는 시가 매도 결과를 기록."""
    records = _load_json(HOLD_OUTCOMES_PATH)
    records.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "hold_days": hold_days,
        "action": action_taken,
        "buy_price": buy_price,
        "exit_price": exit_price,
        "pnl_pct": round(pnl_pct, 4),
    })
    _save_json(HOLD_OUTCOMES_PATH, records)


def get_hold_analysis() -> dict:
    """보유 기간별 수익률 분석.

    Returns:
        {"optimal_hold_days": int, "hold_vs_sell": dict, "sufficient": bool,
         "adaptive_rules": dict}
    """
    records = _load_json(HOLD_OUTCOMES_PATH)

    if len(records) < 10:
        return {"total": len(records), "sufficient": False}

    # 보유일수별 평균 수익률
    by_days: dict[int, list[float]] = {}
    for r in records:
        days = r.get("hold_days", 0)
        pnl = r.get("pnl_pct", 0)
        by_days.setdefault(days, []).append(pnl)

    day_stats = {}
    for days, pnls in sorted(by_days.items()):
        import numpy as np
        day_stats[days] = {
            "count": len(pnls),
            "avg_pnl": round(float(np.mean(pnls)), 4),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
        }

    # 최적 보유일수 (평균 수익률 최고 + 최소 5건)
    best_days = 1
    best_pnl = -999
    for days, stats in day_stats.items():
        if stats["count"] >= 5 and stats["avg_pnl"] > best_pnl:
            best_pnl = stats["avg_pnl"]
            best_days = days

    # 보유 vs 즉시 매도 비교
    held = [r for r in records if r.get("action") == "held"]
    sold = [r for r in records if r.get("action") == "sold_at_open"]
    held_avg = sum(r["pnl_pct"] for r in held) / len(held) if held else 0
    sold_avg = sum(r["pnl_pct"] for r in sold) / len(sold) if sold else 0

    # 적응적 보유 규칙
    # 보유 연장이 더 나은 결과를 냈으면 보유 기준 완화, 아니면 강화
    if held_avg > sold_avg + 0.002:
        # 보유가 유리 → 더 적극적으로 보유
        min_profit_to_hold = 0.001   # +0.1%만 수익이어도 보유
        max_hold_days = min(5, best_days + 1)
    elif held_avg < sold_avg - 0.002:
        # 즉시 매도가 유리 → 보유 기준 엄격
        min_profit_to_hold = 0.008   # +0.8% 이상에서만 보유
        max_hold_days = max(2, best_days)
    else:
        min_profit_to_hold = 0.003
        max_hold_days = 5

    return {
        "total": len(records),
        "sufficient": True,
        "optimal_hold_days": best_days,
        "day_stats": day_stats,
        "hold_avg_pnl": round(held_avg, 4),
        "sell_avg_pnl": round(sold_avg, 4),
        "hold_better": held_avg > sold_avg,
        "adaptive_rules": {
            "min_profit_to_hold": min_profit_to_hold,
            "max_hold_days": max_hold_days,
        },
    }


# ──────────────────────────────────────────────────────────
# 3. 섹터 모멘텀 효과 추적
# ──────────────────────────────────────────────────────────

def record_sector_trade(
    symbol: str,
    sector: str,
    was_strong_sector: bool,
    pnl_pct: float,
) -> None:
    """섹터 기반 매매 결과를 기록."""
    records = _load_json(SECTOR_ACCURACY_PATH)
    records.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "sector": sector,
        "strong_sector": was_strong_sector,
        "pnl_pct": round(pnl_pct, 4),
    })
    _save_json(SECTOR_ACCURACY_PATH, records)


def get_sector_momentum_analysis() -> dict:
    """강세 섹터 우선 매수 효과 분석.

    Returns:
        {"strong_avg_pnl": float, "weak_avg_pnl": float, "momentum_alpha": float,
         "sufficient": bool}
    """
    records = _load_json(SECTOR_ACCURACY_PATH)

    if len(records) < 10:
        return {"total": len(records), "sufficient": False}

    strong = [r for r in records if r.get("strong_sector")]
    weak = [r for r in records if not r.get("strong_sector")]

    strong_avg = sum(r["pnl_pct"] for r in strong) / len(strong) if strong else 0
    weak_avg = sum(r["pnl_pct"] for r in weak) / len(weak) if weak else 0

    strong_wr = sum(1 for r in strong if r["pnl_pct"] > 0) / len(strong) if strong else 0
    weak_wr = sum(1 for r in weak if r["pnl_pct"] > 0) / len(weak) if weak else 0

    # 모멘텀 알파: 강세 섹터가 약세 섹터 대비 초과 수익
    momentum_alpha = strong_avg - weak_avg

    return {
        "total": len(records),
        "sufficient": True,
        "strong_trades": len(strong),
        "weak_trades": len(weak),
        "strong_avg_pnl": round(strong_avg, 4),
        "weak_avg_pnl": round(weak_avg, 4),
        "strong_win_rate": round(strong_wr, 3),
        "weak_win_rate": round(weak_wr, 3),
        "momentum_alpha": round(momentum_alpha, 4),
        "momentum_effective": momentum_alpha > 0.001,
    }


# ──────────────────────────────────────────────────────────
# 4. 미국장 거래 성과 추적
# ──────────────────────────────────────────────────────────

US_TRADE_HISTORY_PATH = Path("logs/us_trade_history.json")
CROSS_MARKET_PATH = Path("logs/cross_market.json")


def record_us_trade_result(
    symbol: str,
    side: str,
    buy_price: float,
    sell_price: float,
    qty: int,
    asset_type: str = "us_long",
    regime_at_trade: str = "unknown",
) -> None:
    """미국장 거래 결과 기록."""
    records = _load_json(US_TRADE_HISTORY_PATH)
    pnl_pct = (sell_price - buy_price) / buy_price if buy_price > 0 else 0
    records.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "asset_type": asset_type,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "qty": qty,
        "pnl_pct": round(pnl_pct, 4),
        "pnl_usd": round((sell_price - buy_price) * qty, 2),
        "regime": regime_at_trade,
    })
    _save_json(US_TRADE_HISTORY_PATH, records, max_records=360)


def get_us_trade_analysis() -> dict:
    """미국장 거래 성과 분석.

    Returns:
        종합 통계 + 자산 유형별 분석 + 적응 파라미터
    """
    records = _load_json(US_TRADE_HISTORY_PATH)

    if len(records) < 5:
        return {"total": len(records), "sufficient": False}

    # 전체 통계
    pnls = [r["pnl_pct"] for r in records]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    avg_pnl = sum(pnls) / len(pnls)
    win_rate = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    # 자산 유형별 분석
    by_type: dict[str, list[float]] = {}
    for r in records:
        t = r.get("asset_type", "us_long")
        by_type.setdefault(t, []).append(r["pnl_pct"])

    type_stats = {}
    for t, t_pnls in by_type.items():
        t_wins = [p for p in t_pnls if p > 0]
        type_stats[t] = {
            "count": len(t_pnls),
            "avg_pnl": round(sum(t_pnls) / len(t_pnls), 4),
            "win_rate": round(len(t_wins) / len(t_pnls), 3),
        }

    # 레짐별 분석
    by_regime: dict[str, list[float]] = {}
    for r in records:
        reg = r.get("regime", "unknown")
        by_regime.setdefault(reg, []).append(r["pnl_pct"])

    regime_stats = {}
    for reg, reg_pnls in by_regime.items():
        r_wins = [p for p in reg_pnls if p > 0]
        regime_stats[reg] = {
            "count": len(reg_pnls),
            "avg_pnl": round(sum(reg_pnls) / len(reg_pnls), 4),
            "win_rate": round(len(r_wins) / len(reg_pnls), 3),
        }

    # 적응 파라미터: 최근 성과 기반 K값/손절 조정
    recent = records[-20:] if len(records) >= 20 else records
    recent_pnls = [r["pnl_pct"] for r in recent]
    recent_wr = sum(1 for p in recent_pnls if p > 0) / len(recent_pnls)

    # 승률이 낮으면 K를 높여 진입 보수적으로
    if recent_wr < 0.35:
        k_adj = 0.05   # K 상향
        stop_adj = -0.005  # 손절 좁힘
    elif recent_wr > 0.60:
        k_adj = -0.03  # K 하향 (진입 완화)
        stop_adj = 0.005   # 손절 여유
    else:
        k_adj = 0
        stop_adj = 0

    return {
        "total": len(records),
        "sufficient": True,
        "avg_pnl": round(avg_pnl, 4),
        "win_rate": round(win_rate, 3),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "by_type": type_stats,
        "by_regime": regime_stats,
        "recent_win_rate": round(recent_wr, 3),
        "adaptive_params": {
            "k_adjustment": k_adj,
            "stop_loss_adjustment": stop_adj,
        },
    }


# ──────────────────────────────────────────────────────────
# 5. 교차 시장 상관관계 학습
# ──────────────────────────────────────────────────────────

def record_cross_market(
    us_market_change: float,
    kr_next_open_change: float,
    us_regime: str = "unknown",
    kr_regime: str = "unknown",
) -> None:
    """미국장 종가 변동 → 한국장 시가 영향 기록.

    Args:
        us_market_change: 미국장 당일 변동률 (QQQ 또는 SPY, %)
        kr_next_open_change: 한국장 다음날 시가 갭 (%)
    """
    records = _load_json(CROSS_MARKET_PATH)
    records.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "us_change": round(us_market_change, 3),
        "kr_gap": round(kr_next_open_change, 3),
        "us_regime": us_regime,
        "kr_regime": kr_regime,
    })
    _save_json(CROSS_MARKET_PATH, records, max_records=180)


def get_cross_market_analysis() -> dict:
    """교차 시장 상관관계 분석.

    미국장 변동과 한국장 갭의 관계를 파악하여
    미국장 종료 후 한국장 전략을 조정하는 데 활용.
    """
    records = _load_json(CROSS_MARKET_PATH)

    if len(records) < 10:
        return {"total": len(records), "sufficient": False}

    us_changes = [r["us_change"] for r in records]
    kr_gaps = [r["kr_gap"] for r in records]

    # 상관계수
    import numpy as np
    if len(us_changes) >= 10:
        correlation = float(np.corrcoef(us_changes, kr_gaps)[0, 1])
    else:
        correlation = 0

    # 미국 급락(-2% 이하) → 한국 갭다운 비율
    us_drops = [(r["us_change"], r["kr_gap"]) for r in records if r["us_change"] <= -2]
    kr_gap_down_after_drop = sum(1 for _, kg in us_drops if kg < 0) / len(us_drops) if us_drops else 0

    # 미국 급등(+2% 이상) → 한국 갭업 비율
    us_jumps = [(r["us_change"], r["kr_gap"]) for r in records if r["us_change"] >= 2]
    kr_gap_up_after_jump = sum(1 for _, kg in us_jumps if kg > 0) / len(us_jumps) if us_jumps else 0

    # 적응 파라미터: 상관관계 강도에 따라 갭 신호 반영 정도 조절
    if abs(correlation) > 0.6:
        gap_weight = 1.2   # 상관관계 강함 → 갭 신호 강하게 반영
    elif abs(correlation) > 0.3:
        gap_weight = 1.0   # 보통
    else:
        gap_weight = 0.7   # 상관관계 약함 → 갭 신호 약하게 반영

    return {
        "total": len(records),
        "sufficient": True,
        "correlation": round(correlation, 3),
        "us_drops_count": len(us_drops),
        "kr_gap_down_after_drop": round(kr_gap_down_after_drop, 3),
        "us_jumps_count": len(us_jumps),
        "kr_gap_up_after_jump": round(kr_gap_up_after_jump, 3),
        "adaptive_params": {
            "gap_signal_weight": gap_weight,
        },
    }


# ──────────────────────────────────────────────────────────
# 통합: 장 후 적응 학습 실행
# ──────────────────────────────────────────────────────────

def run_adaptive_learning(client, cfg: dict) -> dict:
    """장 후: 새 기능들의 성과를 평가하고 파라미터를 적응 조정.

    Args:
        client: KISClient
        cfg: strategy.yaml config dict

    Returns:
        updated config dict with adaptive parameters
    """
    from src.bot.runner import fetch_recent_history

    print("\n[적응 학습] 새 기능 성과 평가...")

    # ── 1. 오버나이트 갭 적중률 ──
    gap_signal = cfg.get("overnight_signal", {})
    if gap_signal and gap_signal.get("direction", "neutral") != "neutral":
        try:
            kospi_hist = fetch_recent_history(client, "069500", days=5)
            close = kospi_hist["close"].astype(float)
            if len(close) >= 2:
                kospi_gap = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
                record_gap_signal(gap_signal, kospi_gap)
        except Exception as e:
            log.warning("gap_accuracy_eval_failed", error=str(e))

    gap_stats = get_gap_accuracy()
    if gap_stats.get("sufficient"):
        print(f"  [갭] 적중률: {gap_stats['accuracy']:.0%} "
              f"(bullish {gap_stats['bullish_accuracy']:.0%}, "
              f"bearish {gap_stats['bearish_accuracy']:.0%}) — "
              f"{gap_stats['total']}건")
        cfg["gap_adaptive_thresholds"] = gap_stats["adaptive_thresholds"]
    else:
        print(f"  [갭] 데이터 부족 ({gap_stats.get('total', 0)}건), 기본값 유지")

    # ── 2. 보유 기간 분석 ──
    hold_stats = get_hold_analysis()
    if hold_stats.get("sufficient"):
        hold_better = "보유 유리" if hold_stats["hold_better"] else "즉시 매도 유리"
        print(f"  [보유] 보유 평균 {hold_stats['hold_avg_pnl']:+.2%} vs "
              f"매도 평균 {hold_stats['sell_avg_pnl']:+.2%} → {hold_better}")
        print(f"  [보유] 최적 보유일: {hold_stats['optimal_hold_days']}일")
        cfg["hold_adaptive_rules"] = hold_stats["adaptive_rules"]
    else:
        print(f"  [보유] 데이터 부족 ({hold_stats.get('total', 0)}건), 기본값 유지")

    # ── 3. 섹터 모멘텀 효과 ──
    # 경험 버퍼에서 오늘 평가된 거래의 섹터 정보 추출
    try:
        from src.experience import _load_experience
        today_str = datetime.now().strftime("%Y-%m-%d")
        exp = _load_experience()
        for r in exp:
            if (r.get("date") == today_str and r.get("evaluated")
                    and r.get("action") == "buy" and r.get("pnl_pct") is not None):
                is_strong = r.get("strong_sector", False)
                record_sector_trade(
                    r["symbol"], r.get("name", ""), is_strong, r["pnl_pct"])
    except Exception as e:
        log.warning("sector_trade_record_failed", error=str(e))

    sector_stats = get_sector_momentum_analysis()
    if sector_stats.get("sufficient"):
        eff = "효과적" if sector_stats["momentum_effective"] else "비효과적"
        print(f"  [섹터] 강세 {sector_stats['strong_avg_pnl']:+.2%} vs "
              f"약세 {sector_stats['weak_avg_pnl']:+.2%} "
              f"(알파 {sector_stats['momentum_alpha']:+.2%}) → {eff}")
        cfg["sector_momentum_effective"] = sector_stats["momentum_effective"]
    else:
        print(f"  [섹터] 데이터 부족 ({sector_stats.get('total', 0)}건), 기본값 유지")

    # ── 4. 하락장 전략 성과 학습 ──
    try:
        from src.strategies.bear_strategy import get_regime_performance, get_adaptive_params
        for regime in ("BEAR", "CAUTION", "CRISIS"):
            perf = get_regime_performance(regime)
            if perf.get("sufficient_data"):
                stats = perf.get("stats", {})
                details = []
                for action, s in stats.items():
                    details.append(f"{action}={s['avg_pnl']:+.2%}({s['count']}건)")
                adaptive = get_adaptive_params(regime)
                print(f"  [레짐-{regime}] {', '.join(details)} → {adaptive['reason']}")
    except Exception as e:
        log.warning("bear_learning_eval_failed", error=str(e))

    # ── 5. 미국장 거래 성과 (한국장 학습 시에도 확인) ──
    us_stats = get_us_trade_analysis()
    if us_stats.get("sufficient"):
        print(f"  [US] 승률: {us_stats['win_rate']:.0%} | 평균: {us_stats['avg_pnl']:+.2%} "
              f"({us_stats['total']}건)")
        for t, ts in us_stats.get("by_type", {}).items():
            print(f"    {t}: {ts['avg_pnl']:+.2%} 승률 {ts['win_rate']:.0%} ({ts['count']}건)")
        us_adaptive = us_stats["adaptive_params"]
        cfg.setdefault("us_session", {}).setdefault("strategy", {})
        cfg["us_adaptive_params"] = us_adaptive
    else:
        print(f"  [US] 데이터 부족 ({us_stats.get('total', 0)}건)")

    # ── 6. 교차 시장 상관관계 ──
    cross_stats = get_cross_market_analysis()
    if cross_stats.get("sufficient"):
        print(f"  [교차] US↔KR 상관: {cross_stats['correlation']:+.2f} "
              f"| 급락후갭다운: {cross_stats['kr_gap_down_after_drop']:.0%} "
              f"| 급등후갭업: {cross_stats['kr_gap_up_after_jump']:.0%}")
        cfg["cross_market_params"] = cross_stats["adaptive_params"]
    else:
        print(f"  [교차] 데이터 부족 ({cross_stats.get('total', 0)}건)")

    return cfg


def run_us_post_learning(client, cfg: dict) -> dict:
    """미국장 종료 후 학습: US 거래 결과 평가 + 교차 시장 데이터 수집.

    Args:
        client: KISClient
        cfg: strategy.yaml config dict

    Returns:
        updated config dict
    """
    from src.bot.us_session import load_us_config, load_us_positions, get_us_price

    print("\n[US 장 후 학습] 미국장 거래 결과 평가...")

    us_cfg = load_us_config()
    if not us_cfg.get("enabled", False):
        print("  US 세션 비활성화. 스킵.")
        return cfg

    # ── 1. US 거래 결과 수집 및 기록 ──
    print("\n  [1] US 거래 결과 평가...")
    import csv
    today_str = datetime.now().strftime("%Y-%m-%d")
    trade_log = Path("logs/trades.csv")
    us_buys: dict[str, float] = {}
    us_trades: list[dict] = []

    if trade_log.exists():
        with trade_log.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row.get("timestamp", "").startswith(today_str):
                    continue
                sym = row.get("symbol", "")
                side = row.get("side", "")
                price = float(row.get("price", 0))

                # US 종목 판별 (영문 티커)
                if not sym.isalpha():
                    continue

                if side == "buy":
                    us_buys[sym] = price
                elif side == "sell" and sym in us_buys:
                    us_trades.append({
                        "symbol": sym,
                        "buy_price": us_buys[sym],
                        "sell_price": price,
                    })

    # 아직 보유 중인 종목은 현재가로 미실현 평가
    us_positions = load_us_positions()
    for sym, pos in us_positions.items():
        if sym not in [t["symbol"] for t in us_trades]:
            cur_price = get_us_price(client, sym, pos.get("exchange", "NASD"))
            if cur_price > 0:
                us_trades.append({
                    "symbol": sym,
                    "buy_price": pos["buy_price"],
                    "sell_price": cur_price,
                    "unrealized": True,
                })

    # 레짐 정보
    bear_state_path = Path("logs/bear_state.json")
    kr_regime = "unknown"
    try:
        if bear_state_path.exists():
            with bear_state_path.open("r", encoding="utf-8") as f:
                bear_data = json.load(f)
            kr_regime = bear_data.get("regime", "unknown")
    except Exception:
        pass

    for trade in us_trades:
        sym = trade["symbol"]
        buy_p = trade["buy_price"]
        sell_p = trade["sell_price"]
        pnl_pct = (sell_p - buy_p) / buy_p if buy_p > 0 else 0

        # 자산 유형 추정
        asset_type = "us_long"
        us_universe = us_cfg.get("universe", [])
        for s in us_universe:
            if s["symbol"] == sym:
                t = s.get("type", "")
                if t == "inverse":
                    asset_type = "us_inverse"
                elif t == "defensive":
                    asset_type = "us_defensive"
                break

        unrealized = trade.get("unrealized", False)
        tag = "(미실현)" if unrealized else "(실현)"
        print(f"    {sym} {tag}: ${buy_p:.2f}→${sell_p:.2f} = {pnl_pct:+.2%} [{asset_type}]")

        if not unrealized:
            record_us_trade_result(
                sym, "sell", buy_p, sell_p, 0,
                asset_type=asset_type, regime_at_trade=kr_regime,
            )

    if not us_trades:
        print("    오늘 US 거래 없음.")

    # ── 2. US 전략 파라미터 적응 ──
    print("\n  [2] US 전략 파라미터 적응...")
    us_analysis = get_us_trade_analysis()
    if us_analysis.get("sufficient"):
        adaptive = us_analysis["adaptive_params"]
        current_k = us_cfg.get("strategy", {}).get("k", 0.5)
        new_k = round(max(0.3, min(0.7, current_k + adaptive["k_adjustment"])), 2)

        current_stop = us_cfg.get("strategy", {}).get("stop_loss_pct", 0.025)
        new_stop = round(max(0.015, min(0.04,
                         current_stop + adaptive["stop_loss_adjustment"])), 3)

        if new_k != current_k or new_stop != current_stop:
            cfg.setdefault("us_session", {}).setdefault("strategy", {})
            cfg["us_session"]["strategy"]["k"] = new_k
            cfg["us_session"]["strategy"]["stop_loss_pct"] = new_stop
            print(f"    K: {current_k} → {new_k}")
            print(f"    손절: {current_stop:.1%} → {new_stop:.1%}")
        else:
            print("    파라미터 변경 없음.")

        # 유형별 성과에 따른 레짐 연동 조정
        by_type = us_analysis.get("by_type", {})
        inverse_stats = by_type.get("us_inverse", {})
        long_stats = by_type.get("us_long", {})

        if inverse_stats.get("count", 0) >= 3 and long_stats.get("count", 0) >= 3:
            inv_pnl = inverse_stats.get("avg_pnl", 0)
            long_pnl = long_stats.get("avg_pnl", 0)
            if inv_pnl > long_pnl + 0.01:
                print(f"    인버스가 롱 대비 우수 ({inv_pnl:+.2%} vs {long_pnl:+.2%}) → 레짐 연동 유지")
            elif long_pnl > inv_pnl + 0.01:
                print(f"    롱이 인버스 대비 우수 ({long_pnl:+.2%} vs {inv_pnl:+.2%}) → 레짐 연동 재검토 필요")
    else:
        print(f"    데이터 부족 ({us_analysis.get('total', 0)}건)")

    # ── 3. 교차 시장 데이터 수집 ──
    print("\n  [3] 교차 시장 데이터 수집...")
    try:
        from src.bot.runner import fetch_recent_history

        # 미국장 변동 (QQQ 기준)
        qqq_price = get_us_price(client, "QQQ", "NASD")
        us_change = 0
        try:
            from src.bot.us_session import fetch_us_history
            qqq_hist = fetch_us_history(client, "QQQ", "NASD", days=5)
            if len(qqq_hist) >= 2:
                us_change = float((qqq_hist["close"].iloc[-1] / qqq_hist["close"].iloc[-2] - 1) * 100)
        except Exception:
            pass

        # 한국장 시가 갭 (어제 종가 → 오늘 시가)
        kr_gap = 0
        try:
            kospi_hist = fetch_recent_history(client, "069500", days=5)
            if len(kospi_hist) >= 2:
                close_prev = float(kospi_hist["close"].iloc[-2])
                open_today = float(kospi_hist["open"].iloc[-1]) if "open" in kospi_hist.columns else close_prev
                kr_gap = (open_today / close_prev - 1) * 100
        except Exception:
            pass

        if us_change != 0 or kr_gap != 0:
            record_cross_market(us_change, kr_gap, us_regime="unknown", kr_regime=kr_regime)
            print(f"    US(QQQ): {us_change:+.2f}% → KR갭: {kr_gap:+.2f}%")

        cross_analysis = get_cross_market_analysis()
        if cross_analysis.get("sufficient"):
            print(f"    상관계수: {cross_analysis['correlation']:+.2f} "
                  f"({cross_analysis['total']}건)")
            cfg["cross_market_params"] = cross_analysis["adaptive_params"]
    except Exception as e:
        print(f"    교차 시장 수집 실패: {e}")

    return cfg
