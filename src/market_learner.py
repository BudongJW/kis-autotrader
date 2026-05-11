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

    cfg = load_config()

    # 1. 시장 환경 갱신
    print("\n[1] 시장 환경 분석...")
    try:
        kospi_hist = fetch_recent_history(client, "069500", days=70)
        regime = analyze_regime(kospi_hist)
        print(f"  추세: {regime.trend} (점수 {regime.trend_score:+.3f})")
        print(f"  변동성: {regime.volatility} (백분위 {regime.vol_percentile:.1f}%)")
        print(f"  추천 K: {regime.recommended_k}")
    except Exception as e:
        print(f"  시장 환경 분석 실패: {e}")
        regime = None

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
    print(f"  KOSPI 5일: {breadth.get('kospi_5d', '?'):+.1f}%")
    print(f"  KOSDAQ 5일: {breadth.get('kosdaq_5d', '?'):+.1f}%")
    print(f"  건강도: {breadth.get('health', 'unknown')}")

    # 4. TA 가중치 최적화
    print("\n[4] TA 가중치 학습...")
    ta_accuracy = load_ta_accuracy()
    total_samples = sum(v["total"] for v in ta_accuracy.values())
    new_weights = optimize_ta_weights(ta_accuracy)
    print(f"  학습 데이터: {total_samples}건")
    for ind, w in new_weights.items():
        acc = ta_accuracy.get(ind, {})
        rate = acc["correct"] / acc["total"] * 100 if acc.get("total", 0) > 0 else 0
        old_w = DEFAULT_WEIGHTS.get(ind, 0)
        change = "=" if abs(w - old_w) < 0.01 else ("+" if w > old_w else "-")
        print(f"  {ind:<6} {w:.3f} ({change}) 적중률 {rate:.0f}% ({acc.get('total', 0)}건)")

    # 5. 시장 신뢰도 산출
    confidence = compute_market_confidence(regime, breadth, sectors) if regime else 0.5
    print(f"\n[5] 시장 신뢰도: {confidence:.1%}")

    # 6. strategy.yaml 업데이트
    print("\n[6] strategy.yaml 업데이트...")

    if regime:
        # K값: 시장 환경 기반 동적 조정
        current_k = cfg.get("strategies", {}).get("volatility_breakout", {}).get("k", 0.5)
        new_k = regime.recommended_k
        # 급격한 변화 방지: 전일 대비 최대 0.05 변경
        if abs(new_k - current_k) > 0.05:
            new_k = current_k + 0.05 * (1 if new_k > current_k else -1)
        new_k = round(new_k, 2)

        vb = cfg.setdefault("strategies", {}).setdefault("volatility_breakout", {})
        vb["k"] = new_k
        print(f"  K: {current_k} → {new_k}")

        cfg["market_regime"] = {
            "trend": regime.trend,
            "volatility": regime.volatility,
            "trend_score": regime.trend_score,
            "vol_percentile": regime.vol_percentile,
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    # TA 가중치 저장
    cfg.setdefault("strategies", {})["ta_weights"] = new_weights

    # 시장 신뢰도
    cfg["market_confidence"] = confidence

    # 강세 섹터 기록
    strong_sectors = [name for name, data in sectors.items()
                      if data.get("momentum") in ("strong", "positive")]
    cfg["strong_sectors"] = strong_sectors

    save_config(cfg)
    print(f"  시장 신뢰도: {confidence:.1%}")
    print(f"  강세 섹터: {', '.join(strong_sectors) if strong_sectors else '없음'}")

    # 7. 시장 로그 축적
    market_entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "regime_trend": regime.trend if regime else "unknown",
        "regime_volatility": regime.volatility if regime else "unknown",
        "trend_score": regime.trend_score if regime else 0,
        "vol_percentile": regime.vol_percentile if regime else 50,
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

    # 3. 누적 학습 데이터 요약
    ta_accuracy = load_ta_accuracy()
    total_evals = sum(v["total"] for v in ta_accuracy.values())
    print(f"\n[3] 누적 학습 현황:")
    print(f"  시장 데이터: {len(market_log)}일")
    print(f"  TA 평가: {total_evals}건")
    for ind, stats in ta_accuracy.items():
        rate = stats["correct"] / stats["total"] * 100 if stats["total"] > 0 else 0
        print(f"    {ind:<6} 적중률 {rate:.0f}% ({stats['total']}건)")

    print("\n장 후 학습 완료.")


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
    parser.add_argument("--phase", required=True, choices=["pre", "post"])
    args = parser.parse_args()

    client = KISClient()

    if args.phase == "pre":
        pre_market(client)
    elif args.phase == "post":
        post_market(client)


if __name__ == "__main__":
    main()
