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

    return cfg
