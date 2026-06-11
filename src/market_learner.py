"""시장 학습 모듈 — 매일 장 전/장 후 시장 전체를 분석하고 전략 파라미터를 적응.

장 전 학습 (pre_market):
  1. 시장 환경(추세/변동성) 갱신 → K값 동적 조정
  2. 섹터 모멘텀 분석 → 강세 업종 탐지
  3. 외국인/기관 수급 동향 → 매수 신뢰도 반영
  4. 최근 TA 지표별 적중률 → 가중치 자동 조정

장 후 학습 (post_market):
  1. 오늘 TA 신호 vs 실제 결과 기록
  2. 전략별 성과 업데이트
  3. 시장 환경 로그 축적 (장기 학습 데이터)

사용:
    python -m src.market_learner --phase pre   # 장 전 (08:30 KST)
    python -m src.market_learner --phase post  # 장 후 (16:00 KST)
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.kis_client import KISClient
from src.bot.runner import fetch_recent_history
from src.market_regime import analyze_regime
from src.strategies.ta_composite import compute_ta_score, DEFAULT_WEIGHTS
from src.strategies.hmm_regime import detect_regime_from_prices, get_regime_action
from src.experience import (
    evaluate_outcomes, update_regime_memory, get_regime_recommendation,
    update_strategy_weights_from_experience, _load_experience,
)
from src.strategies.overnight_gap import get_overnight_signal
from src.adaptive_learning import run_adaptive_learning, run_us_post_learning
from src.strategies.signal_fusion import learn_fusion_weights
from src.pre_briefing import run_pre_briefing
from src.learning_diary import LearningDiary
from src.utils.logger import log

CONFIG_PATH = Path("configs/strategy.yaml")
MARKET_LOG_PATH = Path("logs/market_history.json")
TA_ACCURACY_PATH = Path("logs/ta_accuracy.json")
TRADE_LOG_PATH = Path("logs/trades.csv")

# 섹터 대표 ETF (업종별 모멘텀 추적)
SECTOR_ETFS = {
    "반도체":     "091160",   # KODEX 반도체
    "2차전지":    "305720",   # KODEX 2차전지산업
    "바이오":     "244580",   # KODEX 바이오
    "자동차":     "091180",   # KODEX 자동차
    "금융":       "102110",   # TIGER 200 금융
    "IT":         "098560",   # TIGER 미디어컨텐츠
    "철강":       "117700",   # KODEX 건설
    "KOSPI200":   "069500",   # KODEX 200
    "KOSDAQ150":  "229200",   # KODEX 코스닥150
    "미국나스닥": "395160",   # KODEX 미국나스닥100TR
}

# 외국인/기관 매매동향 추적용 (KOSPI 대표)
INSTITUTION_SYMBOLS = ["069500", "005930", "000660"]


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_market_log() -> list[dict]:
    if MARKET_LOG_PATH.exists():
        with MARKET_LOG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_market_log(data: list[dict]) -> None:
    MARKET_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 최근 90일만 유지
    data = data[-90:]
    with MARKET_LOG_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_ta_accuracy() -> dict:
    if TA_ACCURACY_PATH.exists():
        with TA_ACCURACY_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "rsi": {"correct": 0, "total": 0},
        "macd": {"correct": 0, "total": 0},
        "bb": {"correct": 0, "total": 0},
        "stoch": {"correct": 0, "total": 0},
        "adx": {"correct": 0, "total": 0},
        "ma": {"correct": 0, "total": 0},
        "obv": {"correct": 0, "total": 0},
        "mfi": {"correct": 0, "total": 0},
        "atr": {"correct": 0, "total": 0},
    }


def save_ta_accuracy(data: dict) -> None:
    TA_ACCURACY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TA_ACCURACY_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────
# 장 전 학습
# ──────────────────────────────────────────────────────────

def analyze_sector_momentum(client: KISClient) -> dict[str, dict]:
    """섹터별 최근 5일 모멘텀 분석."""
    results = {}
    for sector, symbol in SECTOR_ETFS.items():
        try:
            hist = fetch_recent_history(client, symbol, days=30)
            if len(hist) < 10:
                continue
            close = hist["close"].astype(float)
            # 5일 수익률
            ret_5d = float((close.iloc[-1] / close.iloc[-5] - 1) * 100)
            # 20일 수익률
            ret_20d = float((close.iloc[-1] / close.iloc[-20] - 1) * 100) if len(close) >= 20 else 0
            # 5일 평균 거래량 vs 20일 평균 거래량
            vol = hist["volume"].astype(float)
            vol_ratio = float(vol.tail(5).mean() / vol.tail(20).mean()) if vol.tail(20).mean() > 0 else 1.0

            results[sector] = {
                "symbol": symbol,
                "ret_5d": round(ret_5d, 2),
                "ret_20d": round(ret_20d, 2),
                "vol_ratio": round(vol_ratio, 2),
                "momentum": "strong" if ret_5d > 2 and vol_ratio > 1.2 else
                            "positive" if ret_5d > 0 else
                            "weak" if ret_5d > -2 else "negative",
            }
        except Exception as e:
            log.warning("sector_scan_failed", sector=sector, error=str(e))
    return results


def analyze_market_breadth(client: KISClient) -> dict:
    """시장 전체 건강도 분석 (KOSPI vs KOSDAQ 비교, 대형주 vs 소형주)."""
    try:
        kospi_hist = fetch_recent_history(client, "069500", days=30)  # KODEX 200
        kosdaq_hist = fetch_recent_history(client, "229200", days=30)  # KODEX 코스닥150

        kospi_close = kospi_hist["close"].astype(float)
        kosdaq_close = kosdaq_hist["close"].astype(float)

        # 최근 5일 성과
        kospi_5d = float((kospi_close.iloc[-1] / kospi_close.iloc[-5] - 1) * 100)
        kosdaq_5d = float((kosdaq_close.iloc[-1] / kosdaq_close.iloc[-5] - 1) * 100)

        # 시장 폭: KOSPI와 KOSDAQ이 모두 오르면 건강, 괴리가 크면 불안
        spread = abs(kospi_5d - kosdaq_5d)
        both_positive = kospi_5d > 0 and kosdaq_5d > 0
        both_negative = kospi_5d < 0 and kosdaq_5d < 0

        if both_positive and spread < 2:
            health = "strong"
        elif both_positive:
            health = "moderate"
        elif both_negative:
            health = "weak"
        else:
            health = "mixed"

        return {
            "kospi_5d": round(kospi_5d, 2),
            "kosdaq_5d": round(kosdaq_5d, 2),
            "spread": round(spread, 2),
            "health": health,
        }
    except Exception as e:
        log.warning("breadth_analysis_failed", error=str(e))
        return {"health": "unknown", "error": str(e)}


def optimize_ta_weights(ta_accuracy: dict) -> dict[str, float]:
    """TA 지표별 적중률 기반 가중치 최적화.

    적중률이 높은 지표에 더 높은 가중치를 부여.
    최소 30건 이상의 데이터가 쌓여야 기본값에서 벗어남.
    """
    weights = dict(DEFAULT_WEIGHTS)
    total_samples = sum(v["total"] for v in ta_accuracy.values())

    if total_samples < 30:
        return weights

    # 적중률 계산
    hit_rates = {}
    for indicator, stats in ta_accuracy.items():
        if stats["total"] >= 5:
            hit_rates[indicator] = stats["correct"] / stats["total"]
        else:
            hit_rates[indicator] = 0.5  # 데이터 부족 시 중립

    # 적중률 기반 가중치 (softmax-like)
    scores = {k: max(v, 0.1) for k, v in hit_rates.items()}  # 최소 0.1
    total = sum(scores.values())
    for k in scores:
        weights[k] = round(scores[k] / total, 3)

    # 극단적 편중 방지: 각 가중치를 0.05~0.35 범위로 클램프
    for k in weights:
        weights[k] = max(0.05, min(0.35, weights[k]))

    # 재정규화
    total = sum(weights.values())
    for k in weights:
        weights[k] = round(weights[k] / total, 3)

    return weights


def compute_optimal_k(history: pd.DataFrame, k_range: tuple = (0.3, 0.7),
                      step: float = 0.05, lookback: int = 30) -> dict:
    """Rolling backtest로 최적 K값을 탐색.

    최근 lookback 거래일 데이터로 각 K에서의 수익률·승률을 시뮬레이션.
    Returns: {"optimal_k": float, "win_rate": float, "expectancy": float, "detail": str}
    """
    if history is None or len(history) < lookback + 5:
        return {"optimal_k": 0.5, "win_rate": 0, "expectancy": 0,
                "detail": "데이터 부족, 기본 K=0.5"}

    close = history["close"].astype(float).values
    high = history["high"].astype(float).values
    low = history["low"].astype(float).values
    opn = history["open"].astype(float).values

    best_k, best_exp = 0.5, -999
    results = {}

    for k_int in range(int(k_range[0] * 100), int(k_range[1] * 100) + 1,
                        int(step * 100)):
        k = k_int / 100.0
        wins, losses = 0, 0
        pnl_sum = 0.0

        for i in range(-lookback, -1):
            prev_range = high[i - 1] - low[i - 1]
            target = opn[i] + prev_range * k
            if close[i] >= target:
                ret = (opn[i + 1] - close[i]) / close[i]
                pnl_sum += ret
                if ret > 0:
                    wins += 1
                else:
                    losses += 1

        total = wins + losses
        if total < 3:
            continue
        wr = wins / total
        avg_win = pnl_sum / total if total > 0 else 0
        exp = wr * abs(avg_win) if avg_win > 0 else avg_win
        results[k] = {"win_rate": wr, "trades": total, "expectancy": round(pnl_sum / total * 100, 3)}

        if pnl_sum / total > best_exp:
            best_exp = pnl_sum / total
            best_k = k

    best_info = results.get(best_k, {})
    return {
        "optimal_k": round(best_k, 2),
        "win_rate": round(best_info.get("win_rate", 0), 3),
        "expectancy": best_info.get("expectancy", 0),
        "trades": best_info.get("trades", 0),
        "detail": (f"Rolling {lookback}일 최적 K={best_k:.2f} "
                   f"(승률 {best_info.get('win_rate', 0):.0%}, "
                   f"기대값 {best_info.get('expectancy', 0):+.3f}%)"),
    }


def compute_market_confidence(regime, breadth: dict, sectors: dict) -> float:
    """시장 전체 신뢰도 점수 (0.0 ~ 1.0).

    1.0 = 적극 매수 가능, 0.0 = 매수 자제.
    single_run.py에서 매수 수량 조절에 사용.
    """
    score = 0.5  # 기본 중립

    # 추세 점수
    if regime.trend == "up":
        score += 0.15
    elif regime.trend == "down":
        score -= 0.2

    # 변동성
    if regime.volatility == "low":
        score += 0.1
    elif regime.volatility == "high":
        score -= 0.15

    # 시장 건강도
    health = breadth.get("health", "unknown")
    if health == "strong":
        score += 0.15
    elif health == "moderate":
        score += 0.05
    elif health == "weak":
        score -= 0.15
    elif health == "mixed":
        score -= 0.05

    # 강세 섹터 비율
    if sectors:
        strong_count = sum(1 for s in sectors.values() if s.get("momentum") in ("strong", "positive"))
        sector_ratio = strong_count / len(sectors)
        score += (sector_ratio - 0.5) * 0.2

    return max(0.0, min(1.0, round(score, 3)))


def pre_market(client: KISClient) -> None:
    """장 전 학습: 시장 분석 → 파라미터 업데이트."""
    print("=" * 60)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 장 전 시장 학습 시작")
    print("=" * 60)

    diary = LearningDiary("pre")
    cfg = load_config()

    # 이전 값 스냅샷 (변경 추적용)
    old_regime = cfg.get("market_regime", {})
    old_k = cfg.get("strategies", {}).get("volatility_breakout", {}).get("k", 0.5)
    old_confidence = cfg.get("market_confidence", 0.5)
    old_strong_sectors = cfg.get("strong_sectors", [])

    # 1. 시장 환경 갱신
    print("\n[1] 시장 환경 분석...")
    try:
        kospi_hist = fetch_recent_history(client, "069500", days=70)
        regime = analyze_regime(kospi_hist)
        print(f"  추세: {regime.trend} (점수 {regime.trend_score:+.3f})")
        print(f"  변동성: {regime.volatility} (백분위 {regime.vol_percentile:.1f}%)")
        print(f"  추천 K: {regime.recommended_k}")
        diary.record_metric("trend_score", regime.trend_score)
        diary.record_metric("vol_percentile", regime.vol_percentile)
        if old_regime.get("trend") != regime.trend:
            diary.record_change("시장체제", "trend", old_regime.get("trend", "?"), regime.trend, "선형회귀 추세 변경")
        if old_regime.get("volatility") != regime.volatility:
            diary.record_change("시장체제", "volatility", old_regime.get("volatility", "?"), regime.volatility)
    except Exception as e:
        print(f"  시장 환경 분석 실패: {e}")
        diary.record_error(f"시장 환경 분석 실패: {e}")
        regime = None

    # 1.5. HMM 레짐 탐지 (선형 분석 보완)
    hmm_regime = None
    hmm_action = None
    try:
        kospi_prices = kospi_hist["close"].astype(float) if regime else None
        if kospi_prices is not None and len(kospi_prices) >= 65:
            hmm_regime = detect_regime_from_prices(kospi_prices)
            hmm_action = get_regime_action(hmm_regime)
            print(f"\n  [HMM] {hmm_regime.detail}")
            print(f"  [HMM] 전환 확률: " +
                  ", ".join(f"{k}={v:.0%}" for k, v in hmm_regime.transition_prob.items()))
            print(f"  [HMM] 행동: {hmm_action['reason']}")
            old_hmm = old_regime.get("hmm_state", "unknown")
            if old_hmm != hmm_regime.state:
                diary.record_change("HMM", "hmm_state", old_hmm, hmm_regime.state,
                                    f"신뢰도 {hmm_regime.confidence:.0%}")
            diary.record_metric("hmm_confidence", hmm_regime.confidence)
            diary.record_decision(hmm_action["reason"])
    except Exception as e:
        print(f"  [HMM] 레짐 탐지 실패: {e}")
        diary.record_error(f"HMM 탐지 실패: {e}")

    # 2. 섹터 모멘텀
    print("\n[2] 섹터 모멘텀 스캔...")
    sectors = analyze_sector_momentum(client)
    for name, data in sorted(sectors.items(), key=lambda x: x[1].get("ret_5d", 0), reverse=True):
        emoji = {"strong": "+", "positive": " ", "weak": "-", "negative": "!"}
        m = data.get("momentum", "?")
        print(f"  [{emoji.get(m, '?')}] {name:<12} 5일 {data['ret_5d']:+.1f}%  "
              f"20일 {data['ret_20d']:+.1f}%  거래량비 {data['vol_ratio']:.1f}x")

    # 3. 시장 전체 건강도
    print("\n[3] 시장 건강도 분석...")
    breadth = analyze_market_breadth(client)
    kospi_5d = breadth.get('kospi_5d')
    kosdaq_5d = breadth.get('kosdaq_5d')
    print(f"  KOSPI 5일: {kospi_5d:+.1f}%" if isinstance(kospi_5d, (int, float)) else f"  KOSPI 5일: N/A")
    print(f"  KOSDAQ 5일: {kosdaq_5d:+.1f}%" if isinstance(kosdaq_5d, (int, float)) else f"  KOSDAQ 5일: N/A")
    print(f"  건강도: {breadth.get('health', 'unknown')}")

    # 4. TA 가중치 최적화
    print("\n[4] TA 가중치 학습...")
    ta_accuracy = load_ta_accuracy()
    total_samples = sum(v["total"] for v in ta_accuracy.values())
    old_ta_weights = dict(cfg.get("strategies", {}).get("ta_weights", DEFAULT_WEIGHTS))
    new_weights = optimize_ta_weights(ta_accuracy)
    print(f"  학습 데이터: {total_samples}건")
    for ind, w in new_weights.items():
        acc = ta_accuracy.get(ind, {})
        rate = acc["correct"] / acc["total"] * 100 if acc.get("total", 0) > 0 else 0
        old_w = DEFAULT_WEIGHTS.get(ind, 0)
        change = "=" if abs(w - old_w) < 0.01 else ("+" if w > old_w else "-")
        print(f"  {ind:<6} {w:.3f} ({change}) 적중률 {rate:.0f}% ({acc.get('total', 0)}건)")
        ow = old_ta_weights.get(ind, old_w)
        if abs(w - ow) >= 0.01:
            diary.record_change("TA가중치", ind, ow, w, f"적중률 {rate:.0f}%")
    diary.record_metric("ta_samples", total_samples)

    # 5. 시장 신뢰도 산출
    confidence = compute_market_confidence(regime, breadth, sectors) if regime else 0.5
    print(f"\n[5] 시장 신뢰도: {confidence:.1%}")
    diary.record_metric("market_health", breadth.get("health", "unknown"))

    # 6. strategy.yaml 업데이트
    print("\n[6] strategy.yaml 업데이트...")

    if regime:
        # K값: 3가지 소스의 가중 평균
        current_k = cfg.get("strategies", {}).get("volatility_breakout", {}).get("k", 0.5)
        regime_k = regime.recommended_k

        # (1) Rolling backtest 최적 K
        adaptive_k_info = compute_optimal_k(kospi_hist, lookback=30)
        adaptive_k = adaptive_k_info["optimal_k"]
        print(f"\n  [적응 K] {adaptive_k_info['detail']}")
        diary.record_metric("adaptive_k", adaptive_k)
        diary.record_metric("adaptive_k_trades", adaptive_k_info.get("trades", 0))

        # (2) 가중 혼합: 레짐 40% + 적응 K 40% + 현재 K 20% (관성)
        if adaptive_k_info.get("trades", 0) >= 5:
            blended_k = regime_k * 0.4 + adaptive_k * 0.4 + current_k * 0.2
        else:
            blended_k = regime_k * 0.7 + current_k * 0.3

        # 급격한 변화 방지: 전일 대비 최대 0.05 변경
        if abs(blended_k - current_k) > 0.05:
            blended_k = current_k + 0.05 * (1 if blended_k > current_k else -1)
        new_k = round(blended_k, 2)

        vb = cfg.setdefault("strategies", {}).setdefault("volatility_breakout", {})
        vb["k"] = new_k
        print(f"  K: {current_k} → {new_k} (레짐={regime_k}, 적응={adaptive_k})")

        # (3) HMM K값 조정 적용
        if hmm_action:
            new_k = round(new_k * hmm_action["k_adjustment"], 2)
            new_k = max(0.3, min(0.7, new_k))
            vb["k"] = new_k
            print(f"  K (HMM 조정): → {new_k}")

        diary.record_change("전략", "K", old_k, new_k,
                            f"레짐+적응K+HMM (적응K={adaptive_k}, 승률={adaptive_k_info.get('win_rate', 0):.0%})")

        cfg["market_regime"] = {
            "trend": regime.trend,
            "volatility": regime.volatility,
            "trend_score": regime.trend_score,
            "vol_percentile": regime.vol_percentile,
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        if hmm_regime:
            cfg["market_regime"]["hmm_state"] = hmm_regime.state
            cfg["market_regime"]["hmm_confidence"] = hmm_regime.confidence
            cfg["market_regime"]["hmm_transition"] = hmm_regime.transition_prob

    # TA 가중치 저장
    cfg.setdefault("strategies", {})["ta_weights"] = new_weights

    # 시장 신뢰도
    cfg["market_confidence"] = confidence
    diary.record_change("시장", "confidence", old_confidence, confidence, "체제+건강도+섹터 종합")

    # 강세 섹터 기록 + 동적 유니버스 보강
    strong_sectors = [name for name, data in sectors.items()
                      if data.get("momentum") in ("strong", "positive")]
    cfg["strong_sectors"] = strong_sectors

    added_sectors = set(strong_sectors) - set(old_strong_sectors)
    removed_sectors = set(old_strong_sectors) - set(strong_sectors)
    if added_sectors:
        diary.record_change("섹터", "강세진입", list(removed_sectors)[:3] if removed_sectors else "없음",
                            list(added_sectors), "모멘텀 전환")
    if removed_sectors and not added_sectors:
        diary.record_change("섹터", "강세이탈", list(removed_sectors), "없음")

    # 강세 섹터 ETF를 동적 유니버스에 추가 (일봉이 없는 죽은 심볼은 제외 —
    # 098560이 매일 재추가돼 봇·하니스에서 평가실패 노이즈를 내던 문제)
    current_syms = {s["symbol"] for s in cfg.get("universe", {}).get("default", [])}
    dynamic_adds = []
    for name in strong_sectors:
        if name in SECTOR_ETFS:
            sym = SECTOR_ETFS[name]
            if sym in current_syms:
                continue
            try:
                hist = fetch_recent_history(client, sym, days=30)
                if hist is None or len(hist) < 22:
                    print(f"  [동적 유니버스] {name}({sym}) 일봉 부족/없음 — 제외(죽은 심볼)")
                    continue
            except Exception:
                print(f"  [동적 유니버스] {name}({sym}) 일봉 조회 실패 — 제외")
                continue
            dynamic_adds.append({"symbol": sym, "name": f"KODEX {name}"})
    cfg["dynamic_universe"] = dynamic_adds
    if dynamic_adds:
        names = [d["name"] for d in dynamic_adds]
        print(f"  동적 유니버스 추가: {', '.join(names)}")

    save_config(cfg)
    print(f"  시장 신뢰도: {confidence:.1%}")
    print(f"  강세 섹터: {', '.join(strong_sectors) if strong_sectors else '없음'}")

    # 6.3. 섹터 로테이션 (주 1회 — 월요일)
    try:
        from src.strategies.sector_rotation import run_sector_rotation, is_rotation_day
        if is_rotation_day():
            flow_cache = {}
            try:
                from src.strategies.flow_signal import load_flow_cache
                flow_cache = load_flow_cache()
            except Exception:
                pass
            rotation = run_sector_rotation(sectors, flow_cache)
            print(f"\n  [로테이션] {rotation.detail}")
            diary.record_change("섹터로테이션", "top_sectors",
                                old_strong_sectors[:3], rotation.top_sectors[:3],
                                rotation.detail)
            diary.record_decision(f"섹터 로테이션: {rotation.detail}")
        else:
            print(f"\n  [로테이션] 월요일에만 실행 (오늘: {datetime.now().strftime('%A')})")
    except Exception as e:
        print(f"\n  [로테이션] 실패: {e}")
        diary.record_error(f"섹터 로테이션 실패: {e}")

    # 6.5. 오버나이트 갭 신호 (미국장 종가 → 한국장 방향)
    print("\n[6.5] 오버나이트 갭 신호...")
    try:
        gap_signal = get_overnight_signal(client)
        print(f"  {gap_signal.detail}")
        print(f"  방향: {gap_signal.direction} | 강도: {gap_signal.strength:.2f} | "
              f"추천: {gap_signal.recommended_action}")

        cfg["overnight_signal"] = gap_signal.as_dict

        # 신뢰도에 갭 신호 반영
        if gap_signal.confidence_boost != 0:
            old_conf = confidence
            confidence = max(0.0, min(1.0, confidence + gap_signal.confidence_boost))
            cfg["market_confidence"] = round(confidence, 3)
            print(f"  신뢰도 조정: {old_conf:.0%} → {confidence:.0%} "
                  f"(갭 {gap_signal.confidence_boost:+.3f})")
            diary.record_change("갭신호", "confidence", old_conf, round(confidence, 3),
                                f"{gap_signal.direction} 갭 반영")
        diary.record_metric("gap_direction", gap_signal.direction)
        diary.record_metric("gap_strength", gap_signal.strength)
    except Exception as e:
        print(f"  오버나이트 갭 신호 실패: {e}")
        diary.record_error(f"갭 신호 실패: {e}")

    # 7. 경험 기반 추천 (과거 유사 레짐에서의 결과)
    print("\n[7] 경험 기반 레짐 추천...")
    if regime:
        regime_trend = regime.trend
        hmm_st = hmm_regime.state if hmm_regime else "unknown"
        rec = get_regime_recommendation(regime_trend, hmm_st)
        print(f"  {rec['reason']}")
        if rec.get("data_points", 0) >= 5:
            # 경험이 충분하면 신뢰도에 반영
            exp_adj = rec.get("confidence_adj", 1.0)
            adjusted = max(0.1, min(1.0, confidence * exp_adj))
            if abs(adjusted - confidence) > 0.03:
                print(f"  신뢰도 조정: {confidence:.0%} → {adjusted:.0%} (경험 반영)")
                confidence = adjusted
                cfg["market_confidence"] = confidence

    # 7.5. ETF 전략 성과 업데이트 (Thompson Sampling)
    adaptive = update_strategy_weights_from_experience()
    cfg["adaptive_allocation"] = adaptive
    print(f"  ETF 전략: 승률 {adaptive.get('win_rate', 0.5):.0%} "
          f"({adaptive.get('trades', 0)}건)")
    diary.record_metric("etf_win_rate", adaptive.get("win_rate", 0.5))
    diary.record_metric("etf_trades", adaptive.get("trades", 0))

    save_config(cfg)

    # 시장 로그 축적
    market_entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "regime_trend": regime.trend if regime else "unknown",
        "regime_volatility": regime.volatility if regime else "unknown",
        "trend_score": regime.trend_score if regime else 0,
        "vol_percentile": regime.vol_percentile if regime else 50,
        "hmm_state": hmm_regime.state if hmm_regime else "unknown",
        "hmm_confidence": hmm_regime.confidence if hmm_regime else 0,
        "breadth": breadth.get("health", "unknown"),
        "kospi_5d": breadth.get("kospi_5d", 0),
        "kosdaq_5d": breadth.get("kosdaq_5d", 0),
        "confidence": confidence,
        "strong_sectors": strong_sectors,
        "k_value": cfg.get("strategies", {}).get("volatility_breakout", {}).get("k", 0.5),
    }
    history = load_market_log()
    # 같은 날짜 중복 방지
    history = [h for h in history if h.get("date") != market_entry["date"]]
    history.append(market_entry)
    save_market_log(history)

    # 8. 장 전 종합 브리핑 (돌파 목표가 사전 계산 + 멀티 타임프레임 + 리스크)
    print("\n[8] 장 전 종합 브리핑...")
    try:
        briefing = run_pre_briefing(client)
        plan = briefing.get("action_plan", {})
        print(f"  전략: {plan.get('strategy_mode', '?')} | "
              f"후보: {plan.get('total_candidates', 0)}종목 | "
              f"예산: {plan.get('budget', {}).get('final_budget_krw', 0):,}원")
    except Exception as e:
        print(f"  브리핑 생성 실패: {e}")
        diary.record_error(f"브리핑 생성 실패: {e}")

    # 9. 시즌 필터 상태 기록
    try:
        from src.strategies.seasonal import get_seasonal_adjustment
        seasonal = get_seasonal_adjustment()
        diary.record_metric("season", seasonal["season"])
        diary.record_metric("seasonal_confidence_mult", seasonal["confidence_mult"])
        diary.record_decision(f"시즌 필터: {seasonal['reason']}")
        print(f"\n[9] 시즌 필터: {seasonal['reason']}")
    except Exception as e:
        diary.record_error(f"시즌 필터 로드 실패: {e}")

    # 10. VAA 월간 리밸런싱 (매월 첫 영업일)
    try:
        from src.strategies.vaa_rebalance import run_vaa_rebalance
        vaa_signal = run_vaa_rebalance(client, cfg)
        if vaa_signal:
            diary.record_change("VAA", "mode", "N/A", vaa_signal.mode, vaa_signal.detail)
            diary.record_metric("vaa_target", vaa_signal.target_symbol)
            diary.record_metric("vaa_momentum", vaa_signal.target_momentum)
            diary.record_decision(f"VAA 리밸런싱: {vaa_signal.detail}")
            print(f"\n[10] VAA 리밸런싱: {vaa_signal.detail}")
        else:
            print(f"\n[10] VAA: 리밸런싱 대상일 아님 (매월 초 실행)")
    except Exception as e:
        print(f"\n[10] VAA 리밸런싱 실패: {e}")
        diary.record_error(f"VAA 리밸런싱 실패: {e}")

    diary.save()
    print(f"\n학습 일지 기록 완료 (변경 {len(diary.changes)}건, 오류 {len(diary.errors)}건)")
    print("\n장 전 학습 완료.")


# ──────────────────────────────────────────────────────────
# 장 후 학습
# ──────────────────────────────────────────────────────────

def evaluate_ta_signals(client: KISClient) -> None:
    """오늘 TA 신호 vs 실제 가격 변동 비교 → 적중률 업데이트.

    매수 신호가 있었던 종목의 이후 가격 변동으로 지표 정확도를 평가.
    """
    ta_accuracy = load_ta_accuracy()

    # 오늘 거래한 종목 추출
    today_str = datetime.now().strftime("%Y-%m-%d")
    traded_symbols = set()
    trade_results = {}  # symbol -> pnl_pct

    if TRADE_LOG_PATH.exists():
        buys = {}
        with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row.get("timestamp", "").startswith(today_str):
                    continue
                symbol = row.get("symbol", "")
                side = row.get("side", "")
                price = int(row.get("price", 0))

                if side == "buy":
                    buys[symbol] = price
                    traded_symbols.add(symbol)
                elif side == "sell" and symbol in buys:
                    pnl_pct = (price - buys[symbol]) / buys[symbol]
                    trade_results[symbol] = pnl_pct

    if not traded_symbols:
        print("  오늘 거래 없음. TA 평가 스킵.")
        return

    # 거래한 종목의 TA 점수를 재계산하고 결과와 비교
    for symbol in traded_symbols:
        try:
            hist = fetch_recent_history(client, symbol, days=70)
            ta = compute_ta_score(hist)

            # 실제 결과 판단
            pnl = trade_results.get(symbol)
            if pnl is None:
                # 아직 매도 안 된 종목 — 현재가로 평가
                from src.bot.single_run import get_price
                cur_price = get_price(client, symbol)
                if cur_price > 0 and symbol in buys:
                    pnl = (cur_price - buys[symbol]) / buys[symbol]

            if pnl is None:
                continue

            was_profitable = pnl > 0

            # 각 지표 방향이 실제 결과와 맞았는지 평가
            indicators = {
                "rsi": ta.rsi_score,
                "macd": ta.macd_score,
                "bb": ta.bb_score,
                "stoch": ta.stoch_score,
                "adx": ta.adx_score,
                "ma": ta.ma_score,
                "obv": ta.obv_score,
                "mfi": ta.mfi_score,
                "atr": ta.atr_score,
            }

            for ind, score in indicators.items():
                if ind not in ta_accuracy:
                    ta_accuracy[ind] = {"correct": 0, "total": 0}
                ta_accuracy[ind]["total"] += 1
                # 양수 점수 + 수익 = 맞음, 음수 점수 + 손실 = 맞음
                if (score > 0 and was_profitable) or (score < 0 and not was_profitable):
                    ta_accuracy[ind]["correct"] += 1

            print(f"  {symbol}: PnL {pnl:+.2%} — TA 평가 기록")

        except Exception as e:
            log.warning("ta_eval_failed", symbol=symbol, error=str(e))

    save_ta_accuracy(ta_accuracy)
    print(f"  TA 적중률 업데이트 완료 (누적 {sum(v['total'] for v in ta_accuracy.values())}건)")


def analyze_market_patterns(market_log: list[dict]) -> dict:
    """시장 로그 패턴 분석 — 반복되는 패턴 탐지.

    최근 N일 시장 데이터에서:
    - 연속 상승/하락 패턴
    - 신뢰도 vs 실제 성과 관계
    - 변동성 사이클
    """
    if len(market_log) < 10:
        return {"pattern": "insufficient_data", "days": len(market_log)}

    recent = market_log[-30:]
    trends = [h.get("regime_trend", "unknown") for h in recent]
    confidences = [h.get("confidence", 0.5) for h in recent]

    # 연속 추세 카운트
    current_trend = trends[-1]
    streak = 1
    for t in reversed(trends[:-1]):
        if t == current_trend:
            streak += 1
        else:
            break

    # 평균 신뢰도 추이
    avg_conf_recent = np.mean(confidences[-5:]) if len(confidences) >= 5 else 0.5
    avg_conf_prior = np.mean(confidences[-15:-5]) if len(confidences) >= 15 else 0.5
    conf_trend = "improving" if avg_conf_recent > avg_conf_prior + 0.05 else \
                 "declining" if avg_conf_recent < avg_conf_prior - 0.05 else "stable"

    return {
        "current_trend": current_trend,
        "trend_streak": streak,
        "avg_confidence_5d": round(float(avg_conf_recent), 3),
        "confidence_direction": conf_trend,
        "data_days": len(market_log),
    }


def post_market(client: KISClient) -> None:
    """장 후 학습: 거래 결과 평가 → 모델 피드백."""
    print("=" * 60)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 장 후 학습 시작")
    print("=" * 60)

    diary = LearningDiary("post")

    # 1. TA 신호 적중률 평가
    print("\n[1] TA 신호 평가...")
    evaluate_ta_signals(client)

    # 2. 시장 패턴 분석
    print("\n[2] 시장 패턴 분석...")
    market_log = load_market_log()
    patterns = analyze_market_patterns(market_log)
    print(f"  현재 추세: {patterns.get('current_trend')} ({patterns.get('trend_streak')}일 연속)")
    print(f"  5일 평균 신뢰도: {patterns.get('avg_confidence_5d', 0):.1%}")
    print(f"  신뢰도 방향: {patterns.get('confidence_direction')}")
    diary.record_metric("trend_streak", patterns.get("trend_streak", 0))
    diary.record_metric("confidence_direction", patterns.get("confidence_direction", "?"))
    diary.record_metric("market_data_days", len(market_log))

    # 3. 경험 버퍼 결과 평가
    print("\n[3] 경험 평가...")
    try:
        # 오늘 거래 결과 수집
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_trades: dict[str, dict] = {}
        buys_today: dict[str, int] = {}

        if TRADE_LOG_PATH.exists():
            with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not row.get("timestamp", "").startswith(today_str):
                        continue
                    sym = row.get("symbol", "")
                    side = row.get("side", "")
                    price = int(row.get("price", 0))
                    if side == "buy":
                        buys_today[sym] = price
                    elif side == "sell" and sym in buys_today:
                        buy_p = buys_today[sym]
                        today_trades[sym] = {
                            "buy_price": buy_p,
                            "sell_price": price,
                            "pnl_pct": (price - buy_p) / buy_p if buy_p > 0 else 0,
                        }

        # 보유 종목 미실현 손익
        holdings_pnl: dict[str, float] = {}
        try:
            from src.bot.single_run import get_all_holdings, get_price
            holdings = get_all_holdings(client) or {}  # 조회 실패(None) 방어
            from src.risk_manager import load_positions
            positions = load_positions()
            for sym, qty in holdings.items():
                pos = positions.get(sym, {})
                buy_p = pos.get("buy_price", 0)
                if buy_p > 0:
                    cur_p = get_price(client, sym)
                    if cur_p > 0:
                        holdings_pnl[sym] = (cur_p - buy_p) / buy_p
        except Exception:
            pass

        evaluate_outcomes(holdings_pnl, today_trades)
        exp_records = _load_experience()
        today_exp = [r for r in exp_records if r.get("date") == today_str]
        evaluated = [r for r in today_exp if r.get("evaluated")]
        wins = [r for r in evaluated if r.get("outcome") == "win"]
        print(f"  오늘 결정: {len(today_exp)}건, 평가 완료: {len(evaluated)}건, "
              f"성공: {len(wins)}건")
        diary.record_metric("decisions_today", len(today_exp))
        diary.record_metric("evaluated", len(evaluated))
        diary.record_metric("wins", len(wins))
        if today_trades:
            diary.record_decision(f"오늘 거래 {len(today_trades)}건 평가 완료 "
                                  f"(성공 {len(wins)}/{len(evaluated)})")
        else:
            diary.record_decision("오늘 거래 없음 — TA 평가 스킵")
    except Exception as e:
        print(f"  경험 평가 실패: {e}")
        diary.record_error(f"경험 평가 실패: {e}")

    # 4. 레짐-행동 메모리 갱신
    print("\n[4] 레짐-행동 메모리 갱신...")
    try:
        regime_memory = update_regime_memory()
        for key, data in regime_memory.items():
            buy = data["buy"]
            if buy["count"] >= 3:
                wr = buy.get("win_rate", 0)
                avg = buy.get("avg_pnl", 0)
                print(f"  {key}: {buy['count']}건, 승률 {wr:.0%}, 평균 {avg:+.2%}")
    except Exception as e:
        print(f"  레짐 메모리 갱신 실패: {e}")

    # 5. Thompson Sampling ETF 성과 갱신
    print("\n[5] ETF 전략 성과 갱신 (Thompson Sampling)...")
    try:
        adaptive = update_strategy_weights_from_experience()
        print(f"  ETF 승률: {adaptive.get('win_rate', 0.5):.0%} "
              f"({adaptive.get('trades', 0)}건)")
    except Exception as e:
        print(f"  전략 성과 갱신 실패: {e}")

    # 6. LGBM 일일 재학습 (warm-start)
    print("\n[6] LGBM 일일 재학습...")
    try:
        from src.strategies.lgbm_predictor import daily_retrain
        cfg = load_config()
        universe = cfg.get("universe", {}).get("default", [])
        symbols = [s["symbol"] for s in universe[:3]]
        if not symbols:
            symbols = ["069500"]
        result = daily_retrain(client, symbols, days=120)
        if result:
            print(f"  완료: accuracy={result['accuracy']:.1%}, AUC={result['auc']:.3f}")
            diary.record_metric("lgbm_accuracy", result["accuracy"])
            diary.record_metric("lgbm_auc", result["auc"])
            diary.record_decision(f"LGBM 재학습 완료 (accuracy={result['accuracy']:.1%}, AUC={result['auc']:.3f})")
        else:
            print("  데이터 부족 또는 lightgbm 미설치 — 스킵")
            diary.record_decision("LGBM 스킵 (데이터 부족)")
    except Exception as e:
        print(f"  LGBM 재학습 실패: {e}")
        diary.record_error(f"LGBM 재학습 실패: {e}")

    # 7. 적응형 학습 (오버나이트갭·보유기간·섹터모멘텀 성과 평가)
    cfg = load_config()
    try:
        cfg = run_adaptive_learning(client, cfg)
        save_config(cfg)
        diary.record_decision("적응형 학습 5개 서브시스템 실행 완료")
    except Exception as e:
        print(f"  적응 학습 실패: {e}")
        diary.record_error(f"적응 학습 실패: {e}")

    # 7.5. 신호 융합 가중치 학습 (Brier Score 최적화)
    print("\n[7.5] 신호 융합 가중치 학습...")
    try:
        fusion_weights = learn_fusion_weights()
        if fusion_weights:
            print(f"  학습 완료: {fusion_weights}")
            diary.record_decision(f"신호 융합 가중치 학습 완료: {fusion_weights}")
        else:
            print("  경험 데이터 부족 (20건 미만). 기본 가중치 사용.")
            diary.record_decision("신호 융합 학습 스킵 (경험 20건 미만)")
    except Exception as e:
        print(f"  융합 가중치 학습 실패: {e}")
        diary.record_error(f"융합 가중치 학습 실패: {e}")

    # 8. 수급 데이터 수집 (pykrx)
    print("\n[8] 수급 데이터 수집...")
    try:
        from src.strategies.flow_signal import compute_flow_signal, save_flow_cache, FlowSignal
        cfg_reload = load_config()
        flow_universe = cfg_reload.get("universe", {}).get("default", [])
        flows: dict[str, FlowSignal] = {}
        for asset in flow_universe:
            sym = asset["symbol"]
            sig = compute_flow_signal(sym, days=5)
            if sig:
                flows[sym] = sig
                if sig.signal != 0:
                    print(f"  {asset.get('name', sym)}: {sig.detail} → {sig.signal:+.2f}")
        if flows:
            save_flow_cache(flows)
            diary.record_metric("flow_symbols", len(flows))
            diary.record_decision(f"수급 데이터 {len(flows)}종목 수집 완료")
        else:
            print("  수급 데이터 없음 (pykrx 미설치 또는 데이터 부족)")
    except Exception as e:
        print(f"  수급 수집 실패: {e}")
        diary.record_error(f"수급 수집 실패: {e}")

    # 9. 포트폴리오 테일 리스크 (VaR/ES)
    print("\n[9] 테일 리스크 분석...")
    try:
        from src.strategies.tail_risk import compute_portfolio_var
        from src.risk_manager import load_positions
        positions = load_positions()
        held_syms = list(positions.keys())
        if held_syms:
            held_hists = {}
            for sym in held_syms[:10]:
                try:
                    held_hists[sym] = fetch_recent_history(client, sym, days=70)
                except Exception:
                    pass
            if held_hists:
                tr = compute_portfolio_var(held_hists)
                print(f"  VaR₉₅={tr.var_95:.2%} | ES₉₅={tr.es_95:.2%} | "
                      f"Vol={tr.portfolio_vol:.1%} | MDD={tr.max_drawdown:.2%}")
                print(f"  리스크: {tr.risk_level} (배율 {tr.size_mult:.0%})")
                diary.record_metric("var_95", tr.var_95)
                diary.record_metric("es_95", tr.es_95)
                diary.record_metric("risk_level", tr.risk_level)
                diary.record_decision(f"테일 리스크: {tr.detail}")
            else:
                print("  보유 종목 히스토리 없음")
        else:
            print("  보유 포지션 없음 — 테일 리스크 스킵")
    except Exception as e:
        print(f"  테일 리스크 분석 실패: {e}")
        diary.record_error(f"테일 리스크 분석 실패: {e}")

    # 10. 누적 학습 데이터 요약
    ta_accuracy = load_ta_accuracy()
    total_evals = sum(v["total"] for v in ta_accuracy.values())
    print(f"\n[10] 누적 학습 현황:")
    print(f"  시장 데이터: {len(market_log)}일")
    print(f"  TA 평가: {total_evals}건")
    total_exp = len(_load_experience())
    print(f"  경험 버퍼: {total_exp}건")
    diary.record_metric("ta_total_evals", total_evals)
    diary.record_metric("experience_buffer", total_exp)

    diary.save()
    print(f"\n학습 일지 기록 완료 (변경 {len(diary.changes)}건, 오류 {len(diary.errors)}건)")
    print("\n장 후 학습 완료.")


# ──────────────────────────────────────────────────────────
# 미국장 후 학습
# ──────────────────────────────────────────────────────────

def post_us_market(client: KISClient) -> None:
    """미국장 종료 후 학습: US 거래 평가 + 교차 시장 + 한국장 사전 준비.

    06:30 KST에 실행 — 미국장 폐장 직후.
    """
    print("=" * 60)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 미국장 후 학습 시작")
    print("=" * 60)

    diary = LearningDiary("post_us")
    cfg = load_config()
    old_confidence = cfg.get("market_confidence", 0.5)

    # 1. US 거래 결과 평가 + 교차 시장 데이터
    try:
        cfg = run_us_post_learning(client, cfg)
        save_config(cfg)
        diary.record_decision("US 거래 결과 평가 + 교차 시장 학습 완료")
    except Exception as e:
        print(f"  US 학습 실패: {e}")
        diary.record_error(f"US 학습 실패: {e}")

    # 2. US 결과 기반 한국장 전략 사전 조정
    print("\n[사전 조정] US 결과 → 한국장 전략 반영...")
    try:
        from src.bot.us_session import fetch_us_history

        # QQQ/SPY 종가 확인 → 오버나이트 갭 방향 사전 추정
        qqq_change = 0
        spy_change = 0
        try:
            qqq_hist = fetch_us_history(client, "QQQ", "NASD", days=5)
            if len(qqq_hist) >= 2:
                qqq_change = float((qqq_hist["close"].iloc[-1] / qqq_hist["close"].iloc[-2] - 1) * 100)

            spy_hist = fetch_us_history(client, "SPY", "AMEX", days=5)  # SPY는 NYSE Arca → AMEX
            if len(spy_hist) >= 2:
                spy_change = float((spy_hist["close"].iloc[-1] / spy_hist["close"].iloc[-2] - 1) * 100)
        except Exception:
            pass

        print(f"  QQQ: {qqq_change:+.2f}% | SPY: {spy_change:+.2f}%")

        # 미국장 결과에 따라 한국장 시가 갭 방향 예측
        avg_us = (qqq_change + spy_change) / 2
        if avg_us <= -2:
            predicted_gap = "bearish"
            gap_action = "reduce_size"
            confidence_adj = -0.15
        elif avg_us <= -1:
            predicted_gap = "bearish"
            gap_action = "reduce_size"
            confidence_adj = -0.08
        elif avg_us >= 2:
            predicted_gap = "bullish"
            gap_action = "aggressive_buy"
            confidence_adj = 0.10
        elif avg_us >= 1:
            predicted_gap = "bullish"
            gap_action = "normal"
            confidence_adj = 0.05
        else:
            predicted_gap = "neutral"
            gap_action = "normal"
            confidence_adj = 0

        # 교차 시장 데이터 반영
        cross_params = cfg.get("cross_market_params", {})
        gap_weight = cross_params.get("gap_signal_weight", 1.0)
        confidence_adj *= gap_weight

        cfg["overnight_signal"] = {
            "direction": predicted_gap,
            "recommended_action": gap_action,
            "nasdaq_change": round(qqq_change, 2),
            "sp500_change": round(spy_change, 2),
            "strength": round(abs(avg_us) / 3, 2),
            "confidence_boost": round(confidence_adj, 3),
            "source": "us_post_learning",
        }

        # 신뢰도 사전 조정
        current_conf = cfg.get("market_confidence", 0.5)
        new_conf = max(0.1, min(1.0, current_conf + confidence_adj))
        cfg["market_confidence"] = round(new_conf, 3)

        print(f"  예측: {predicted_gap} | 행동: {gap_action} | "
              f"신뢰도: {current_conf:.0%} → {new_conf:.0%}")

        diary.record_metric("qqq_change", round(qqq_change, 2))
        diary.record_metric("spy_change", round(spy_change, 2))
        diary.record_change("갭예측", "direction", "이전", predicted_gap,
                            f"QQQ {qqq_change:+.1f}%, SPY {spy_change:+.1f}%")
        diary.record_change("갭예측", "confidence", old_confidence, round(new_conf, 3),
                            f"행동: {gap_action}")

        save_config(cfg)
    except Exception as e:
        print(f"  사전 조정 실패: {e}")
        diary.record_error(f"사전 조정 실패: {e}")

    # 3. LGBM warm-start (US 데이터 포함 재학습)
    print("\n[LGBM] 모델 갱신...")
    try:
        from src.strategies.lgbm_predictor import daily_retrain
        universe = cfg.get("universe", {}).get("default", [])
        symbols = [s["symbol"] for s in universe[:3]]
        if not symbols:
            symbols = ["069500"]
        result = daily_retrain(client, symbols, days=120)
        if result:
            print(f"  완료: accuracy={result['accuracy']:.1%}, AUC={result['auc']:.3f}")
            diary.record_metric("lgbm_accuracy", result["accuracy"])
            diary.record_metric("lgbm_auc", result["auc"])
            diary.record_decision(f"LGBM 갱신 완료 (accuracy={result['accuracy']:.1%})")
        else:
            print("  데이터 부족 또는 스킵")
            diary.record_decision("LGBM 스킵 (데이터 부족)")
    except Exception as e:
        print(f"  LGBM 갱신 실패: {e}")
        diary.record_error(f"LGBM 갱신 실패: {e}")

    diary.save()
    print(f"\n학습 일지 기록 완료 (변경 {len(diary.changes)}건, 오류 {len(diary.errors)}건)")
    print("\n미국장 후 학습 완료.")


# ──────────────────────────────────────────────────────────
# 장 중 적응 (single_run.py에서 호출)
# ──────────────────────────────────────────────────────────

def get_market_confidence() -> float:
    """현재 시장 신뢰도 반환 (strategy.yaml에서 로드)."""
    try:
        cfg = load_config()
        return cfg.get("market_confidence", 0.5)
    except Exception:
        return 0.5


def get_intraday_regime_adjustment(client: KISClient) -> dict:
    """장 중 시장 환경 변화 감지.

    장 전 분석과 현재 상황이 크게 달라졌는지 체크.
    반환: {"adjust_k": float, "reduce_size": bool, "reason": str}
    """
    try:
        cfg = load_config()
        morning_trend = cfg.get("market_regime", {}).get("trend_score", 0)

        hist = fetch_recent_history(client, "069500", days=30)
        close = hist["close"].astype(float)

        # 오늘 장중 움직임 (가능하면 최근 봉 기준)
        today_change = float((close.iloc[-1] / close.iloc[-2] - 1)) if len(close) >= 2 else 0

        # 장 전 상승 추세였는데 오늘 -1.5% 이상 급락 → 보수적 전환
        if morning_trend > 0 and today_change < -0.015:
            return {
                "adjust_k": 0.05,  # K를 높여서 진입 보수적으로
                "reduce_size": True,
                "reason": f"장중 급락 감지 ({today_change:+.1%}), 보수적 전환",
            }
        # 장 전 하락 추세였는데 오늘 +1.5% 반등 → 소폭 적극적
        elif morning_trend < 0 and today_change > 0.015:
            return {
                "adjust_k": -0.03,
                "reduce_size": False,
                "reason": f"장중 반등 감지 ({today_change:+.1%}), 소폭 적극 전환",
            }

        return {"adjust_k": 0, "reduce_size": False, "reason": "장중 환경 변화 없음"}

    except Exception as e:
        return {"adjust_k": 0, "reduce_size": False, "reason": f"장중 분석 실패: {e}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="시장 학습 모듈")
    parser.add_argument("--phase", required=True, choices=["pre", "post", "post_us"])
    args = parser.parse_args()

    client = KISClient()

    if args.phase == "pre":
        pre_market(client)
    elif args.phase == "post":
        post_market(client)
    elif args.phase == "post_us":
        post_us_market(client)


if __name__ == "__main__":
    main()
