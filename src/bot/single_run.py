"""자동매매 봇 — 1분 간격 연속 감시 + 단일 실행 겸용.

실행 모드:
  --loop     : 1분 간격 연속 감시 (장 시작~마감). GitHub Actions 장시간 실행용.
  (기본)     : 1회 체크 후 종료. 5분 cron 백업용.

전략: ETF 변동성 돌파 (전체 자본 집중)
  - 오버나이트 갭 신호 (미국장 종가 → 한국장 방향)
  - TA 복합 점수 + LGBM 예측 필터
  - Kelly Criterion 포지션 사이징

리스크 관리 (1분마다 체크):
  - 장중 손절매: ATR×1.5 기반 동적 손절 (ATR 없으면 고정 -3%)
  - 추적 손절: ATR×2.0 수익 도달 후 고점 대비 ATR×1.0 하락 시 매도
  - 동적 ROI: 보유 시간별 최소 수익률 도달 시 청산
  - 터뷸런스 필터: KOSPI200 변동성 급등 시 신규 매수 차단
"""

from __future__ import annotations

import argparse
import os
import sys
import time as time_mod
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from src.config import settings
from src.kis_client import KISClient
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.strategies.ta_composite import compute_ta_score, TAScore
from src.bot.runner import fetch_recent_history, get_holding_qty
from src.tracker import log_trade, get_summary
from src.risk_manager import (
    check_stop_loss, check_turbulence, record_buy, record_pyramid,
    remove_position,
    load_positions, save_positions, get_kelly_position_size, should_hold_overnight,
    check_daily_loss_limit, check_max_positions, compute_atr_for_position,
    get_drawdown_scale, PARTIAL_SELL_RATIO,
)
from src.market_learner import get_market_confidence, get_intraday_regime_adjustment
from src.execution.twap import TWAPEngine
from src.strategies.lgbm_predictor import get_prediction_filter
from src.strategies.signal_fusion import fuse_signals, BUY_THRESHOLD, STRONG_BUY_THRESHOLD
from src.experience import log_decision, get_regime_recommendation
from src.adaptive_learning import record_hold_outcome, record_sector_trade
from src.pre_briefing import load_briefing, get_precomputed_target
from src.strategies.bear_strategy import (
    detect_market_regime, compute_bear_allocation, inverse_breakout_signal,
    compute_annualized_vol, log_bear_trade, get_adaptive_params,
    leveraged_entry_allowed,
)
from src.safety import killswitch
from src.safety.order_gates import check_order
from src.strategies.r_multiple import log_r_multiple
from src.utils.logger import log


def _safe_order_cash(client, symbol: str, qty: int, price: float, side: str) -> dict:
    """check_order 안전장치 통과 시에만 client.order_cash 호출.
    차단되면 rt_cd='G'(Gate)인 가짜 거부 응답 반환. 모든 매수/매도 통과 지점.

    모든 주문 시도(체결·차단)를 SQLite 원장에 기록.
    """
    from src.safety.ledger import record_order_attempt

    ok, reason = check_order(symbol, qty, price, side)
    if not ok:
        print(f"    ⚠️ 주문 차단: {reason}")
        record_order_attempt(side, symbol, qty, price, "blocked",
                             gate_reason=reason)
        # 차단도 텔레그램 알람 (선택, 너무 많으면 끌 수 있음)
        try:
            from src.safety.notifier import notify_error
            notify_error(f"주문 차단: {reason}", context=f"{side} {symbol} {qty}@{price}")
        except Exception:
            pass
        return {"rt_cd": "G", "msg1": f"안전장치: {reason}", "msg_cd": "GATE_BLOCKED"}

    resp = client.order_cash(symbol, qty=qty, price=price, side=side)
    rt = resp.get("rt_cd", "")
    status = "executed" if rt == "0" else ("rejected" if rt == "E" else "error")
    record_order_attempt(side, symbol, qty, price, status,
                         reason=resp.get("msg1", "")[:200])
    return resp

KST = ZoneInfo("Asia/Seoul")

MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 20)
MARKET_END = dtime(15, 30)
SELL_WINDOW_END = dtime(9, 10)

CONFIG_PATH = Path("configs/strategy.yaml")

def _now() -> datetime:
    """KST 기준 현재 시각. GitHub Actions(UTC) 환경에서도 안전."""
    return datetime.now(KST)


# ETF 전략에 전체 자본 집중 (급등주 전략 폐지)
DEFAULT_ETF_RATIO = 1.0

# 루프 간격 (초)
RISK_CHECK_INTERVAL = 60       # 리스크 체크: 1분
STRATEGY_CHECK_INTERVAL = 300  # 전략 체크: 5분 (기본)
STRATEGY_CHECK_EARLY = 120     # 장 초반(09:00~10:00) 전략 체크: 2분
TURBULENCE_CHECK_INTERVAL = 180  # 터뷸런스 체크: 3분
EARLY_SESSION_END = dtime(10, 0)  # 장 초반 종료 시각

# 루프 자체 최대 실행시간(초). GitHub Actions 잡 하드 타임아웃(360분)에
# 걸려 강제 종료되면 정리 스텝(거래기록 업로드·저널 푸시)이 스킵돼 체결 기록이
# 유실된다(2026-06-02 498400 매수 기록 유실 사례). 하드 한도보다 먼저 스스로
# 정상 종료(break)해 정리 스텝을 보장하고, 다음 스케줄 run이 세션을 이어받는다.
# 340분 = 잡 셋업(~3분)+정리(~3분)에도 360분 한도까지 여유 확보.
MAX_LOOP_RUNTIME_SEC = 340 * 60


def _runtime_exceeded(loop_start_epoch: float, now_epoch: float,
                      limit_sec: float = MAX_LOOP_RUNTIME_SEC) -> bool:
    """루프 시작 후 limit_sec 이상 경과했는지. 하드 타임아웃 전 정상 종료 판정용."""
    return (now_epoch - loop_start_epoch) >= limit_sec


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_universe() -> list[dict]:
    """기본 유니버스 + 섹터 모멘텀 기반 동적 추가분."""
    cfg = load_config()
    universe = list(cfg.get("universe", {}).get("default", []))
    dynamic = cfg.get("dynamic_universe", [])
    if dynamic:
        existing = {s["symbol"] for s in universe}
        for d in dynamic:
            if d["symbol"] not in existing:
                universe.append(d)
    return universe


def load_strategy_params() -> dict:
    return load_config().get("strategies", {}).get("volatility_breakout", {})


def load_risk_params() -> dict:
    return load_config().get("risk", {})


def load_ta_weights() -> dict | None:
    """strategy.yaml에서 TA 가중치를 로드. 없으면 None (기본값 사용)."""
    w = load_config().get("strategies", {}).get("ta_weights")
    if w and isinstance(w, dict):
        return w
    return None


def get_all_holdings(client: KISClient) -> dict[str, int]:
    result = {}
    try:
        resp = client.get_balance()
        if resp.get("rt_cd") == "0":
            for item in resp.get("output1", []):
                qty = int(item.get("hldg_qty", 0))
                if qty > 0:
                    result[item.get("pdno", "")] = qty
    except Exception as e:
        log.error("get_all_holdings_failed", error=str(e))
    return result


def get_available_cash(client: KISClient) -> int:
    try:
        resp = client.get_balance()
        if resp.get("rt_cd") == "0":
            cash = resp.get("output2", [{}])
            if cash:
                c = cash[0]
                available = int(c.get("ord_psbl_cash", 0))
                if available <= 0:
                    available = int(c.get("dnca_tot_amt", 0))
                return available
    except Exception:
        pass
    return 0


def get_price(client: KISClient, symbol: str) -> int:
    try:
        resp = client.get_price(symbol)
        if resp.get("rt_cd") == "0":
            return int(resp["output"]["stck_prpr"])
    except Exception:
        pass
    return 0


def sell_holdings(client: KISClient, holdings: dict[str, int], universe_syms: set,
                  label: str, dry_run: bool,
                  twap_engine: TWAPEngine | None = None) -> None:
    """보유 종목 매도. TWAP 엔진이 있으면 분할 매도."""
    for symbol, qty in holdings.items():
        price = get_price(client, symbol)
        tag = "ETF" if symbol in universe_syms else "급등주"
        print(f"  [{label}] {tag} {symbol} {qty}주 @ ~{price:,}원")

        if twap_engine and label not in ("시가매도", "장마감청산"):
            # 일반 매도만 TWAP 분할. 시가매도/장마감청산은 즉시 전량.
            twap_engine.submit(symbol, qty, "sell", tag, price)
            continue

        if not dry_run:
            resp = _safe_order_cash(client, symbol, qty, price, "sell")
            rt = resp.get("rt_cd")
            print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
            if rt == "0":
                log_trade(symbol, tag, "sell", qty, price)
                # 보유 기간 결과 기록 (적응 학습용)
                positions = load_positions()
                pos = positions.get(symbol, {})
                buy_p = pos.get("buy_price", 0)
                hold_d = pos.get("hold_days", 0)
                if buy_p > 0 and price > 0:
                    pnl = (price - buy_p) / buy_p
                    action_type = "held" if hold_d > 0 else "sold_at_open"
                    record_hold_outcome(symbol, hold_d, action_type, buy_p, price, pnl)
                r_val = log_r_multiple(symbol, price)
                if r_val is not None:
                    print(f"    R배수: {r_val:+.2f}R")
                remove_position(symbol)
                log.info(f"{label}_sell", symbol=symbol, qty=qty, price=price)
            elif rt == "E":
                log.warning(f"{label}_sell_error", symbol=symbol, msg=resp.get("msg1", ""))
        else:
            print("    (dry-run)")


def check_risk_and_sell(client: KISClient, holdings: dict[str, int],
                        universe_syms: set, dry_run: bool) -> dict[str, int]:
    """보유 종목에 대해 리스크 체크 (손절/추적손절/분할매도/ROI). 매도 후 남은 보유분 반환."""
    remaining = dict(holdings)

    for symbol, qty in holdings.items():
        price = get_price(client, symbol)
        if price <= 0:
            continue

        should_sell, reason = check_stop_loss(symbol, price)
        if should_sell:
            is_partial = reason.startswith("[분할]")
            sell_qty = max(1, int(qty * PARTIAL_SELL_RATIO)) if is_partial else qty

            tag = "ETF" if symbol in universe_syms else "급등주"
            label = "분할매도" if is_partial else "리스크"
            print(f"  [{label}] {tag} {symbol} {sell_qty}/{qty}주 @ {price:,}원 — {reason}")
            if not dry_run:
                resp = _safe_order_cash(client, symbol, sell_qty, price, "sell")
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, tag, "sell", sell_qty, price)
                    if is_partial:
                        remaining[symbol] = qty - sell_qty
                        positions = load_positions()
                        if symbol in positions:
                            positions[symbol]["qty"] = qty - sell_qty
                            save_positions(positions)
                    else:
                        r_val = log_r_multiple(symbol, price)
                        if r_val is not None:
                            print(f"    R배수: {r_val:+.2f}R")
                        remove_position(symbol)
                        remaining.pop(symbol, None)
                elif rt == "E":
                    log.warning("risk_sell_error", symbol=symbol, msg=resp.get("msg1", ""))
            else:
                print("    (dry-run)")
                if not is_partial:
                    remaining.pop(symbol, None)
                else:
                    remaining[symbol] = qty - sell_qty

    return remaining


def run_etf_strategy(client: KISClient, budget: int, holdings: dict,
                     universe: list[dict], dry_run: bool,
                     twap_engine: TWAPEngine | None = None) -> int:
    """ETF 변동성 돌파 전략. 섹터 모멘텀 기반으로 우선순위 정렬. 사용한 금액을 반환."""
    universe_syms = {s["symbol"] for s in universe}

    etf_held = {s: q for s, q in holdings.items() if s in universe_syms}
    # 피라미딩: 기존 보유 중이라도 수익 중이면 추가 매수 허용
    pyramid_mode = False
    if etf_held:
        positions = load_positions()
        can_pyramid = False
        for sym, qty in etf_held.items():
            pos = positions.get(sym, {})
            buy_p = pos.get("buy_price", 0)
            pyramid_count = pos.get("pyramid_count", 0)
            if buy_p > 0 and pyramid_count < 3:
                cur_p = get_price(client, sym)
                pnl = (cur_p - buy_p) / buy_p if buy_p > 0 else 0
                if pnl >= 0.02:  # +2% 이상 수익 시 피라미딩 가능
                    can_pyramid = True
                    print(f"  [피라미딩] {sym} 수익 {pnl:+.1%} — 추가 매수 가능 "
                          f"({pyramid_count + 1}/3단위)")
        if not can_pyramid:
            syms = ", ".join(f"{s}({q}주)" for s, q in etf_held.items())
            print(f"  [ETF] 보유 중: {syms}. 리스크 관리 대기.")
            return 0
        pyramid_mode = True
        budget = int(budget * 0.5)  # 피라미딩은 반 사이즈

    params = load_strategy_params()
    k = params.get("k", 0.5)
    ma = params.get("trend_ma", 20)
    strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)

    # 섹터 모멘텀 기반 유니버스 우선순위 정렬
    cfg = load_config()
    strong = cfg.get("strong_sectors", [])
    if strong:
        def _sector_priority(stock: dict) -> int:
            name = stock["name"]
            for i, sec in enumerate(strong):
                if sec in name:
                    return i  # 강세 순위 그대로
            return len(strong)  # 강세 아닌 것은 뒤로
        universe = sorted(universe, key=_sector_priority)
        print(f"  [ETF] 강세 섹터 우선: {', '.join(strong)}")

    print(f"  [ETF] K={k}, MA={ma} | 배정: {budget:,}원")

    # 사전 브리핑 데이터 활용
    briefing = load_briefing()
    if briefing:
        plan = briefing.get("action_plan", {})
        mtf = briefing.get("multi_timeframe", {})
        if mtf.get("status") == "ready":
            print(f"  [브리핑] 추세: {mtf.get('alignment', '?')} "
                  f"(강도 {mtf.get('trend_strength', 0):+.2f}) "
                  f"→ {mtf.get('recommendation', '?')}")
        candidates = plan.get("total_candidates", 0)
        if candidates > 0:
            top = plan.get("top_candidates", [])
            top_names = [c["name"] for c in top[:3]]
            print(f"  [브리핑] 매수 후보 {candidates}종목: {', '.join(top_names)}")

    strong_sectors = set(cfg.get("strong_sectors", []))

    for stock in universe:
        symbol = stock["symbol"]
        name = stock["name"]
        is_strong = any(sec in name for sec in strong_sectors)
        try:
            history = fetch_recent_history(client, symbol, days=70)
            signal = strategy.generate_signal(symbol, history)
            cur_price = int(signal.price)

            ta_weights = load_ta_weights()
            ta = compute_ta_score(history, weights=ta_weights)
            print(f"  [ETF] {name} {signal.type.value} @ {cur_price:,}원 — {signal.reason}")
            print(f"    TA분석: {ta.detail}")

            # 사전 분석 데이터 참조
            pre_target = get_precomputed_target(symbol)
            if pre_target:
                print(f"    [사전] 목표: {pre_target['est_target']:,}원 "
                      f"| 돌파확률: {pre_target['breakout_prob']:.0%} "
                      f"| 점수: {pre_target['buy_score']}")

            # 경험 기록용 컨텍스트
            _ta_scores = {
                "rsi": ta.rsi_score, "macd": ta.macd_score,
                "bb": ta.bb_score, "stoch": ta.stoch_score,
                "adx": ta.adx_score, "ma": ta.ma_score,
                "total": ta.total,
            }

            breakout_passed = signal.type.value == "BUY"

            if not breakout_passed:
                # 돌파 미통과: TA 임계값 완화 (was 20 → 5)
                # 거의 모든 종목이 융합 평가로 진입 → 실제 차단은 fusion에서
                if ta.total < 5:
                    log_decision(symbol, name, "skip", f"신호 없음: {signal.reason}",
                                 cur_price, strategy="etf", ta_scores=_ta_scores)
                    continue
                print(f"    돌파 미통과, TA={ta.total:+.0f} — 융합 평가 진행")

            # LGBM 예측 (차단하지 않고 확률만 수집, history 재사용)
            lgbm_filter = get_prediction_filter(client, symbol, history=history)
            lgbm_prob = lgbm_filter.get("up_prob", 0.5)
            if lgbm_prob != 0.5:
                print(f"    LGBM: 상승 {lgbm_prob:.0%} — {lgbm_filter['reason']}")

            # 오버나이트 갭 + 레짐 정보 수집
            gap_info = cfg.get("overnight_signal", {})
            regime_info = cfg.get("market_regime", {})
            regime_state = regime_info.get("hmm_state", "unknown")
            regime_conf = regime_info.get("hmm_confidence", 0.5)
            market_conf = cfg.get("market_confidence", 0.5)

            overnight_gap = None
            if gap_info:
                overnight_gap = {
                    "direction": gap_info.get("direction", "neutral"),
                    "strength": gap_info.get("strength", 0),
                }

            # 수급 신호 (pykrx 캐시)
            flow_val = 0.0
            try:
                from src.strategies.flow_signal import load_flow_cache
                flow_cache = load_flow_cache()
                if symbol in flow_cache:
                    flow_val = flow_cache[symbol].get("signal", 0.0)
                    if flow_val != 0:
                        print(f"    수급: {flow_cache[symbol].get('detail', '')}")
            except Exception:
                pass

            # 신호 확률적 융합: 모든 신호를 가중 결합
            fusion = fuse_signals(
                ta_score=ta.total,
                lgbm_prob=lgbm_prob,
                breakout_signal=breakout_passed,
                overnight_gap=overnight_gap,
                regime=regime_state,
                regime_confidence=regime_conf,
                market_confidence=market_conf,
                flow_signal=flow_val,
            )
            print(f"    융합: {fusion.detail}")

            if fusion.signal == "SKIP":
                # 평균회귀 fallback — 횡보장(sideways HMM) 전용
                mr_buy = False
                try:
                    hmm_state = cfg.get("market_regime", {}).get("hmm_state", "unknown")
                    if hmm_state in ("sideways", "low_vol"):
                        from src.strategies.mean_reversion import compute_mean_reversion_signal
                        mr_sig = compute_mean_reversion_signal(history)
                        if mr_sig.is_buy and mr_sig.score >= 50:  # was 70 — active mode
                            mr_buy = True
                            print(f"    [평균회귀] {mr_sig.reason} | score={mr_sig.score} "
                                  f"| RSI={mr_sig.rsi} BB={mr_sig.bb_position_pct}% "
                                  f"VWAP={mr_sig.vwap_deviation_pct:+.1f}%"
                                  f" (HMM={hmm_state})")
                            sizing_ratio = 0.3
                    elif hmm_state not in ("unknown",):
                        pass  # 추세장에서는 평균회귀 진입 차단
                except Exception as _mr_e:
                    pass

                if not mr_buy:
                    print(f"    융합 판단: SKIP (확률 {fusion.final_prob:.0%} < 55%)")
                    log_decision(symbol, name, "skip",
                                 f"융합 SKIP ({fusion.final_prob:.0%})",
                                 cur_price, strategy="etf", ta_scores=_ta_scores,
                                 lgbm_prob=lgbm_prob)
                    continue

            # 돌파 미통과 시 STRONG_BUY만 허용 (약한 신호로 진입 방지)
            if not breakout_passed and fusion.signal != "STRONG_BUY":
                print(f"    돌파 미통과 + BUY급 → SKIP (STRONG_BUY 필요)")
                log_decision(symbol, name, "skip",
                             f"돌파 미통과, 융합 BUY {fusion.final_prob:.0%} (STRONG 필요)",
                             cur_price, strategy="etf", ta_scores=_ta_scores,
                             lgbm_prob=lgbm_prob)
                continue

            # 확신도 연동 포지션 사이징: 융합 확률에 비례하여 투입
            # 55%→30%, 70%→65%, 85%+→100%
            fp = fusion.final_prob
            if fp >= STRONG_BUY_THRESHOLD:
                sizing_ratio = 1.0
            else:
                sizing_ratio = 0.3 + (fp - BUY_THRESHOLD) / (STRONG_BUY_THRESHOLD - BUY_THRESHOLD) * 0.7
            sizing_ratio = max(0.3, min(1.0, sizing_ratio))

            # 섹터 집중도 확인
            try:
                from src.strategies.sector_rotation import check_sector_concentration
                conc_ok, conc_reason = check_sector_concentration(holdings, universe, symbol)
                if not conc_ok:
                    print(f"    [집중도] {conc_reason}")
                    log_decision(symbol, name, "skip", conc_reason,
                                 cur_price, strategy="etf", ta_scores=_ta_scores)
                    continue
            except Exception:
                pass

            # Drawdown 스케일링: 연속 손실/수익에 따라 조정
            dd_scale, dd_reason = get_drawdown_scale()
            sizing_ratio *= dd_scale
            sizing_ratio = max(0.2, min(1.2, sizing_ratio))
            if dd_scale != 1.0:
                print(f"    Drawdown: {dd_reason} (최종 투입={sizing_ratio:.0%})")

            qty = int(budget * 0.999 * sizing_ratio // cur_price)
            if qty <= 0:
                # 소액 자본 최소 1주 보장 (active mode 확장):
                # - 돌파+융합 50%+ 또는 융합 55%+ 또는 TA 25+ → 1주 매수 허용
                # - 자본 분산: 단일 종목 max 35% of cash
                medium_signal = (
                    (breakout_passed and fusion.final_prob >= 0.50)
                    or fusion.final_prob >= 0.55
                    or ta.total >= 25
                )
                single_share_cost = int(cur_price * 1.001)
                avail_cash = get_available_cash(client)
                max_single_position = int(avail_cash * 0.35)
                if (medium_signal and single_share_cost <= max_single_position
                        and single_share_cost <= avail_cash):
                    qty = 1
                    reason_label = (
                        "강신호 돌파+융합" if (breakout_passed and fusion.final_prob >= 0.65)
                        else "중간 돌파+융합" if (breakout_passed and fusion.final_prob >= 0.50)
                        else f"융합 {fusion.final_prob:.0%}" if fusion.final_prob >= 0.55
                        else f"TA={ta.total:+.0f} 강세"
                    )
                    print(f"    [최소1주] {reason_label} (융합 {fusion.final_prob:.0%}, "
                          f"TA {ta.total:+.0f}, 돌파 {'O' if breakout_passed else 'X'}) — 1주 매수")
                    print(f"    예산: {single_share_cost:,}원 / 가용: {avail_cash:,}원")
                else:
                    print(f"    매수 불가 (예산 {budget:,}원×{sizing_ratio:.0%}, 주가 {cur_price:,}원, "
                          f"신호 약함: 융합 {fusion.final_prob:.0%}, TA {ta.total:+.0f})")
                    continue

            # 호가 임밸런스 timing filter — 매수 직전 호가 약세면 SKIP
            try:
                from src.strategies.orderbook_imbalance import (
                    get_imbalance, should_skip_buy, is_strong_buy,
                )
                imb = get_imbalance(client, symbol)
                if imb.ok:
                    print(f"    [호가] {imb.reason}")
                    if should_skip_buy(imb):
                        print(f"    [호가 임밸런스] 약세 {imb.weighted:+.2f} → 매수 SKIP")
                        log_decision(symbol, name, "skip",
                                     f"호가 약세 {imb.weighted:+.2f}",
                                     cur_price, strategy="etf", ta_scores=_ta_scores,
                                     lgbm_prob=lgbm_prob)
                        continue
                    if is_strong_buy(imb):
                        # 호가 강세 시 사이즈 소폭 상향 (최대 +20%)
                        sizing_ratio = min(1.2, sizing_ratio * 1.1)
                        qty = int(budget * 0.999 * sizing_ratio // cur_price)
                        print(f"    [호가 강세] 사이즈 ↑ ({sizing_ratio:.0%})")
            except Exception as _imb_e:
                pass  # imbalance 실패는 무시 (기본 동작 유지)

            total = qty * cur_price
            buy_label = "STRONG_BUY" if fusion.signal == "STRONG_BUY" else "BUY"
            print(f"    [{buy_label}] {name} {qty}주 @ {cur_price:,}원 = {total:,}원 "
                  f"(융합={fusion.final_prob:.0%}, 투입={sizing_ratio:.0%}, TA={ta.total:+.0f})")

            # ATR 계산 (동적 손절용)
            atr_value = compute_atr_for_position(history)

            _extra = {"strong_sector": is_strong, "fusion_prob": fusion.final_prob,
                       "fusion_signal": fusion.signal, "breakout_signal": breakout_passed,
                       "atr_at_buy": round(atr_value, 2),
                       "sizing_ratio": round(sizing_ratio, 2)}

            _record_fn = record_pyramid if pyramid_mode else record_buy

            if twap_engine:
                twap_engine.submit(symbol, qty, "buy", name, cur_price)
                if pyramid_mode:
                    _record_fn(symbol, cur_price, qty, atr=atr_value)
                else:
                    _record_fn(symbol, cur_price, qty, atr=atr_value)
                log_decision(symbol, name, "buy",
                             f"융합 {buy_label} ({fusion.final_prob:.0%}, TA={ta.total:+.0f})"
                             + (" [피라미딩]" if pyramid_mode else ""),
                             cur_price, qty=qty, strategy="etf", ta_scores=_ta_scores,
                             lgbm_prob=lgbm_prob, extra=_extra)
                return total

            if not dry_run:
                resp = _safe_order_cash(client, symbol, qty, cur_price, "buy")
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, name, "buy", qty, cur_price)
                    if pyramid_mode:
                        _record_fn(symbol, cur_price, qty, atr=atr_value)
                    else:
                        _record_fn(symbol, cur_price, qty, atr=atr_value)
                    log_decision(symbol, name, "buy",
                                 f"융합 {buy_label} ({fusion.final_prob:.0%}, TA={ta.total:+.0f})"
                                 + (" [피라미딩]" if pyramid_mode else ""),
                                 cur_price, qty=qty, strategy="etf",
                                 ta_scores=_ta_scores,
                                 lgbm_prob=lgbm_prob,
                                 extra=_extra)
                    return total
                elif rt == "E":
                    log.warning("etf_buy_error", symbol=symbol, msg=resp.get("msg1", ""))
            else:
                print("    (dry-run)")
                return total

        except Exception as e:
            print(f"    ERROR: {e}")

    print("  [ETF] 돌파 종목 없음 (또는 TA 거부).")
    return 0


# ──────────────────────────────────────────────────────────
# 하락장 전략 (인버스 + 방어자산)
# ──────────────────────────────────────────────────────────

def load_bear_config() -> dict:
    """bear_strategy 설정 로드."""
    cfg = load_config()
    return cfg.get("bear_strategy", {})


def _is_leveraged_type(asset_type: str | None) -> bool:
    """레버리지 ETP 여부(CLAUDE.md #6 금지 대상).

    inverse_2x / leverage_2x / 3x / 곱버스 등을 모두 레버리지로 판정.
    inverse_1x · inverse · defensive 등 1배수는 False.
    """
    if not asset_type:
        return False
    t = str(asset_type).lower()
    return any(tag in t for tag in ("2x", "3x", "4x", "lev", "곱버스"))


def load_inverse_universe() -> list[dict]:
    cfg = load_config()
    # 레버리지 인버스는 설정에 남아 있어도 로드 단계에서 걸러낸다(CLAUDE.md #6 이중 방어).
    inv = cfg.get("universe", {}).get("inverse", [])
    return [s for s in inv if not _is_leveraged_type(s.get("type"))]


def load_defensive_universe() -> list[dict]:
    cfg = load_config()
    return cfg.get("universe", {}).get("defensive", [])


def load_income_universe() -> list[dict]:
    cfg = load_config()
    return cfg.get("universe", {}).get("income", [])


def load_canary_universe() -> list[dict]:
    cfg = load_config()
    return cfg.get("universe", {}).get("canary", [])


def load_leveraged_config() -> dict:
    return load_config().get("leveraged", {}) or {}


def run_leveraged_strategy(client: KISClient, budget: int, holdings: dict,
                           regime_result, rapid_level: str, hmm_state: str,
                           hmm_conf: float, dry_run: bool) -> int:
    """레버리지 ETF 추세추종 진입 (CLAUDE.md #6 가드, 기본 OFF).

    강한 상승추세 게이트(leveraged_entry_allowed) 통과 시에만, 하드손절·비중캡 적용.
    leveraged.enabled=false면 즉시 종료. dry_run/leveraged.dry_run이면 주문 미전송(모의).
    """
    lc = load_leveraged_config()
    if not lc.get("enabled", False):
        return 0
    allowed, reason = leveraged_entry_allowed(
        regime_result.regime if regime_result else "BULL",
        rapid_level, hmm_state, hmm_conf, {"leveraged": lc})
    if not allowed:
        print(f"  [레버리지] 진입 차단 — {reason}")
        return 0
    lev_dry = dry_run or lc.get("dry_run", True)
    uni = [u for u in (lc.get("universe") or []) if u.get("market", "KR") == "KR"]
    if not uni:
        return 0
    if any(u["symbol"] in holdings for u in uni):
        print("  [레버리지] 이미 보유 중. 스킵.")
        return 0
    params = load_strategy_params()
    k = params.get("k", 0.5)
    ma = params.get("trend_ma", 20)
    hard_stop = float(lc.get("hard_stop_pct", 0.04))
    print(f"  [레버리지] 게이트 통과 — {reason} "
          f"{'(dry-run 모의)' if lev_dry else '(LIVE 실거래)'}, 배정 {budget:,}원")
    for stock in uni:
        symbol = stock["symbol"]
        name = stock["name"]
        try:
            history = fetch_recent_history(client, symbol, days=70)
            # 상향 변동성 돌파 판정 (방향무관 — 레버리지 롱도 동일 돌파 수식)
            sig = inverse_breakout_signal(history, k=k, trend_ma=ma)
            print(f"    {name}: {sig['reason']}")
            if not sig["breakout"]:
                continue
            ta = compute_ta_score(history)
            if ta.total < 15:
                print(f"    TA={ta.total:+.0f} 부족, 스킵")
                continue
            cur_price = int(sig["price"])
            qty = int(budget * 0.999 // cur_price)
            if qty <= 0:
                continue
            total = qty * cur_price
            print(f"    [레버리지 BUY] {name} {qty}주 @ {cur_price:,}원 = {total:,}원 "
                  f"(TA={ta.total:+.0f}, 하드손절 -{hard_stop:.0%})")
            if lev_dry:
                print("      (dry-run — 주문 미전송)")
                return total
            resp = _safe_order_cash(client, symbol, qty, cur_price, "buy")
            print(f"      응답: rt_cd={resp.get('rt_cd')}, msg={resp.get('msg1', '')}")
            if resp.get("rt_cd") == "0":
                record_buy(symbol, cur_price, qty,
                           asset_type=stock.get("type", "leverage_2x"))
                from src.risk_manager import save_positions
                pos = load_positions()
                if symbol in pos:
                    pos[symbol]["initial_risk"] = round(cur_price * hard_stop, 2)
                    save_positions(pos)
                return total
        except Exception as e:
            log.warning("leveraged_buy_failed", symbol=symbol, error=str(e))
    return 0


def run_bear_strategy(client: KISClient, budget: int, holdings: dict,
                      regime_result, allocation, dry_run: bool,
                      twap_engine: TWAPEngine | None = None) -> int:
    """하락장 전략: 인버스 ETF 돌파 + 방어자산 매수.

    레짐과 배분 결과에 따라:
      - BEAR: 인버스 ETF 에 변동성 돌파 적용, 나머지 채권
      - CAUTION: 채권 위주, 롱 축소
      - CRISIS: 현금 + 단기채 (매수 없음)
    """
    r = regime_result.regime
    today_str = _now().strftime("%Y-%m-%d")

    print(f"  [하락장] 레짐: {r} ({regime_result.confidence:.0%}) | {regime_result.detail}")
    print(f"  [배분] {allocation.detail}")

    if r == "CRISIS":
        print("  [하락장] CRISIS — 모든 위험자산 회피. 현금 보유.")
        return 0

    used = 0
    params = load_strategy_params()
    k = params.get("k", 0.5)
    ma = params.get("trend_ma", 20)

    # 성과 학습 기반 파라미터 조정
    adaptive = get_adaptive_params(r)
    if adaptive["reason"] != "기본 파라미터":
        print(f"  [학습] {adaptive['reason']}")

    # ── 인버스 ETF 매수 (BEAR 모드) ──
    if r == "BEAR" and allocation.inverse_pct > 0:
        inv_budget = int(budget * allocation.inverse_pct * adaptive.get("inverse_scale", 1.0))
        inv_universe = load_inverse_universe()
        inv_syms = {s["symbol"] for s in inv_universe}

        # 이미 인버스 보유 중이면 스킵
        inv_held = any(s in inv_syms for s in holdings)
        if inv_held:
            print(f"  [인버스] 이미 보유 중. 추가 매수 스킵.")
        elif inv_budget >= 10000:
            print(f"  [인버스] 배정: {inv_budget:,}원 (K={k}, MA={ma})")

            for stock in inv_universe:
                symbol = stock["symbol"]
                name = stock["name"]
                asset_type = stock.get("type", "inverse_1x")
                # 레버리지 인버스 절대 금지(CLAUDE.md #6) — 로드 필터에 더한 최종 방어선.
                if _is_leveraged_type(asset_type):
                    print(f"    [인버스] {name} 레버리지({asset_type}) — 매수 금지(스킵)")
                    log_decision(symbol, name, "skip",
                                 f"레버리지 인버스 금지: {asset_type}",
                                 0.0, strategy="bear_inverse")
                    continue
                try:
                    history = fetch_recent_history(client, symbol, days=70)
                    sig = inverse_breakout_signal(history, k=k, trend_ma=ma)
                    print(f"    {name}: {sig['reason']}")

                    if not sig["breakout"]:
                        log_decision(symbol, name, "skip",
                                     f"인버스 미돌파: {sig['reason']}",
                                     sig["price"], strategy="bear_inverse")
                        continue

                    # TA 보조 확인 (인버스도 TA 적용)
                    ta = compute_ta_score(history)
                    if ta.total < 10:
                        print(f"    TA={ta.total:+.0f} 부족, 스킵")
                        log_decision(symbol, name, "skip",
                                     f"인버스 TA 부족 ({ta.total:+.0f})",
                                     sig["price"], strategy="bear_inverse")
                        continue

                    cur_price = int(sig["price"])
                    qty = int(inv_budget * 0.999 // cur_price)
                    if qty <= 0:
                        continue

                    total = qty * cur_price
                    atr_value = compute_atr_for_position(history)
                    print(f"    [인버스 BUY] {name} {qty}주 @ {cur_price:,}원 = {total:,}원 "
                          f"(TA={ta.total:+.0f})")

                    if twap_engine:
                        twap_engine.submit(symbol, qty, "buy", name, cur_price)
                        record_buy(symbol, cur_price, qty, atr=atr_value,
                                   asset_type=asset_type)
                    elif not dry_run:
                        resp = _safe_order_cash(client, symbol, qty, cur_price, "buy")
                        rt = resp.get("rt_cd")
                        print(f"      응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                        if rt == "0":
                            log_trade(symbol, name, "buy", qty, cur_price)
                            record_buy(symbol, cur_price, qty, atr=atr_value,
                                       asset_type=asset_type)
                            log_bear_trade(r, "inverse", symbol, cur_price, today_str)
                            used += total
                            break
                    else:
                        print("      (dry-run)")
                        used += total
                        break

                    log_decision(symbol, name, "buy",
                                 f"인버스 돌파 (TA={ta.total:+.0f})",
                                 cur_price, qty=qty, strategy="bear_inverse")
                    break

                except Exception as e:
                    print(f"    ERROR: {e}")

    # ── 방어자산 매수 (BEAR / CAUTION) ──
    if allocation.defensive_pct > 0:
        def_budget = int(budget * allocation.defensive_pct * adaptive.get("defensive_scale", 1.0))
        def_universe = load_defensive_universe()
        def_syms = {s["symbol"] for s in def_universe}

        def_held = any(s in def_syms for s in holdings)
        if def_held:
            print(f"  [방어] 이미 보유 중.")
        elif def_budget >= 10000:
            print(f"  [방어] 배정: {def_budget:,}원")

            # 방어자산 중 모멘텀이 가장 좋은 것 선택
            best_sym, best_name, best_score = None, None, -999
            for stock in def_universe:
                symbol = stock["symbol"]
                name = stock["name"]
                try:
                    history = fetch_recent_history(client, symbol, days=70)
                    if history is not None and len(history) >= 22:
                        from src.strategies.bear_strategy import weighted_momentum
                        score = weighted_momentum(history["close"])
                        if score > best_score:
                            best_sym, best_name, best_score = symbol, name, score
                except Exception:
                    pass

            if best_sym:
                cur_price = get_price(client, best_sym)
                if cur_price > 0:
                    qty = int(def_budget * 0.999 // cur_price)
                    if qty > 0:
                        total = qty * cur_price
                        print(f"    [방어 BUY] {best_name} {qty}주 @ {cur_price:,}원 "
                              f"(모멘텀={best_score:.4f})")

                        if twap_engine:
                            twap_engine.submit(best_sym, qty, "buy", best_name, cur_price)
                            record_buy(best_sym, cur_price, qty, asset_type="defensive")
                        elif not dry_run:
                            resp = _safe_order_cash(client, best_sym, qty, cur_price, "buy")
                            rt = resp.get("rt_cd")
                            if rt == "0":
                                log_trade(best_sym, best_name, "buy", qty, cur_price)
                                record_buy(best_sym, cur_price, qty, asset_type="defensive")
                                log_bear_trade(r, "defensive", best_sym, cur_price, today_str)
                                used += total
                        else:
                            print("      (dry-run)")
                            used += total

                        log_decision(best_sym, best_name, "buy",
                                     f"방어자산 (모멘텀={best_score:.4f})",
                                     cur_price, qty=qty, strategy="bear_defensive")

    if used == 0:
        print("  [하락장] 매수 조건 미충족. 현금 보유.")

    return used


# ──────────────────────────────────────────────────────────
# 횡보장 인컴 전략 (커버드콜 ETF)
# ──────────────────────────────────────────────────────────

def run_income_strategy(client: KISClient, budget: int, holdings: dict,
                        dry_run: bool,
                        twap_engine: "TWAPEngine | None" = None) -> int:
    """횡보장 인컴 전략: 커버드콜 ETF 모멘텀 기반 매수.

    HMM sideways/low_vol 상태에서 돌파 전략이 매수하지 못할 때,
    커버드콜 ETF로 옵션 프리미엄 수익을 추구.
    """
    income_universe = load_income_universe()
    if not income_universe:
        return 0

    income_syms = {s["symbol"] for s in income_universe}
    if any(s in income_syms for s in holdings):
        print("  [인컴] 커버드콜 ETF 이미 보유 중.")
        return 0

    if budget < 10000:
        return 0

    print(f"  [인컴] 횡보장 → 커버드콜 ETF 탐색 (배정: {budget:,}원)")

    best_sym, best_name, best_score = None, None, -999
    for stock in income_universe:
        symbol = stock["symbol"]
        name = stock["name"]
        try:
            history = fetch_recent_history(client, symbol, days=70)
            if history is not None and len(history) >= 22:
                from src.strategies.bear_strategy import weighted_momentum
                score = weighted_momentum(history["close"])
                print(f"    {name} ({symbol}): 모멘텀={score:.4f}")
                if score > best_score:
                    best_sym, best_name, best_score = symbol, name, score
        except Exception:
            pass

    if not best_sym or best_score < -0.1:
        print("  [인컴] 모멘텀 양호한 커버드콜 ETF 없음. 스킵.")
        return 0

    cur_price = get_price(client, best_sym)
    if cur_price <= 0:
        return 0

    qty = int(budget * 0.999 // cur_price)
    if qty <= 0:
        return 0

    total = qty * cur_price
    print(f"    [인컴 BUY] {best_name} {qty}주 @ {cur_price:,}원 (모멘텀={best_score:.4f})")

    if twap_engine:
        twap_engine.submit(best_sym, qty, "buy", best_name, cur_price)
        record_buy(best_sym, cur_price, qty, asset_type="income")
    elif not dry_run:
        resp = _safe_order_cash(client, best_sym, qty, cur_price, "buy")
        if resp.get("rt_cd") == "0":
            log_trade(best_sym, best_name, "buy", qty, cur_price)
            record_buy(best_sym, cur_price, qty, asset_type="income")
        else:
            print(f"    주문 실패: {resp.get('msg1', '')}")
            return 0
    else:
        print("      (dry-run)")

    log_decision(best_sym, best_name, "buy",
                 f"횡보장 인컴 (커버드콜, 모멘텀={best_score:.4f})",
                 cur_price, qty=qty, strategy="income")
    return total


# ──────────────────────────────────────────────────────────
# 레짐 판단 + 전략 분기
# ──────────────────────────────────────────────────────────

def evaluate_regime(client: KISClient) -> tuple:
    """현재 시장 레짐을 평가하고 배분 결정을 반환.

    Returns:
        (regime_result, allocation, bear_enabled)
    """
    bear_cfg = load_bear_config()
    if not bear_cfg.get("enabled", False):
        return None, None, False

    cfg = load_config()

    # KOSPI200 히스토리 (SMA200용)
    kospi_history = None
    try:
        kospi_history = fetch_recent_history(client, "069500", days=250)
    except Exception as e:
        log.warning("regime_kospi_fetch_failed", error=str(e))

    # 카나리아 유니버스 히스토리
    canary_universe = load_canary_universe()
    canary_histories = {}
    for c in canary_universe:
        try:
            hist = fetch_recent_history(client, c["symbol"], days=270)
            canary_histories[c["symbol"]] = hist
        except Exception:
            canary_histories[c["symbol"]] = None

    # HMM 상태 (이미 계산된 것 활용)
    regime_info = cfg.get("market_regime", {})
    hmm_state = regime_info.get("hmm_state", "unknown")
    hmm_conf = regime_info.get("hmm_confidence", 0.5)

    # 레짐 판단
    regime_result = detect_market_regime(
        kospi_history, canary_histories,
        hmm_state=hmm_state, hmm_confidence=hmm_conf,
        cfg=bear_cfg,
    )

    # 변동성 계산
    current_vol = compute_annualized_vol(kospi_history) if kospi_history is not None else 0.20

    # 배분 결정
    allocation = compute_bear_allocation(regime_result, current_vol, bear_cfg)

    return regime_result, allocation, True


# ──────────────────────────────────────────────────────────
# 1회 실행 (기존 5분 cron 호환)
# ──────────────────────────────────────────────────────────

def run_once(dry_run: bool) -> None:
    """1회 체크: 리스크 관리 + 전략 실행."""
    now = _now()
    t = now.time()

    summary = get_summary()
    print(f"[{now:%Y-%m-%d %H:%M:%S}] mode={settings.mode.value} | "
          f"거래: {summary['total_trades']}건, PnL: {summary['pnl']:+,}원 ({summary['pnl_pct']:+.1f}%)")

    if t < MARKET_OPEN or t > MARKET_END:
        print(f"  장외 시간 ({t:%H:%M}). 스킵.")
        return

    universe = load_universe()
    universe_syms = {s["symbol"] for s in universe}
    client = KISClient()
    holdings = get_all_holdings(client)
    cash = get_available_cash(client)

    print(f"  예수금: {cash:,}원 | 보유: {holdings if holdings else '없음'}")

    # ── 09:00~09:10 전일 보유분 평가 → 조건부 매도/보유 ──
    if MARKET_OPEN <= t <= SELL_WINDOW_END and holdings:
        bot_positions = load_positions()
        to_sell = {}
        for symbol, qty in holdings.items():
            # 봇이 매수하지 않은 수동 보유분은 시가 매도 대상에서 제외
            if symbol not in bot_positions:
                print(f"  [수동 보유] {symbol} {qty}주 — 사용자 보유분, 봇 매도 대상 아님")
                continue
            price = get_price(client, symbol)
            if price <= 0:
                # 가격 조회 실패 시 자동 매도하지 않음 (price=0 시장가 주문 방지)
                print(f"  [가격 조회 실패] {symbol} {qty}주 — 매도 보류")
                continue
            hold, reason = should_hold_overnight(symbol, price)
            if hold:
                print(f"  [보유 유지] {symbol} {qty}주 — {reason}")
            else:
                print(f"  [시가 매도] {symbol} {qty}주 — {reason}")
                to_sell[symbol] = qty
        if to_sell:
            sell_holdings(client, to_sell, universe_syms, "시가매도", dry_run)
        return

    # ── 장중 리스크 체크: 손절/추적손절/ROI ──
    if holdings:
        print("  [리스크 체크]")
        holdings = check_risk_and_sell(client, holdings, universe_syms, dry_run)

    # ── 15:20 이후 미매도 청산 (봇 포지션만, 수동 보유분은 유지) ──
    if t > MARKET_CLOSE and holdings:
        bot_positions = load_positions()
        bot_holdings = {s: q for s, q in holdings.items() if s in bot_positions}
        if bot_holdings:
            sell_holdings(client, bot_holdings, universe_syms, "장마감청산", dry_run)
        return

    # ── 장중: 두 전략 실행 ──
    if t > MARKET_CLOSE:
        print(f"  매수 시간 지남 ({t:%H:%M}). 스킵.")
        return

    # ── 터뷸런스 필터: 변동성 급등 시 매수 차단 ──
    is_turbulent, turb_reason = check_turbulence(client)
    print(f"  [터뷸런스] {turb_reason}")
    if is_turbulent:
        print("  시장 변동성 급등. 신규 매수 차단. 현금 보유.")
        return

    # ── 일일 손실 한도 체크 ──
    loss_exceeded, loss_reason = check_daily_loss_limit(client)
    print(f"  [일일손실] {loss_reason}")
    if loss_exceeded:
        print("  일일 손실 한도 초과. 신규 매수 차단.")
        return

    # ── 최대 동시 포지션 체크 (시즌 조정 포함) ──
    risk_cfg = load_risk_params()
    max_pos = risk_cfg.get("max_concurrent_positions", 5)
    try:
        from src.strategies.seasonal import get_seasonal_adjustment
        _season = get_seasonal_adjustment()
        max_pos = max(1, max_pos + _season.get("max_positions_adj", 0))
    except Exception:
        pass
    can_buy, pos_reason = check_max_positions(max_pos)
    if not can_buy:
        print(f"  [포지션] {pos_reason}. 신규 매수 차단.")
        return

    # ── 시장 신뢰도 + 장중 적응 ──
    confidence = get_market_confidence()
    intraday = get_intraday_regime_adjustment(client)
    print(f"  [시장 신뢰도] {confidence:.0%} | {intraday['reason']}")

    # ── 시즌 필터 (할로윈 전략) ──
    try:
        from src.strategies.seasonal import get_seasonal_adjustment
        seasonal = get_seasonal_adjustment()
        confidence *= seasonal["confidence_mult"]
        print(f"  [시즌] {seasonal['reason']} (신뢰도 x{seasonal['confidence_mult']})")
    except Exception:
        seasonal = None

    # ── 오버나이트 갭 신호 반영 ──
    cfg = load_config()
    gap = cfg.get("overnight_signal", {})
    gap_action = gap.get("recommended_action", "normal")
    if gap_action == "skip":
        print(f"  [오버나이트] 미국 급락 → 매수 스킵 (NASDAQ {gap.get('nasdaq_change', 0):+.1f}%)")
        return

    # 신뢰도가 낮으면 투자 비율 축소
    size_factor = max(0.3, confidence)  # 최소 30%, 최대 100%
    if intraday.get("reduce_size"):
        size_factor *= 0.7  # 장중 급변 시 추가 30% 축소

    # 오버나이트 갭 신호로 사이즈 조정
    if gap_action == "aggressive_buy":
        size_factor = min(1.0, size_factor * 1.2)
        print(f"  [오버나이트] 미국 강세 → 적극 매수 (x1.2)")
    elif gap_action == "reduce_size":
        size_factor *= 0.7
        print(f"  [오버나이트] 미국 약세 → 규모 축소 (x0.7)")

    # ── VAA 월간 시그널 반영 ──
    try:
        from src.strategies.vaa_rebalance import load_vaa_state
        vaa = load_vaa_state().get("current", {})
        if vaa.get("mode") == "defensive":
            size_factor *= 0.5
            print(f"  [VAA] 방어 모드 → {vaa.get('target_name', '?')} 선호, 공격 자산 축소 (x0.5)")
        elif vaa.get("mode") == "offensive":
            print(f"  [VAA] 공격 모드 → {vaa.get('target_name', '?')} 최우선")
    except Exception:
        pass

    # ── 레짐 판단 + 전략 분기 ──
    regime_result, allocation, bear_enabled = evaluate_regime(client)

    if bear_enabled and regime_result and regime_result.regime in ("BEAR", "CRISIS", "CAUTION"):
        print(f"  [레짐] {regime_result.regime} — 하락장 전략 진입")
        total_budget = int(cash * size_factor)

        if regime_result.regime == "CAUTION" and allocation.long_pct > 0:
            # CAUTION: 롱 비중만큼 기존 전략, 나머지 방어
            long_budget = int(total_budget * allocation.long_pct)
            etf_held = any(s in universe_syms for s in holdings)
            if not etf_held and long_budget >= 10000:
                print(f"  [CAUTION] 롱 배정: {long_budget:,}원 ({allocation.long_pct:.0%})")
                run_etf_strategy(client, long_budget, holdings, universe, dry_run)
            bear_budget = total_budget - long_budget
            if bear_budget >= 10000:
                run_bear_strategy(client, bear_budget, holdings, regime_result,
                                  allocation, dry_run)
        else:
            # BEAR / CRISIS: 전체 하락장 전략
            run_bear_strategy(client, total_budget, holdings, regime_result,
                              allocation, dry_run)
    else:
        # BULL 또는 하락장 전략 비활성: 기존 ETF 전략
        etf_held = any(s in universe_syms for s in holdings)
        etf_budget = int(cash * size_factor) if not etf_held else 0

        if size_factor < 1.0:
            print(f"  [배분 조정] 신뢰도 반영: ETF {etf_budget:,}원 (x{size_factor:.0%})")

        etf_used = run_etf_strategy(client, etf_budget, holdings, universe, dry_run)

        # ── 레버리지 ETF (BULL 강추세 게이트, CLAUDE.md #6 가드, 기본 OFF) ──
        try:
            lev_cfg = load_leveraged_config()
            if lev_cfg.get("enabled", False):
                hmm_s = cfg.get("market_regime", {}).get("hmm_state", "unknown")
                hmm_c = float(cfg.get("market_regime", {}).get("hmm_confidence", 0) or 0)
                lev_budget = int(cash * size_factor * float(lev_cfg.get("max_weight", 0.15)))
                if lev_budget >= 10000:
                    # BULL 분기라 급락 트리거는 NONE(있었으면 레짐이 격상돼 여기 안 옴)
                    run_leveraged_strategy(client, lev_budget, holdings, regime_result,
                                           "NONE", hmm_s, hmm_c, dry_run)
        except Exception as e:
            log.warning("leveraged_strategy_skipped", error=str(e))

        if etf_used == 0 and not etf_held:
            hmm = cfg.get("market_regime", {}).get("hmm_state", "unknown")
            if hmm in ("sideways", "low_vol"):
                income_budget = int(cash * size_factor * 0.5)
                income_used = run_income_strategy(
                    client, income_budget, holdings, dry_run)
                if income_used == 0:
                    print("  돌파·인컴 모두 미충족. 현금 보유.")
            else:
                print("  돌파 없음. 현금 보유.")


# ──────────────────────────────────────────────────────────
# 연속 감시 루프 (1분 간격)
# ──────────────────────────────────────────────────────────

def run_loop(dry_run: bool) -> None:
    """1분 간격 연속 감시. 장 시작~마감까지 실행.

    매 1분: 보유 종목 리스크 체크 (손절/추적손절/ROI)
    매 5분: ETF 변동성 돌파 전략 실행
    매 3분: 터뷸런스 필터 갱신
    """
    summary = get_summary()
    print(f"{'=' * 60}")
    print(f"[루프 모드] 1분 간격 연속 감시 시작")
    print(f"  mode={settings.mode.value} | dry_run={dry_run}")
    print(f"  거래: {summary['total_trades']}건, PnL: {summary['pnl']:+,}원")
    print(f"  리스크 체크: {RISK_CHECK_INTERVAL}초 | 전략 체크: {STRATEGY_CHECK_INTERVAL}초")
    print(f"{'=' * 60}")

    client = KISClient()
    universe = load_universe()
    universe_syms = {s["symbol"] for s in universe}
    twap = TWAPEngine()

    loop_start_epoch = time_mod.time()  # 하드 타임아웃 전 자체 종료 기준
    last_strategy_check = 0.0     # epoch. 전략 체크 마지막 시각
    last_turbulence_check = 0.0   # epoch. 터뷸런스 체크 마지막 시각
    sold_at_open = False          # 시가 매도 완료 여부
    is_turbulent = False          # 현재 터뷸런스 상태
    bought_today = False          # 오늘 매수 완료 여부

    # ── Killswitch 초기 체크 (루프 진입 전) ──
    ks_status = killswitch.get_status()
    if ks_status["active"]:
        print(f"\n⚠️  [Killswitch] mode={ks_status['mode']} | reason={ks_status['reason']} "
              f"| set_by={ks_status['set_by']}")
        if ks_status["mode"] == "full_stop":
            print("  full_stop 모드 → 루프 진입 안 함. 종료.")
            return

    # ── 포지션 동기화: KIS 잔고 → internal positions.json ──
    # 액면분할·배당락·유상증자 시 KIS가 보낸 수량·평단가로 자동 보정
    try:
        from src.safety.position_sync import sync_from_broker
        changes = sync_from_broker(client, market="KR")
        important = changes["qty_changed"] + changes["price_changed"]
        if important:
            print(f"[포지션 동기화] 액분·배당 조정 감지:")
            for sym, old, new in changes["qty_changed"]:
                print(f"  {sym}: 수량 {old} → {new}")
            for sym, old, new in changes["price_changed"]:
                print(f"  {sym}: 평단 {old:,.0f} → {new:,.0f}")
    except Exception as e:
        log.warning("position_sync_skipped", error=str(e))

    # ── 캐리 포지션 흡수: 이전 run에서 산 보유분을 손절 관리 대상으로 ──
    # positions.json이 매 run 리셋되어 봇이 자기 보유분을 잊고 손절을 안 거는 문제 방지.
    # (봇 유니버스 ∩ 봇 거래이력)인 broker 보유분만 흡수, 진짜 수동분은 보호.
    try:
        from src.risk_manager import adopt_carried_positions
        from src.merge_trades import traded_symbols
        bal = client.get_balance()
        broker_holdings = {}
        if bal.get("rt_cd") == "0":
            for it in bal.get("output1", []):
                sym = it.get("pdno", "")
                q = int(float(it.get("hldg_qty", 0) or 0))
                if sym and q > 0:
                    broker_holdings[sym] = {
                        "qty": q,
                        "buy_price": float(it.get("pchs_avg_pric", 0) or 0),
                        "current_price": float(it.get("prpr", 0) or 0),
                    }
        uni_syms = {s["symbol"] for s in universe}
        uni_syms |= {s["symbol"] for s in load_inverse_universe()}
        traded = traded_symbols("logs/trades.csv")
        n = adopt_carried_positions(broker_holdings, uni_syms, traded)
        if n:
            print(f"[캐리 흡수] 이전 매수분 {n}개를 손절 관리 대상으로 흡수 "
                  f"(positions 미복원 보완)")
    except Exception as e:
        log.warning("adopt_carried_skipped", error=str(e))

    while True:
        now = _now()
        t = now.time()
        epoch_now = time_mod.time()

        # ── Killswitch 매 루프 체크 ──
        if killswitch.is_full_stop():
            print(f"\n⚠️  [{now:%H:%M:%S}] Killswitch full_stop 감지. 루프 종료.")
            break

        # ── 자체 최대 실행시간 → 정상 종료(핸드오프) ──
        # GitHub 하드 타임아웃(360분)에 강제 종료되면 정리 스텝이 스킵돼
        # 거래기록이 유실되므로, 그 전에 스스로 break해 정리 스텝을 보장한다.
        # 마감 전이면 다음 스케줄 run이 세션을 이어받는다(concurrency 큐).
        if _runtime_exceeded(loop_start_epoch, epoch_now):
            print(f"\n[{now:%H:%M:%S}] 최대 실행시간({MAX_LOOP_RUNTIME_SEC // 60}분) 도달 "
                  f"— 정상 종료(핸드오프). 정리 스텝 실행 후 다음 run이 이어받음.")
            break

        # ── 장 마감 → 종료 ──
        if t > MARKET_END:
            print(f"\n[{now:%H:%M:%S}] 장 마감. 루프 종료.")
            break

        # ── 장 시작 전 → 대기 ──
        if t < MARKET_OPEN:
            # KST tzinfo 부착해서 aware-aware 뺄셈 (now는 datetime.now(KST))
            open_dt = datetime.combine(now.date(), MARKET_OPEN, tzinfo=KST)
            wait = (open_dt - now).total_seconds()
            wait = min(wait, 60)  # 최대 60초씩 대기 (중간에 체크)
            if wait > 10:
                print(f"[{now:%H:%M:%S}] 장 시작 대기 ({wait:.0f}초)")
            time_mod.sleep(max(1, wait))
            continue

        # ── 보유 현황 조회 ──
        holdings = get_all_holdings(client)

        # ── 09:00~09:10 시가 평가 → 조건부 매도/보유 ──
        if MARKET_OPEN <= t <= SELL_WINDOW_END and not sold_at_open:
            if holdings:
                print(f"\n[{now:%H:%M:%S}] === 시가 평가 ===")
                bot_positions = load_positions()
                to_sell = {}
                for symbol, qty in holdings.items():
                    # 봇이 매수하지 않은 수동 보유분은 시가 매도 대상에서 제외
                    if symbol not in bot_positions:
                        print(f"  [수동 보유] {symbol} {qty}주 — 사용자 보유분, 봇 매도 대상 아님")
                        continue
                    price = get_price(client, symbol)
                    if price <= 0:
                        # 가격 조회 실패 시 자동 매도하지 않음 (price=0 시장가 주문 방지)
                        print(f"  [가격 조회 실패] {symbol} {qty}주 — 매도 보류")
                        continue
                    hold, reason = should_hold_overnight(symbol, price)
                    if hold:
                        print(f"  [보유 유지] {symbol} {qty}주 — {reason}")
                    else:
                        print(f"  [시가 매도] {symbol} {qty}주 — {reason}")
                        to_sell[symbol] = qty
                if to_sell:
                    sell_holdings(client, to_sell, universe_syms, "시가매도", dry_run)
                holdings = get_all_holdings(client)
            sold_at_open = True
            time_mod.sleep(RISK_CHECK_INTERVAL)
            continue

        # ── TWAP 트랜치 실행 (매 1분) ──
        if twap.has_pending():
            twap_results = twap.tick(client, dry_run)
            if twap_results:
                for tr in twap_results:
                    if tr["side"] == "buy":
                        bought_today = True
                holdings = get_all_holdings(client)

        # ── 리스크 체크 (매 1분) — 보유 종목 가격 확인 ──
        if holdings:
            risk_sold = False
            for symbol, qty in list(holdings.items()):
                price = get_price(client, symbol)
                if price <= 0:
                    continue

                should_sell, reason = check_stop_loss(symbol, price)
                if should_sell:
                    is_partial = reason.startswith("[분할]")
                    sell_qty = max(1, int(qty * PARTIAL_SELL_RATIO)) if is_partial else qty
                    tag = "ETF" if symbol in universe_syms else "급등주"
                    label = "분할매도" if is_partial else "리스크"
                    print(f"\n[{now:%H:%M:%S}] [{label}] {tag} {symbol} "
                          f"{sell_qty}/{qty}주 @ {price:,}원 — {reason}")
                    if not dry_run:
                        resp = _safe_order_cash(client, symbol, sell_qty, price, "sell")
                        rt = resp.get("rt_cd")
                        print(f"  응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                        if rt == "0":
                            log_trade(symbol, tag, "sell", sell_qty, price)
                            if is_partial:
                                holdings[symbol] = qty - sell_qty
                                positions = load_positions()
                                if symbol in positions:
                                    positions[symbol]["qty"] = qty - sell_qty
                                    save_positions(positions)
                            else:
                                r_val = log_r_multiple(symbol, price)
                                if r_val is not None:
                                    print(f"  R배수: {r_val:+.2f}R")
                                remove_position(symbol)
                            risk_sold = True
                        elif rt == "E":
                            log.warning("loop_risk_sell_error", symbol=symbol,
                                        msg=resp.get("msg1", ""))
                    else:
                        print("  (dry-run)")
                        risk_sold = True

            if risk_sold:
                holdings = get_all_holdings(client)

        # ── 15:20 이후 미매도 청산 (봇 포지션만, 수동 보유분은 유지) ──
        if t > MARKET_CLOSE and holdings:
            bot_positions = load_positions()
            bot_holdings = {s: q for s, q in holdings.items() if s in bot_positions}
            if bot_holdings:
                print(f"\n[{now:%H:%M:%S}] === 장마감 청산 ===")
                sell_holdings(client, bot_holdings, universe_syms, "장마감청산", dry_run)
            time_mod.sleep(RISK_CHECK_INTERVAL)
            continue

        # ── 매수 시간 지남 ──
        if t > MARKET_CLOSE:
            time_mod.sleep(RISK_CHECK_INTERVAL)
            continue

        # ── 터뷸런스 체크 (매 3분) ──
        if epoch_now - last_turbulence_check >= TURBULENCE_CHECK_INTERVAL:
            is_turbulent, turb_reason = check_turbulence(client)
            last_turbulence_check = epoch_now
            if is_turbulent:
                print(f"[{now:%H:%M:%S}] [터뷸런스] {turb_reason}")

        # ── 전략 체크 — 신규 매수 탐색 ──
        # 장 초반(~10:00)에는 2분 간격, 이후 5분 간격
        # Killswitch block_buy_only 모드면 전략 체크 자체를 스킵 (리스크 체크는 계속)
        strategy_interval = STRATEGY_CHECK_EARLY if t < EARLY_SESSION_END else STRATEGY_CHECK_INTERVAL
        if killswitch.is_buy_blocked():
            time_mod.sleep(RISK_CHECK_INTERVAL)
            continue
        if (epoch_now - last_strategy_check >= strategy_interval
                and not bought_today
                and not is_turbulent
                and t > SELL_WINDOW_END):

            last_strategy_check = epoch_now

            # 일일 손실 한도 체크
            loss_exceeded, loss_reason = check_daily_loss_limit(client)
            if loss_exceeded:
                print(f"[{now:%H:%M:%S}] [일일손실] {loss_reason}")
                time_mod.sleep(RISK_CHECK_INTERVAL)
                continue

            # 시즌 필터 로드
            try:
                from src.strategies.seasonal import get_seasonal_adjustment
                seasonal = get_seasonal_adjustment()
            except Exception:
                seasonal = {"confidence_mult": 1.0, "max_positions_adj": 0}

            # 최대 동시 포지션 체크 (시즌 조정 포함)
            risk_cfg = load_risk_params()
            max_pos = risk_cfg.get("max_concurrent_positions", 5)
            max_pos = max(1, max_pos + seasonal.get("max_positions_adj", 0))
            can_buy, pos_reason = check_max_positions(max_pos)
            if not can_buy:
                print(f"[{now:%H:%M:%S}] [포지션] {pos_reason}")
                time_mod.sleep(RISK_CHECK_INTERVAL)
                continue

            cash = get_available_cash(client)
            if cash < 10000:
                time_mod.sleep(RISK_CHECK_INTERVAL)
                continue

            # Kelly Criterion: 전체 자본 대비 최적 투입 비율
            kelly_f = get_kelly_position_size("combined")
            # 최소 Kelly 20% 보장 (소액 자본에서 1주도 못 사는 문제 회피)
            # 강한 신호 시 사이즈 추가 확장은 run_etf_strategy에서 1주 보장 로직으로 처리
            kelly_cap = max(int(cash * kelly_f), int(cash * 0.20))
            etf_budget_cap = min(cash, kelly_cap)

            # 시장 신뢰도 반영 (시즌 필터 적용)
            confidence = get_market_confidence()
            confidence *= seasonal["confidence_mult"]
            intraday = get_intraday_regime_adjustment(client)
            size_factor = max(0.3, confidence)
            if intraday.get("reduce_size"):
                size_factor *= 0.7

            # C4: VIX 필터 반영 — 시카고 변동성지수 기반 시장 환경
            try:
                from src.strategies.vix_filter import get_vix_filter
                vix = get_vix_filter()
                if vix:
                    print(f"[{now:%H:%M:%S}] [VIX] {vix.detail}")
                    if vix.skip_buy:
                        print(f"  VIX panic → 신규 매수 차단")
                        time_mod.sleep(RISK_CHECK_INTERVAL)
                        continue
                    size_factor *= vix.size_multiplier
                    confidence *= vix.confidence_multiplier
            except Exception as _vix_e:
                pass

            # 오버나이트 갭 신호 반영
            try:
                cfg = load_config()
                gap = cfg.get("overnight_signal", {})
                gap_action = gap.get("recommended_action", "normal")
                if gap_action == "skip":
                    print(f"\n[{now:%H:%M:%S}] [오버나이트] 미국 급락 → 매수 스킵")
                    time_mod.sleep(RISK_CHECK_INTERVAL)
                    continue
                elif gap_action == "aggressive_buy":
                    size_factor = min(1.0, size_factor * 1.2)
                elif gap_action == "reduce_size":
                    size_factor *= 0.7
            except Exception:
                cfg = load_config()
                gap = {}

            # 경험 기반 레짐 추천 반영
            try:
                cur_regime = cfg.get("market_regime", {}).get("trend", "unknown")
                cur_hmm = cfg.get("market_regime", {}).get("hmm_state", "unknown")
                regime_rec = get_regime_recommendation(cur_regime, cur_hmm)
                if regime_rec.get("data_points", 0) >= 5:
                    size_factor *= regime_rec["confidence_adj"]
                    print(f"  [경험] {regime_rec['reason']}")
            except Exception:
                regime_rec = {}

            # 테일 리스크 체크 (VaR/ES 기반)
            try:
                from src.strategies.tail_risk import get_tail_risk_adjustment
                tr_mult, tr_reason = get_tail_risk_adjustment()
                if tr_mult < 1.0:
                    size_factor *= tr_mult
                    print(f"  [테일리스크] {tr_reason}")
            except Exception:
                pass

            # 장중 모멘텀 스캐너 (급등 감지)
            try:
                from src.strategies.intraday_scanner import scan_intraday_momentum
                surges = scan_intraday_momentum(client, universe)
                for s in surges[:3]:
                    print(f"  [스캐너] {s.signal}: {s.detail}")
            except Exception:
                pass

            print(f"\n[{now:%H:%M:%S}] === 전략 체크 ===")
            print(f"  예수금: {cash:,}원 | Kelly={kelly_f:.0%} "
                  f"| 신뢰도: {confidence:.0%} | {intraday['reason']}")

            # ── 레짐 판단 + 전략 분기 ──
            regime_result, allocation, bear_enabled = evaluate_regime(client)
            # 모든 분기에서 참조되므로 분기 전에 한 번만 계산 (BEAR/CRISIS 분기에서 미정의되는 버그 방지)
            etf_held = any(s in universe_syms for s in holdings)

            if bear_enabled and regime_result and regime_result.regime in ("BEAR", "CRISIS", "CAUTION"):
                print(f"  [레짐] {regime_result.regime} — 하락장 전략")
                total_budget = int(etf_budget_cap * size_factor)

                if regime_result.regime == "CAUTION" and allocation.long_pct > 0:
                    long_budget = int(total_budget * allocation.long_pct)
                    if not etf_held and long_budget >= 10000:
                        etf_used = run_etf_strategy(client, long_budget, holdings,
                                                     universe, dry_run, twap_engine=twap)
                        if etf_used > 0:
                            bought_today = True
                    bear_budget = total_budget - long_budget
                    if bear_budget >= 10000:
                        bear_used = run_bear_strategy(client, bear_budget, holdings,
                                                       regime_result, allocation,
                                                       dry_run, twap_engine=twap)
                        if bear_used > 0:
                            bought_today = True
                else:
                    bear_used = run_bear_strategy(client, total_budget, holdings,
                                                   regime_result, allocation,
                                                   dry_run, twap_engine=twap)
                    if bear_used > 0:
                        bought_today = True
            else:
                etf_budget = int(etf_budget_cap * size_factor) if not etf_held else 0

                # ETF 변동성 돌파 (TWAP 분할 매수)
                etf_used = run_etf_strategy(client, etf_budget, holdings, universe,
                                            dry_run, twap_engine=twap)
                if etf_used > 0:
                    bought_today = True

            if not bought_today and not etf_held:
                loop_hmm = cfg.get("market_regime", {}).get("hmm_state", "unknown")
                if loop_hmm in ("sideways", "low_vol"):
                    income_budget = int(etf_budget_cap * size_factor * 0.5)
                    income_used = run_income_strategy(
                        client, income_budget, holdings, dry_run, twap_engine=twap)
                    if income_used > 0:
                        bought_today = True
                    else:
                        print("  돌파·인컴 모두 미충족. 현금 보유.")
                else:
                    print("  돌파 없음. 현금 보유.")

        # ── 1분 대기 ──
        elapsed = time_mod.time() - epoch_now
        sleep_time = max(1, RISK_CHECK_INTERVAL - elapsed)
        time_mod.sleep(sleep_time)


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 자동매매 봇")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--loop", action="store_true",
                        help="1분 간격 연속 감시 (장 시작~마감)")
    args = parser.parse_args()

    if args.loop:
        run_loop(args.dry_run)
    else:
        run_once(args.dry_run)


if __name__ == "__main__":
    main()
