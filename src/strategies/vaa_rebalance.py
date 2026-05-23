"""VAA (Vigilant Asset Allocation) 월간 리밸런싱.

Wouter Keller의 VAA 전략 변형 — 국내 ETF로 구현.
카나리아 자산(유로, 금)의 모멘텀이 음전환하면 방어 자산으로 이동,
양전환 시 공격 자산 중 모멘텀 최고 자산에 집중.

리밸런싱 주기: 매월 첫 영업일 (장전 학습 시점에 시그널 생성)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import yaml


@dataclass
class VAASignal:
    """월간 VAA 리밸런싱 결과."""
    date: str
    mode: str  # "offensive" | "defensive"
    target_symbol: str
    target_name: str
    target_momentum: float
    canary_bad: int  # 음전환 카나리아 수
    canary_scores: dict[str, float] = field(default_factory=dict)
    offensive_scores: dict[str, float] = field(default_factory=dict)
    defensive_scores: dict[str, float] = field(default_factory=dict)
    detail: str = ""


def compute_vaa_signal(
    canary_histories: dict[str, dict],
    offensive_histories: dict[str, dict],
    defensive_histories: dict[str, dict],
    cfg: dict | None = None,
) -> VAASignal:
    """VAA 시그널 계산.

    Args:
        canary_histories: {심볼: {"prices": pd.Series, "name": str}}
        offensive_histories: {심볼: {"prices": pd.Series, "name": str}}
        defensive_histories: {심볼: {"prices": pd.Series, "name": str}}
        cfg: bear_strategy.canary_alert 설정

    Returns:
        VAASignal with target asset and mode
    """
    from src.strategies.bear_strategy import weighted_momentum

    if cfg is None:
        cfg = {}
    canary_cfg = cfg.get("canary_alert", {})
    months = canary_cfg.get("momentum_months", [1, 3, 6, 12])
    weights = canary_cfg.get("momentum_weights", [12, 4, 2, 1])

    canary_scores = {}
    bad_count = 0
    for sym, info in canary_histories.items():
        prices = info["prices"]
        if prices is not None and len(prices) >= 22:
            score = weighted_momentum(prices, months, weights)
        else:
            score = 0.0
        canary_scores[sym] = round(score, 4)
        if score <= 0:
            bad_count += 1

    offensive_scores = {}
    for sym, info in offensive_histories.items():
        prices = info["prices"]
        if prices is not None and len(prices) >= 22:
            score = weighted_momentum(prices, months, weights)
        else:
            score = 0.0
        offensive_scores[sym] = round(score, 4)

    defensive_scores = {}
    for sym, info in defensive_histories.items():
        prices = info["prices"]
        if prices is not None and len(prices) >= 22:
            score = weighted_momentum(prices, months, weights)
        else:
            score = 0.0
        defensive_scores[sym] = round(score, 4)

    today_str = datetime.now().strftime("%Y-%m-%d")

    if bad_count > 0:
        if defensive_scores:
            best_sym = max(defensive_scores, key=defensive_scores.get)
            best_name = defensive_histories[best_sym].get("name", best_sym)
            best_score = defensive_scores[best_sym]
        else:
            best_sym, best_name, best_score = "cash", "현금", 0.0
        return VAASignal(
            date=today_str,
            mode="defensive",
            target_symbol=best_sym,
            target_name=best_name,
            target_momentum=best_score,
            canary_bad=bad_count,
            canary_scores=canary_scores,
            offensive_scores=offensive_scores,
            defensive_scores=defensive_scores,
            detail=f"카나리아 {bad_count}개 음전환 → 방어: {best_name} (모멘텀 {best_score:+.4f})",
        )
    else:
        if offensive_scores:
            best_sym = max(offensive_scores, key=offensive_scores.get)
            best_name = offensive_histories[best_sym].get("name", best_sym)
            best_score = offensive_scores[best_sym]
        else:
            best_sym, best_name, best_score = "cash", "현금", 0.0
        return VAASignal(
            date=today_str,
            mode="offensive",
            target_symbol=best_sym,
            target_name=best_name,
            target_momentum=best_score,
            canary_bad=0,
            canary_scores=canary_scores,
            offensive_scores=offensive_scores,
            defensive_scores=defensive_scores,
            detail=f"카나리아 전부 양전환 → 공격: {best_name} (모멘텀 {best_score:+.4f})",
        )


def is_rebalance_day() -> bool:
    """매월 첫 영업일인지 판단 (월초 1~5일, 평일)."""
    now = datetime.now()
    if now.day > 5:
        return False
    return now.weekday() < 5  # 월~금


def load_vaa_state() -> dict:
    """이전 VAA 시그널 로드."""
    path = Path("logs/vaa_state.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_vaa_state(signal: VAASignal) -> None:
    """VAA 시그널 저장."""
    path = Path("logs/vaa_state.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    state = load_vaa_state()
    state["current"] = asdict(signal)

    history = state.get("history", [])
    history.append({
        "date": signal.date,
        "mode": signal.mode,
        "target": signal.target_symbol,
        "target_name": signal.target_name,
        "momentum": signal.target_momentum,
        "canary_bad": signal.canary_bad,
    })
    if len(history) > 24:
        history = history[-24:]
    state["history"] = history

    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_vaa_universe(cfg: dict) -> dict:
    """strategy.yaml에서 VAA 유니버스 추출.

    Returns:
        {
            "canary": [{"symbol": ..., "name": ...}, ...],
            "offensive": [...],
            "defensive": [...],
        }
    """
    universe = cfg.get("universe", {})
    bear_cfg = cfg.get("bear_strategy", {})

    canary = bear_cfg.get("canary", universe.get("canary", []))
    defensive = universe.get("defensive", [])
    offensive = universe.get("default", [])

    return {
        "canary": canary if isinstance(canary, list) else [],
        "offensive": offensive,
        "defensive": defensive,
    }


def run_vaa_rebalance(client, cfg: dict | None = None) -> VAASignal | None:
    """VAA 월간 리밸런싱 실행.

    장전 학습에서 호출. 매월 첫 영업일에만 실행.
    """
    if not is_rebalance_day():
        return None

    if cfg is None:
        cfg_path = Path("configs/strategy.yaml")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    from src.bot.runner import fetch_recent_history

    universe = get_vaa_universe(cfg)
    bear_cfg = cfg.get("bear_strategy", {})

    def _fetch_prices(assets: list[dict]) -> dict:
        result = {}
        for asset in assets:
            sym = asset["symbol"]
            name = asset.get("name", sym)
            try:
                hist = fetch_recent_history(client, sym, days=260)
                result[sym] = {"prices": hist["close"], "name": name}
            except Exception:
                result[sym] = {"prices": None, "name": name}
        return result

    canary_h = _fetch_prices(universe["canary"])
    offensive_h = _fetch_prices(universe["offensive"])
    defensive_h = _fetch_prices(universe["defensive"])

    signal = compute_vaa_signal(canary_h, offensive_h, defensive_h, bear_cfg)
    save_vaa_state(signal)
    return signal
