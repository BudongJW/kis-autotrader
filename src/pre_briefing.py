"""장 전 사전 분석 브리핑 — 매일 08:30 KST 실행.

기존 pre_market 학습 이후 호출되어,
전 종목 돌파 목표가 사전 계산 + 멀티 타임프레임 분석 +
포트폴리오 리스크 스냅샷 + 종합 액션 플랜을 생성한다.

결과는 logs/pre_briefing.json에 저장되어
single_run.py 봇이 장 시작 즉시 사전 계산된 목표가를 활용한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.kis_client import KISClient
from src.bot.runner import fetch_recent_history
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.strategies.ta_composite import compute_ta_score
from src.risk_manager import (
    load_positions, get_kelly_position_size, get_drawdown_scale,
    compute_atr_for_position,
)
from src.utils.logger import log

CONFIG_PATH = Path("configs/strategy.yaml")
BRIEFING_PATH = Path("logs/pre_briefing.json")


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    from src.config_overrides import apply_user_overrides
    return apply_user_overrides(cfg)


# ──────────────────────────────────────────────────────────
# 1. 유니버스 전 종목 사전 돌파 목표가 계산
# ──────────────────────────────────────────────────────────

def precompute_breakout_targets(client: KISClient, universe: list[dict],
                                 k: float, trend_ma: int) -> list[dict]:
    """장 시작 전 전 종목의 돌파 목표가를 사전 계산.

    목표가 = 전일 종가 기준 추정 시가 + (전일 고가 - 전일 저가) × K
    실제 시가는 장 시작 후 확정되므로 전일 종가를 추정 시가로 사용.
    """
    targets = []

    for stock in universe:
        symbol = stock["symbol"]
        name = stock["name"]

        try:
            hist = fetch_recent_history(client, symbol, days=70)
            if len(hist) < trend_ma + 5:
                targets.append({
                    "symbol": symbol, "name": name,
                    "status": "데이터 부족",
                })
                continue

            close = hist["close"].astype(float)
            high = hist["high"].astype(float)
            low = hist["low"].astype(float)

            # 전일 데이터
            prev_close = float(close.iloc[-1])
            prev_high = float(high.iloc[-1])
            prev_low = float(low.iloc[-1])
            prev_range = prev_high - prev_low

            # 추정 목표가 (실제 시가 대신 전일 종가 사용)
            est_target = prev_close + prev_range * k
            # 실제 시가가 전일 종가와 다를 수 있으므로 범위도 제공
            target_low = est_target - prev_range * 0.02  # 시가 갭다운 감안
            target_high = est_target + prev_range * 0.02  # 시가 갭업 감안

            # 추세 필터
            ma = float(close.rolling(trend_ma).mean().iloc[-1])
            above_trend = prev_close > ma
            ma_distance = (prev_close - ma) / ma * 100  # MA와의 거리(%)

            # TA 점수
            ta = compute_ta_score(hist)

            # ATR (14일)
            atr = compute_atr_for_position(hist)
            atr_pct = (atr / prev_close * 100) if prev_close > 0 else 0

            # 5일/20일 모멘텀
            ret_5d = float((close.iloc[-1] / close.iloc[-5] - 1) * 100) if len(close) >= 5 else 0
            ret_20d = float((close.iloc[-1] / close.iloc[-20] - 1) * 100) if len(close) >= 20 else 0

            # 거래량 추세
            vol = hist["volume"].astype(float)
            vol_5d = float(vol.tail(5).mean())
            vol_20d = float(vol.tail(20).mean())
            vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 1.0

            # 돌파 확률 추정 (과거 K일간 돌파 성공률)
            breakout_count = 0
            total_days = min(20, len(hist) - 2)
            for i in range(2, total_days + 2):
                day = hist.iloc[-i]
                prev_day = hist.iloc[-i - 1]
                day_range = float(prev_day["high"]) - float(prev_day["low"])
                day_target = float(day["open"]) + day_range * k
                if float(day["high"]) >= day_target:
                    breakout_count += 1
            breakout_prob = breakout_count / total_days if total_days > 0 else 0

            # 종합 매수 적합도 점수 (0~100)
            score = 0
            if above_trend:
                score += 25
            if ta.total > 0:
                score += min(25, ta.total)
            if ret_5d > 0:
                score += min(15, ret_5d * 3)
            if vol_ratio > 1.1:
                score += 10
            if breakout_prob > 0.3:
                score += min(15, breakout_prob * 50)
            if atr_pct < 3:  # 적당한 변동성
                score += 10
            score = max(0, min(100, round(score)))

            targets.append({
                "symbol": symbol,
                "name": name,
                "prev_close": prev_close,
                "prev_range": round(prev_range),
                "est_target": round(est_target),
                "target_range": [round(target_low), round(target_high)],
                "above_trend": above_trend,
                "ma_distance_pct": round(ma_distance, 1),
                "ta_score": ta.total,
                "atr": round(atr),
                "atr_pct": round(atr_pct, 1),
                "ret_5d": round(ret_5d, 1),
                "ret_20d": round(ret_20d, 1),
                "vol_ratio": round(vol_ratio, 2),
                "breakout_prob": round(breakout_prob, 2),
                "buy_score": score,
                "status": "ready",
            })

        except Exception as e:
            targets.append({
                "symbol": symbol, "name": name,
                "status": f"실패: {e}",
            })

    # 매수 적합도 순 정렬
    targets.sort(key=lambda x: x.get("buy_score", 0), reverse=True)
    return targets


# ──────────────────────────────────────────────────────────
# 2. 멀티 타임프레임 분석
# ──────────────────────────────────────────────────────────

def analyze_multi_timeframe(client: KISClient, symbol: str = "069500") -> dict:
    """KOSPI 200 기준 주간/월간 추세 분석.

    일봉 데이터에서 주간/월간 관점의 추세를 추출하여
    일일 전략의 방향성을 보정한다.
    """
    try:
        hist = fetch_recent_history(client, symbol, days=120)
        close = hist["close"].astype(float)

        if len(close) < 60:
            return {"status": "데이터 부족"}

        # 일간 추세
        ma5 = float(close.rolling(5).mean().iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(60).mean().iloc[-1])
        cur = float(close.iloc[-1])

        daily_trend = "up" if ma5 > ma20 else "down"

        # 주간 관점 (20일 MA 기울기)
        ma20_series = close.rolling(20).mean().dropna()
        if len(ma20_series) >= 5:
            ma20_slope = float((ma20_series.iloc[-1] - ma20_series.iloc[-5]) / ma20_series.iloc[-5] * 100)
        else:
            ma20_slope = 0
        weekly_trend = "up" if ma20_slope > 0.3 else "down" if ma20_slope < -0.3 else "sideways"

        # 월간 관점 (60일 MA 기울기)
        ma60_series = close.rolling(60).mean().dropna()
        if len(ma60_series) >= 20:
            ma60_slope = float((ma60_series.iloc[-1] - ma60_series.iloc[-20]) / ma60_series.iloc[-20] * 100)
        else:
            ma60_slope = 0
        monthly_trend = "up" if ma60_slope > 1 else "down" if ma60_slope < -1 else "sideways"

        # MA 정배열/역배열
        if ma5 > ma20 > ma60:
            alignment = "정배열"  # 강세
            alignment_score = 1.0
        elif ma5 < ma20 < ma60:
            alignment = "역배열"  # 약세
            alignment_score = -1.0
        else:
            alignment = "혼조"
            alignment_score = 0.0

        # 현재가 vs 각 MA의 위치
        above_ma5 = cur > ma5
        above_ma20 = cur > ma20
        above_ma60 = cur > ma60

        # 추세 강도 종합 (-1.0 ~ 1.0)
        trend_strength = 0
        if above_ma5:
            trend_strength += 0.2
        if above_ma20:
            trend_strength += 0.3
        if above_ma60:
            trend_strength += 0.3
        trend_strength += alignment_score * 0.2
        trend_strength = max(-1.0, min(1.0, trend_strength))

        # 전략 권고
        if trend_strength > 0.5:
            recommendation = "적극 매수"
            size_multiplier = 1.2
        elif trend_strength > 0:
            recommendation = "일반 매수"
            size_multiplier = 1.0
        elif trend_strength > -0.3:
            recommendation = "소규모 매수"
            size_multiplier = 0.7
        else:
            recommendation = "매수 자제"
            size_multiplier = 0.4

        return {
            "status": "ready",
            "current_price": cur,
            "ma5": round(ma5), "ma20": round(ma20), "ma60": round(ma60),
            "daily_trend": daily_trend,
            "weekly_trend": weekly_trend,
            "weekly_slope": round(ma20_slope, 2),
            "monthly_trend": monthly_trend,
            "monthly_slope": round(ma60_slope, 2),
            "alignment": alignment,
            "above_ma": {"ma5": above_ma5, "ma20": above_ma20, "ma60": above_ma60},
            "trend_strength": round(trend_strength, 2),
            "recommendation": recommendation,
            "size_multiplier": size_multiplier,
        }

    except Exception as e:
        return {"status": f"실패: {e}"}


# ──────────────────────────────────────────────────────────
# 3. 포트폴리오 리스크 스냅샷
# ──────────────────────────────────────────────────────────

def get_portfolio_risk_snapshot(client: KISClient) -> dict:
    """현재 포트폴리오 리스크 상태 요약."""
    positions = load_positions()
    kelly_f = get_kelly_position_size("combined")
    dd_scale, dd_reason = get_drawdown_scale()

    # 보유 종목 상세
    holdings_detail = []
    total_exposure = 0

    for sym, pos in positions.items():
        buy_price = pos.get("buy_price", 0)
        qty = pos.get("qty", 0)
        atr = pos.get("atr_at_buy", 0)
        hold_days = pos.get("hold_days", 0)
        asset_type = pos.get("asset_type", "long")
        max_hold = pos.get("max_hold_days", 5)

        exposure = buy_price * qty
        total_exposure += exposure

        # 손절 라인
        if atr > 0:
            stop_price = buy_price - atr * 1.5
        else:
            stop_price = buy_price * 0.97

        holdings_detail.append({
            "symbol": sym,
            "buy_price": buy_price,
            "qty": qty,
            "exposure": exposure,
            "hold_days": hold_days,
            "max_hold_days": max_hold,
            "days_remaining": max(0, max_hold - hold_days),
            "asset_type": asset_type,
            "stop_price": round(stop_price),
            "atr": round(atr),
        })

    # 잔고 조회
    cash = 0
    try:
        resp = client.get_balance()
        if resp.get("rt_cd") == "0":
            o2 = resp.get("output2", [{}])
            if isinstance(o2, list) and o2:
                o2 = o2[0]
            cash = int(o2.get("dnca_tot_amt", 0))
    except Exception:
        pass

    total_value = cash + total_exposure
    exposure_pct = (total_exposure / total_value * 100) if total_value > 0 else 0
    cash_pct = 100 - exposure_pct

    # 매수 가능 여력
    kelly_cap = max(int(cash * kelly_f), int(cash * 0.10))
    available_buy = int(kelly_cap * dd_scale)

    # US 포지션
    us_positions = {}
    us_cash = 0
    try:
        from src.bot.us_session import load_us_positions, get_us_available_cash
        us_positions = load_us_positions()
        us_cash = get_us_available_cash(client)
    except Exception:
        pass

    return {
        "cash_krw": cash,
        "total_exposure_krw": total_exposure,
        "total_value_krw": total_value,
        "exposure_pct": round(exposure_pct, 1),
        "cash_pct": round(cash_pct, 1),
        "kr_positions": len(positions),
        "kr_holdings": holdings_detail,
        "kelly_f": round(kelly_f, 3),
        "drawdown_scale": dd_scale,
        "drawdown_reason": dd_reason,
        "available_buy_krw": available_buy,
        "us_positions": len(us_positions),
        "us_cash_usd": round(us_cash, 2),
    }


# ──────────────────────────────────────────────────────────
# 4. 종합 액션 플랜
# ──────────────────────────────────────────────────────────

def generate_action_plan(
    targets: list[dict],
    mtf: dict,
    risk: dict,
    cfg: dict,
) -> dict:
    """모든 분석 결과를 종합하여 오늘의 액션 플랜을 생성."""

    # 시장 상태 요약
    regime = cfg.get("market_regime", {})
    confidence = cfg.get("market_confidence", 0.5)
    gap = cfg.get("overnight_signal", {})
    bear_regime = "BULL"
    try:
        bear_state_path = Path("logs/bear_state.json")
        if bear_state_path.exists():
            import json as _json
            with bear_state_path.open("r", encoding="utf-8") as f:
                bear_regime = _json.load(f).get("regime", "BULL")
    except Exception:
        pass

    # 매수 대상 선별 (score 50 이상 + 추세 필터 통과)
    buy_candidates = [
        t for t in targets
        if t.get("status") == "ready"
        and t.get("buy_score", 0) >= 50
        and t.get("above_trend", False)
    ]

    # 멀티 타임프레임 권고 반영
    mtf_multiplier = mtf.get("size_multiplier", 1.0)

    # 오버나이트 갭 반영
    gap_action = gap.get("recommended_action", "normal")
    if gap_action == "skip":
        buy_candidates = []
        gap_note = "미국 급락 → 매수 스킵"
    elif gap_action == "aggressive_buy":
        gap_note = "미국 급등 → 적극 매수"
    elif gap_action == "reduce_size":
        mtf_multiplier *= 0.7
        gap_note = "미국 약세 → 규모 축소"
    else:
        gap_note = "정상"

    # 최종 매수 예산
    available = risk.get("available_buy_krw", 0)
    final_budget = int(available * mtf_multiplier)

    # 하락장이면 인버스 모드
    if bear_regime in ("BEAR", "CRISIS"):
        strategy_mode = "인버스/방어"
    elif bear_regime == "CAUTION":
        strategy_mode = "축소 매수 + 부분 방어"
    else:
        strategy_mode = "일반 매수"

    # 우선 매수 대상 (상위 3개)
    top_candidates = buy_candidates[:3]

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "market_summary": {
            "regime": regime.get("trend", "unknown"),
            "bear_regime": bear_regime,
            "confidence": confidence,
            "volatility": regime.get("volatility", "unknown"),
            "mtf_trend": mtf.get("alignment", "unknown"),
            "mtf_strength": mtf.get("trend_strength", 0),
            "gap_direction": gap.get("direction", "neutral"),
            "gap_note": gap_note,
        },
        "strategy_mode": strategy_mode,
        "budget": {
            "available_krw": available,
            "final_budget_krw": final_budget,
            "mtf_multiplier": mtf_multiplier,
            "kelly_f": risk.get("kelly_f", 0.1),
        },
        "risk_snapshot": {
            "exposure_pct": risk.get("exposure_pct", 0),
            "kr_positions": risk.get("kr_positions", 0),
            "us_positions": risk.get("us_positions", 0),
            "drawdown_scale": risk.get("drawdown_scale", 1.0),
        },
        "top_candidates": [
            {
                "symbol": t["symbol"],
                "name": t["name"],
                "target_price": t.get("est_target", 0),
                "buy_score": t.get("buy_score", 0),
                "ta_score": t.get("ta_score", 0),
                "breakout_prob": t.get("breakout_prob", 0),
            }
            for t in top_candidates
        ],
        "total_candidates": len(buy_candidates),
        "all_targets_count": len([t for t in targets if t.get("status") == "ready"]),
    }


# ──────────────────────────────────────────────────────────
# 메인: 장 전 브리핑 생성
# ──────────────────────────────────────────────────────────

def run_pre_briefing(client: KISClient) -> dict:
    """장 전 종합 브리핑 생성 → logs/pre_briefing.json 저장."""
    print("\n[장 전 브리핑] 종합 분석 시작...")

    cfg = load_config()
    vb = cfg.get("strategies", {}).get("volatility_breakout", {})
    k = vb.get("k", 0.5)
    trend_ma = vb.get("trend_ma", 20)

    # 유니버스 로드
    universe = cfg.get("universe", {}).get("default", [])
    # 동적 유니버스 추가
    dynamic = cfg.get("dynamic_universe", [])
    existing_syms = {s["symbol"] for s in universe}
    for d in dynamic:
        if d["symbol"] not in existing_syms:
            universe.append(d)

    # 1. 돌파 목표가 사전 계산
    print(f"\n  [1] 유니버스 {len(universe)}종목 돌파 목표가 계산 (K={k})...")
    targets = precompute_breakout_targets(client, universe, k, trend_ma)
    ready = [t for t in targets if t.get("status") == "ready"]
    print(f"      분석 완료: {len(ready)}/{len(universe)}종목")
    for t in targets[:5]:
        if t.get("status") == "ready":
            trend_mark = "+" if t.get("above_trend") else "-"
            print(f"      {t['name']:<20} 목표: {t['est_target']:>8,}원 "
                  f"TA={t['ta_score']:>+3.0f} 돌파확률={t['breakout_prob']:.0%} "
                  f"점수={t['buy_score']} [{trend_mark}]")

    # 2. 멀티 타임프레임 분석
    print(f"\n  [2] 멀티 타임프레임 분석...")
    mtf = analyze_multi_timeframe(client)
    if mtf.get("status") == "ready":
        print(f"      일간: {mtf['daily_trend']} | 주간: {mtf['weekly_trend']} "
              f"({mtf['weekly_slope']:+.1f}%) | 월간: {mtf['monthly_trend']} "
              f"({mtf['monthly_slope']:+.1f}%)")
        print(f"      정렬: {mtf['alignment']} | 추세강도: {mtf['trend_strength']:+.2f} "
              f"→ {mtf['recommendation']}")
    else:
        print(f"      {mtf.get('status', 'unknown')}")

    # 3. 포트폴리오 리스크 스냅샷
    print(f"\n  [3] 포트폴리오 리스크 스냅샷...")
    risk = get_portfolio_risk_snapshot(client)
    print(f"      현금: {risk['cash_krw']:,}원 ({risk['cash_pct']:.0f}%) "
          f"| 노출: {risk['total_exposure_krw']:,}원 ({risk['exposure_pct']:.0f}%)")
    print(f"      KR포지션: {risk['kr_positions']}개 | US포지션: {risk['us_positions']}개")
    print(f"      Kelly: {risk['kelly_f']:.0%} | DD스케일: {risk['drawdown_scale']:.2f} "
          f"({risk['drawdown_reason']})")
    print(f"      매수가능: {risk['available_buy_krw']:,}원")

    # 4. 종합 액션 플랜
    print(f"\n  [4] 종합 액션 플랜...")
    plan = generate_action_plan(targets, mtf, risk, cfg)
    ms = plan["market_summary"]
    print(f"      시장: {ms['regime']}/{ms['bear_regime']} | 신뢰도: {ms['confidence']:.0%}")
    print(f"      전략: {plan['strategy_mode']} | 예산: {plan['budget']['final_budget_krw']:,}원")
    print(f"      갭: {ms['gap_direction']} ({ms['gap_note']})")
    if plan["top_candidates"]:
        print(f"      매수 후보 ({plan['total_candidates']}종목 중 상위):")
        for c in plan["top_candidates"]:
            print(f"        {c['name']}: 목표 {c['target_price']:,}원 "
                  f"(점수={c['buy_score']}, TA={c['ta_score']:+.0f})")
    else:
        print(f"      매수 후보 없음.")

    # 결과 저장
    briefing = {
        "generated_at": datetime.now().isoformat(),
        "targets": targets,
        "multi_timeframe": mtf,
        "risk_snapshot": risk,
        "action_plan": plan,
    }

    BRIEFING_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BRIEFING_PATH.open("w", encoding="utf-8") as f:
        json.dump(briefing, f, ensure_ascii=False, indent=2)

    print(f"\n  [장 전 브리핑] logs/pre_briefing.json 저장 완료.")
    return briefing


def load_briefing() -> dict | None:
    """저장된 브리핑 로드 (single_run.py에서 활용)."""
    if not BRIEFING_PATH.exists():
        return None
    try:
        with BRIEFING_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # 오늘 생성된 브리핑만 유효
        gen_date = data.get("generated_at", "")[:10]
        if gen_date != datetime.now().strftime("%Y-%m-%d"):
            return None
        return data
    except Exception:
        return None


def get_precomputed_target(symbol: str) -> dict | None:
    """특정 종목의 사전 계산된 돌파 목표가 반환."""
    briefing = load_briefing()
    if not briefing:
        return None
    for t in briefing.get("targets", []):
        if t.get("symbol") == symbol and t.get("status") == "ready":
            return t
    return None
