"""경험 버퍼 — 거래 결정의 전체 컨텍스트를 기록·조회·학습.

매 거래 결정(매수/매도/스킵) 시 그 순간의 시장 상황, TA 점수,
LGBM 예측, 레짐, Kelly, 신뢰도 등을 한 레코드로 저장한다.

장 후 학습에서 결과(PnL)를 역추적하여 "이런 상황에서 이렇게 했더니
이런 결과가 났다"를 누적하고, 다음 날 의사결정에 활용한다.

데이터 흐름:
  장 중  →  log_decision()  →  experience.json (컨텍스트 + 결정)
  장 후  →  evaluate_outcomes()  →  결과(PnL) 채움
  장 전  →  query_similar()  →  유사 상황 참조
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.utils.logger import log

EXPERIENCE_PATH = Path("logs/experience.json")
REGIME_MEMORY_PATH = Path("logs/regime_memory.json")
STRATEGY_WEIGHTS_PATH = Path("logs/strategy_weights.json")

MAX_EXPERIENCE_RECORDS = 500  # 최근 500건 유지 (~3개월)


# ──────────────────────────────────────────────────────────
# Experience Buffer
# ──────────────────────────────────────────────────────────

def _load_experience() -> list[dict]:
    if EXPERIENCE_PATH.exists():
        try:
            with EXPERIENCE_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_experience(records: list[dict]) -> None:
    EXPERIENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    records = records[-MAX_EXPERIENCE_RECORDS:]
    with EXPERIENCE_PATH.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def log_decision(
    symbol: str,
    name: str,
    action: str,          # "buy", "sell", "skip"
    reason: str,          # 왜 이 결정을 했는지
    price: int,
    qty: int = 0,
    market_context: dict | None = None,
    ta_scores: dict | None = None,
    lgbm_prob: float | None = None,
    kelly_f: float | None = None,
    confidence: float | None = None,
    regime: str | None = None,
    hmm_state: str | None = None,
    strategy: str | None = None,      # "etf" / "surge"
    extra: dict | None = None,
) -> None:
    """거래 결정 1건의 전체 컨텍스트를 기록."""
    records = _load_experience()

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "name": name,
        "action": action,
        "reason": reason,
        "price": price,
        "qty": qty,
        "strategy": strategy or "unknown",
        # 시장 컨텍스트
        "regime": regime,
        "hmm_state": hmm_state,
        "confidence": confidence,
        "kelly_f": kelly_f,
        # 시그널 컨텍스트
        "ta_scores": ta_scores,
        "lgbm_prob": lgbm_prob,
        # 시장 지표
        "market_context": market_context,
        # 결과 (장 후에 채움)
        "outcome": None,        # "win" / "loss" / "hold"
        "pnl_pct": None,        # 수익률
        "exit_price": None,     # 매도 가격
        "evaluated": False,
    }

    if extra:
        record.update(extra)

    records.append(record)
    _save_experience(records)


def evaluate_outcomes(holdings_pnl: dict[str, float],
                      today_trades: dict[str, dict]) -> None:
    """장 후: 오늘 결정들의 결과를 역추적하여 채움.

    Args:
        holdings_pnl: {symbol: pnl_pct} — 보유 종목의 당일 수익률
        today_trades: {symbol: {"buy_price": int, "sell_price": int, "pnl_pct": float}}
    """
    records = _load_experience()
    today_str = datetime.now().strftime("%Y-%m-%d")

    for record in records:
        if record.get("evaluated") or record.get("date") != today_str:
            continue

        symbol = record["symbol"]
        action = record["action"]

        if action == "buy":
            if symbol in today_trades:
                trade = today_trades[symbol]
                record["pnl_pct"] = trade.get("pnl_pct", 0)
                record["exit_price"] = trade.get("sell_price", 0)
                record["outcome"] = "win" if trade.get("pnl_pct", 0) > 0 else "loss"
            elif symbol in holdings_pnl:
                # 아직 보유 중 — 미실현 손익
                record["pnl_pct"] = holdings_pnl[symbol]
                record["outcome"] = "hold"
            record["evaluated"] = True

        elif action == "skip":
            # 스킵한 종목이 올랐으면 기회 손실, 내렸으면 올바른 판단
            if symbol in holdings_pnl:
                missed_pnl = holdings_pnl.get(symbol, 0)
                record["pnl_pct"] = missed_pnl
                record["outcome"] = "missed_gain" if missed_pnl > 0.005 else "correct_skip"
            record["evaluated"] = True

        elif action == "sell":
            if symbol in today_trades:
                trade = today_trades[symbol]
                record["pnl_pct"] = trade.get("pnl_pct", 0)
                record["outcome"] = "win" if trade.get("pnl_pct", 0) > 0 else "loss"
            record["evaluated"] = True

    _save_experience(records)
    evaluated_count = sum(1 for r in records if r.get("date") == today_str and r.get("evaluated"))
    log.info("experience_evaluated", date=today_str, count=evaluated_count)


def query_similar(regime: str | None = None,
                  hmm_state: str | None = None,
                  strategy: str | None = None,
                  min_records: int = 5) -> dict:
    """유사한 과거 상황에서의 결과 통계를 반환.

    Returns:
        {"count": int, "win_rate": float, "avg_pnl": float,
         "best_action": str, "avg_kelly": float}
    """
    records = _load_experience()
    matches = []

    for r in records:
        if not r.get("evaluated") or r.get("pnl_pct") is None:
            continue
        if regime and r.get("regime") != regime:
            continue
        if hmm_state and r.get("hmm_state") != hmm_state:
            continue
        if strategy and r.get("strategy") != strategy:
            continue
        matches.append(r)

    if len(matches) < min_records:
        return {"count": len(matches), "sufficient": False}

    buys = [r for r in matches if r["action"] == "buy"]
    wins = [r for r in buys if r.get("outcome") == "win"]
    pnls = [r["pnl_pct"] for r in buys if r.get("pnl_pct") is not None]

    win_rate = len(wins) / len(buys) if buys else 0
    avg_pnl = float(np.mean(pnls)) if pnls else 0

    # 스킵이 옳았는지
    skips = [r for r in matches if r["action"] == "skip"]
    correct_skips = [r for r in skips if r.get("outcome") == "correct_skip"]
    skip_accuracy = len(correct_skips) / len(skips) if skips else 0

    # 가장 성과 좋았던 행동
    if win_rate > 0.55 and avg_pnl > 0:
        best_action = "buy_confident"
    elif win_rate < 0.4 or avg_pnl < -0.005:
        best_action = "skip_recommended"
    else:
        best_action = "buy_cautious"

    return {
        "count": len(matches),
        "sufficient": True,
        "win_rate": round(win_rate, 3),
        "avg_pnl": round(avg_pnl, 4),
        "best_action": best_action,
        "skip_accuracy": round(skip_accuracy, 3),
        "total_buys": len(buys),
        "total_skips": len(skips),
    }


# ──────────────────────────────────────────────────────────
# Regime-Action Memory
# ──────────────────────────────────────────────────────────

def _load_regime_memory() -> dict:
    if REGIME_MEMORY_PATH.exists():
        try:
            with REGIME_MEMORY_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_regime_memory(data: dict) -> None:
    REGIME_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REGIME_MEMORY_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_regime_memory() -> dict:
    """경험 버퍼에서 레짐별 행동-결과 통계를 집계.

    Returns:
        {"bull/low_vol": {"buy": {"count": 20, "win_rate": 0.65, "avg_pnl": 0.008}, ...}, ...}
    """
    records = _load_experience()
    memory: dict[str, dict] = {}

    for r in records:
        if not r.get("evaluated") or r.get("pnl_pct") is None:
            continue

        regime = r.get("regime", "unknown")
        hmm = r.get("hmm_state", "unknown")
        key = f"{regime}/{hmm}"

        if key not in memory:
            memory[key] = {
                "buy": {"count": 0, "wins": 0, "pnl_sum": 0.0},
                "skip": {"count": 0, "correct": 0},
                "sell": {"count": 0, "wins": 0, "pnl_sum": 0.0},
                "recommended_confidence": 0.5,
                "recommended_kelly_adj": 1.0,
            }

        action = r["action"]
        if action in ("buy", "sell"):
            memory[key][action]["count"] += 1
            if r.get("outcome") == "win":
                memory[key][action]["wins"] += 1
            memory[key][action]["pnl_sum"] += r.get("pnl_pct", 0)
        elif action == "skip":
            memory[key]["skip"]["count"] += 1
            if r.get("outcome") == "correct_skip":
                memory[key]["skip"]["correct"] += 1

    # 레짐별 추천 파라미터 계산
    for key, data in memory.items():
        buy = data["buy"]
        if buy["count"] >= 5:
            win_rate = buy["wins"] / buy["count"]
            avg_pnl = buy["pnl_sum"] / buy["count"]

            # 승률+평균PnL 기반 신뢰도 조정
            if win_rate >= 0.6 and avg_pnl > 0:
                data["recommended_confidence"] = min(0.9, 0.5 + win_rate * 0.4)
                data["recommended_kelly_adj"] = 0.9  # 적극적
            elif win_rate < 0.4 or avg_pnl < -0.005:
                data["recommended_confidence"] = max(0.2, win_rate * 0.5)
                data["recommended_kelly_adj"] = 1.3  # 보수적
            else:
                data["recommended_confidence"] = 0.5
                data["recommended_kelly_adj"] = 1.0

            # 요약 통계 추가
            buy["win_rate"] = round(win_rate, 3)
            buy["avg_pnl"] = round(avg_pnl, 4)

        skip = data["skip"]
        if skip["count"] >= 3:
            skip["accuracy"] = round(skip["correct"] / skip["count"], 3)

    _save_regime_memory(memory)
    return memory


def get_regime_recommendation(regime: str, hmm_state: str) -> dict:
    """현재 레짐에 대한 과거 경험 기반 추천을 반환.

    Returns:
        {"confidence_adj": float, "kelly_adj": float, "reason": str, "data_points": int}
    """
    memory = _load_regime_memory()
    key = f"{regime}/{hmm_state}"

    if key not in memory:
        return {
            "confidence_adj": 1.0,
            "kelly_adj": 1.0,
            "reason": "경험 데이터 없음 — 기본값 사용",
            "data_points": 0,
        }

    data = memory[key]
    buy = data["buy"]

    if buy["count"] < 5:
        return {
            "confidence_adj": 1.0,
            "kelly_adj": 1.0,
            "reason": f"데이터 부족 ({buy['count']}건) — 기본값 사용",
            "data_points": buy["count"],
        }

    win_rate = buy.get("win_rate", 0.5)
    avg_pnl = buy.get("avg_pnl", 0)
    confidence_adj = data.get("recommended_confidence", 0.5) / 0.5  # 0.5 기준 배율
    kelly_adj = data.get("recommended_kelly_adj", 1.0)

    if win_rate >= 0.6:
        reason = f"경험 {buy['count']}건: 승률 {win_rate:.0%}, 평균 {avg_pnl:+.2%} → 적극적 매매"
    elif win_rate < 0.4:
        reason = f"경험 {buy['count']}건: 승률 {win_rate:.0%}, 평균 {avg_pnl:+.2%} → 보수적 전환"
    else:
        reason = f"경험 {buy['count']}건: 승률 {win_rate:.0%}, 평균 {avg_pnl:+.2%} → 중립"

    return {
        "confidence_adj": round(confidence_adj, 2),
        "kelly_adj": round(kelly_adj, 2),
        "reason": reason,
        "data_points": buy["count"],
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
    }


# ──────────────────────────────────────────────────────────
# Thompson Sampling — 전략별 적응적 배분
# ──────────────────────────────────────────────────────────

def _load_strategy_weights() -> dict:
    if STRATEGY_WEIGHTS_PATH.exists():
        try:
            with STRATEGY_WEIGHTS_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # Beta(2,2) prior — 약한 사전분포 (50%에서 시작)
    return {
        "etf": {"alpha": 2, "beta": 2, "trades": 0},
        "surge": {"alpha": 2, "beta": 2, "trades": 0},
    }


def _save_strategy_weights(data: dict) -> None:
    STRATEGY_WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STRATEGY_WEIGHTS_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_strategy_weights_from_experience() -> dict[str, float]:
    """경험 버퍼에서 전략별 성과를 집계하여 Thompson Sampling 파라미터 갱신.

    Returns:
        {"etf": ratio, "surge": ratio} — 합계 1.0 배분 비율
    """
    records = _load_experience()
    weights = _load_strategy_weights()

    # 기존 카운트 리셋 후 전체 재집계 (데이터 일관성)
    for key in weights:
        weights[key] = {"alpha": 2, "beta": 2, "trades": 0}

    for r in records:
        if not r.get("evaluated") or r["action"] != "buy":
            continue
        strategy = r.get("strategy", "unknown")
        if strategy not in weights:
            continue

        weights[strategy]["trades"] += 1
        if r.get("outcome") == "win":
            weights[strategy]["alpha"] += 1
        elif r.get("outcome") == "loss":
            weights[strategy]["beta"] += 1

    _save_strategy_weights(weights)

    # Thompson Sampling: Beta 분포의 기대값으로 배분
    # E[Beta(a,b)] = a / (a+b)
    expected = {}
    for key, params in weights.items():
        expected[key] = params["alpha"] / (params["alpha"] + params["beta"])

    total = sum(expected.values())
    if total <= 0:
        return {"etf": 0.6, "surge": 0.4}

    ratios = {k: round(v / total, 2) for k, v in expected.items()}

    # 극단 방지: 20%~80% 범위
    for k in ratios:
        ratios[k] = max(0.20, min(0.80, ratios[k]))

    # 재정규화
    total = sum(ratios.values())
    ratios = {k: round(v / total, 2) for k, v in ratios.items()}

    return ratios


def get_adaptive_allocation() -> dict[str, float]:
    """현재 Thompson Sampling 기반 전략 배분 비율 반환.

    학습 데이터 부족 시 기본값(60/40) 사용.
    """
    weights = _load_strategy_weights()
    total_trades = sum(w["trades"] for w in weights.values())

    if total_trades < 10:
        return {"etf": 0.60, "surge": 0.40}

    expected = {}
    for key, params in weights.items():
        expected[key] = params["alpha"] / (params["alpha"] + params["beta"])

    total = sum(expected.values())
    ratios = {k: round(v / total, 2) for k, v in expected.items()}

    for k in ratios:
        ratios[k] = max(0.20, min(0.80, ratios[k]))

    total = sum(ratios.values())
    return {k: round(v / total, 2) for k, v in ratios.items()}
