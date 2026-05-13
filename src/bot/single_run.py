"""자동매매 봇 — 1분 간격 연속 감시 + 단일 실행 겸용.

실행 모드:
  --loop     : 1분 간격 연속 감시 (장 시작~마감). GitHub Actions 장시간 실행용.
  (기본)     : 1회 체크 후 종료. 5분 cron 백업용.

전략 A: ETF 변동성 돌파 (기대값 기반 동적 배분)
전략 B: 급등주 단타 (기대값 기반 동적 배분)

리스크 관리 (1분마다 체크):
  - 장중 손절매: -3% 도달 시 즉시 매도
  - 추적 손절: +1.5% 도달 후 고점 대비 -1% 시 매도
  - 동적 ROI: 보유 시간별 최소 수익률 도달 시 청산
  - 터뷸런스 필터: KOSPI200 변동성 급등 시 신규 매수 차단
  - 기대값 기반 배분: 전략별 성과에 따라 자본 비율 자동 조정
"""

from __future__ import annotations

import argparse
import sys
import time as time_mod
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import yaml

from src.config import settings
from src.kis_client import KISClient
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.strategies.surge_scanner import scan_surge_candidates
from src.strategies.ta_composite import compute_ta_score, TAScore
from src.bot.runner import fetch_recent_history, get_holding_qty
from src.tracker import log_trade, get_summary
from src.risk_manager import (
    check_stop_loss, check_turbulence, record_buy, remove_position,
    load_positions, get_strategy_expectancy, get_kelly_position_size,
)
from src.market_learner import get_market_confidence, get_intraday_regime_adjustment
from src.execution.twap import TWAPEngine
from src.strategies.lgbm_predictor import get_prediction_filter
from src.utils.logger import log

MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 20)
MARKET_END = dtime(15, 30)
SELL_WINDOW_END = dtime(9, 10)

CONFIG_PATH = Path("configs/strategy.yaml")

# 기본 자본 배분 비율 (기대값 데이터 없을 때)
DEFAULT_ETF_RATIO = 0.60
DEFAULT_SURGE_RATIO = 0.40

# 루프 간격 (초)
RISK_CHECK_INTERVAL = 60       # 리스크 체크: 1분
STRATEGY_CHECK_INTERVAL = 300  # 전략 체크: 5분
TURBULENCE_CHECK_INTERVAL = 180  # 터뷸런스 체크: 3분


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_universe() -> list[dict]:
    return load_config().get("universe", {}).get("default", [])


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
            resp = client.order_cash(symbol, qty=qty, price=price, side="sell")
            rt = resp.get("rt_cd")
            print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
            if rt == "0":
                log_trade(symbol, tag, "sell", qty, price)
                remove_position(symbol)
                log.info(f"{label}_sell", symbol=symbol, qty=qty, price=price)
            elif rt == "E":
                log.warning(f"{label}_sell_error", symbol=symbol, msg=resp.get("msg1", ""))
        else:
            print("    (dry-run)")


def check_risk_and_sell(client: KISClient, holdings: dict[str, int],
                        universe_syms: set, dry_run: bool) -> dict[str, int]:
    """보유 종목에 대해 리스크 체크 (손절/추적손절/ROI). 매도 후 남은 보유분 반환."""
    remaining = dict(holdings)

    for symbol, qty in holdings.items():
        price = get_price(client, symbol)
        if price <= 0:
            continue

        should_sell, reason = check_stop_loss(symbol, price)
        if should_sell:
            tag = "ETF" if symbol in universe_syms else "급등주"
            print(f"  [리스크] {tag} {symbol} {qty}주 @ {price:,}원 — {reason}")
            if not dry_run:
                resp = client.order_cash(symbol, qty=qty, price=price, side="sell")
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, tag, "sell", qty, price)
                    remove_position(symbol)
                    remaining.pop(symbol, None)
                elif rt == "E":
                    log.warning("risk_sell_error", symbol=symbol, msg=resp.get("msg1", ""))
            else:
                print("    (dry-run)")
                remaining.pop(symbol, None)

    return remaining


def run_etf_strategy(client: KISClient, budget: int, holdings: dict,
                     universe: list[dict], dry_run: bool,
                     twap_engine: TWAPEngine | None = None) -> int:
    """ETF 변동성 돌파 전략. 사용한 금액을 반환."""
    universe_syms = {s["symbol"] for s in universe}

    etf_held = {s: q for s, q in holdings.items() if s in universe_syms}
    if etf_held:
        syms = ", ".join(f"{s}({q}주)" for s, q in etf_held.items())
        print(f"  [ETF] 보유 중: {syms}. 리스크 관리 대기.")
        return 0

    params = load_strategy_params()
    k = params.get("k", 0.5)
    ma = params.get("trend_ma", 20)
    strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)
    print(f"  [ETF] K={k}, MA={ma} | 배정: {budget:,}원")

    for stock in universe:
        symbol = stock["symbol"]
        name = stock["name"]
        try:
            history = fetch_recent_history(client, symbol, days=70)
            signal = strategy.generate_signal(symbol, history)
            cur_price = int(signal.price)

            ta_weights = load_ta_weights()
            ta = compute_ta_score(history, weights=ta_weights)
            print(f"  [ETF] {name} {signal.type.value} @ {cur_price:,}원 — {signal.reason}")
            print(f"    TA분석: {ta.detail}")

            if signal.type.value != "BUY":
                continue

            if ta.total <= -20:
                print(f"    TA 거부 (점수 {ta.total:+.0f} ≤ -20). 매수 스킵.")
                continue

            # LGBM 예측 필터
            lgbm_filter = get_prediction_filter(client, symbol)
            if not lgbm_filter["allow"]:
                print(f"    LGBM 차단: {lgbm_filter['reason']}")
                continue
            if lgbm_filter["up_prob"] != 0.5:  # 모델이 있을 때만 출력
                print(f"    LGBM: 상승 {lgbm_filter['up_prob']:.0%} — {lgbm_filter['reason']}")

            qty = int(budget * 0.999 // cur_price)
            if qty <= 0:
                print(f"    매수 불가 (예산 {budget:,}원, 주가 {cur_price:,}원)")
                continue

            total = qty * cur_price
            print(f"    [매수] {name} {qty}주 @ {cur_price:,}원 = {total:,}원 (TA={ta.total:+.0f})")

            if twap_engine:
                twap_engine.submit(symbol, qty, "buy", name, cur_price)
                return total

            if not dry_run:
                resp = client.order_cash(symbol, qty=qty, price=cur_price, side="buy")
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, name, "buy", qty, cur_price)
                    record_buy(symbol, cur_price, qty)
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


def run_surge_strategy(client: KISClient, budget: int, holdings: dict,
                       universe_syms: set, dry_run: bool,
                       twap_engine: TWAPEngine | None = None) -> int:
    """급등주 단타 전략. 사용한 금액을 반환."""
    surge_held = {s: q for s, q in holdings.items() if s not in universe_syms}
    if surge_held:
        syms = ", ".join(f"{s}({q}주)" for s, q in surge_held.items())
        print(f"  [급등주] 보유 중: {syms}. 리스크 관리 대기.")
        return 0

    print(f"  [급등주] 배정: {budget:,}원 | 스캐닝 중...")

    try:
        candidates = scan_surge_candidates(client)
    except Exception as e:
        print(f"  [급등주] 스캔 실패: {e}")
        return 0

    if not candidates:
        print("  [급등주] 조건 충족 종목 없음.")
        return 0

    print(f"  [급등주] 후보 {len(candidates)}개 발견. 상위 5:")
    for c in candidates[:5]:
        print(f"    {c.name:<12} {c.price:>7,}원 +{c.change_pct:.1f}% "
              f"거래량:{c.volume:,} 점수={c.score:.1f}")

    for c in candidates[:10]:
        qty = int(budget * 0.999 // c.price)
        if qty <= 0:
            continue

        try:
            hist = fetch_recent_history(client, c.symbol, days=70)
            ta_weights = load_ta_weights()
            ta = compute_ta_score(hist, weights=ta_weights)
            print(f"    {c.name} TA분석: {ta.detail}")
            if ta.total < 0:
                print(f"    TA 거부 ({ta.total:+.0f} < 0). 스킵.")
                continue
        except Exception as e:
            print(f"    {c.name} TA 조회 실패: {e}. 스킵.")
            continue

        live_price = get_price(client, c.symbol) if not dry_run else c.price
        if live_price <= 0:
            print(f"    실시간 가격 조회 실패. 스킵.")
            continue
        qty = int(budget * 0.999 // live_price)
        if qty <= 0:
            continue
        total = qty * live_price

        print(f"    [매수] {c.name} {qty}주 @ {live_price:,}원 = {total:,}원 (TA={ta.total:+.0f})")

        if twap_engine:
            twap_engine.submit(c.symbol, qty, "buy", c.name, live_price)
            return total

        if not dry_run:
            resp = client.order_cash(c.symbol, qty=qty, price=live_price, side="buy")
            rt = resp.get("rt_cd")
            print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
            if rt == "0":
                log_trade(c.symbol, c.name, "buy", qty, live_price)
                record_buy(c.symbol, live_price, qty)
                return total
            elif rt == "E":
                log.warning("surge_buy_error", symbol=c.symbol, msg=resp.get("msg1", ""))
        else:
            print("    (dry-run)")
            return total

    print("  [급등주] 예산 내 매수 가능 종목 없음 (또는 TA 거부).")
    return 0


# ──────────────────────────────────────────────────────────
# 1회 실행 (기존 5분 cron 호환)
# ──────────────────────────────────────────────────────────

def run_once(dry_run: bool) -> None:
    """1회 체크: 리스크 관리 + 전략 실행."""
    now = datetime.now()
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

    # 기대값 기반 동적 자본 배분
    expectancy = get_strategy_expectancy()
    etf_ratio = expectancy.get("etf", DEFAULT_ETF_RATIO)
    surge_ratio = expectancy.get("surge", DEFAULT_SURGE_RATIO)
    etf_budget_cap = int(cash * etf_ratio)
    surge_budget_cap = int(cash * surge_ratio)

    print(f"  예수금: {cash:,}원 | 보유: {holdings if holdings else '없음'}")
    print(f"  배분: ETF {etf_budget_cap:,}원({etf_ratio:.0%}) / "
          f"급등주 {surge_budget_cap:,}원({surge_ratio:.0%})")

    # ── 09:00~09:10 전일 보유분 전량 매도 ──
    if MARKET_OPEN <= t <= SELL_WINDOW_END and holdings:
        sell_holdings(client, holdings, universe_syms, "시가매도", dry_run)
        return

    # ── 장중 리스크 체크: 손절/추적손절/ROI ──
    if holdings:
        print("  [리스크 체크]")
        holdings = check_risk_and_sell(client, holdings, universe_syms, dry_run)
        if not holdings:
            cash = get_available_cash(client)
            etf_budget_cap = int(cash * etf_ratio)
            surge_budget_cap = int(cash * surge_ratio)

    # ── 15:20 이후 미매도 청산 ──
    if t > MARKET_CLOSE and holdings:
        sell_holdings(client, holdings, universe_syms, "장마감청산", dry_run)
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

    # ── 시장 신뢰도 + 장중 적응 ──
    confidence = get_market_confidence()
    intraday = get_intraday_regime_adjustment(client)
    print(f"  [시장 신뢰도] {confidence:.0%} | {intraday['reason']}")

    # 신뢰도가 낮으면 투자 비율 축소
    size_factor = max(0.3, confidence)  # 최소 30%, 최대 100%
    if intraday.get("reduce_size"):
        size_factor *= 0.7  # 장중 급변 시 추가 30% 축소

    # ETF 보유 여부에 따라 예산 동적 배분
    etf_held = any(s in universe_syms for s in holdings)
    surge_held = any(s not in universe_syms for s in holdings)

    etf_budget = int(etf_budget_cap * size_factor) if not etf_held else 0
    remaining = cash - etf_budget if not etf_held else cash
    surge_budget = int(min(surge_budget_cap, remaining) * size_factor) if not surge_held else 0

    if size_factor < 1.0:
        print(f"  [배분 조정] 신뢰도 반영: ETF {etf_budget:,}원, 급등주 {surge_budget:,}원 "
              f"(x{size_factor:.0%})")

    # 전략 A: ETF
    etf_used = run_etf_strategy(client, etf_budget, holdings, universe, dry_run)

    # 전략 B: 급등주
    if surge_budget > 0:
        actual_surge_budget = surge_budget if etf_used == 0 else max(0, cash - etf_used)
        if actual_surge_budget >= 10000:
            run_surge_strategy(client, actual_surge_budget, holdings,
                             universe_syms, dry_run)
    elif surge_held:
        print("  [급등주] 이미 보유 중.")

    if etf_used == 0 and not etf_held:
        print("  돌파/급등 없음. 현금 보유.")


# ──────────────────────────────────────────────────────────
# 연속 감시 루프 (1분 간격)
# ──────────────────────────────────────────────────────────

def run_loop(dry_run: bool) -> None:
    """1분 간격 연속 감시. 장 시작~마감까지 실행.

    매 1분: 보유 종목 리스크 체크 (손절/추적손절/ROI)
    매 5분: 전략 실행 (변동성 돌파 + 급등주 스캐닝)
    매 3분: 터뷸런스 필터 갱신

    리스크 체크는 가격만 조회하므로 API 부하 최소화.
    전략 실행은 일봉 + TA 계산이 포함되어 5분 간격으로 실행.
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

    last_strategy_check = 0.0     # epoch. 전략 체크 마지막 시각
    last_turbulence_check = 0.0   # epoch. 터뷸런스 체크 마지막 시각
    sold_at_open = False          # 시가 매도 완료 여부
    is_turbulent = False          # 현재 터뷸런스 상태
    bought_today = False          # 오늘 매수 완료 여부

    while True:
        now = datetime.now()
        t = now.time()
        epoch_now = time_mod.time()

        # ── 장 마감 → 종료 ──
        if t > MARKET_END:
            print(f"\n[{now:%H:%M:%S}] 장 마감. 루프 종료.")
            break

        # ── 장 시작 전 → 대기 ──
        if t < MARKET_OPEN:
            wait = (datetime.combine(now.date(), MARKET_OPEN) - now).total_seconds()
            wait = min(wait, 60)  # 최대 60초씩 대기 (중간에 체크)
            if wait > 10:
                print(f"[{now:%H:%M:%S}] 장 시작 대기 ({wait:.0f}초)")
            time_mod.sleep(max(1, wait))
            continue

        # ── 보유 현황 조회 ──
        holdings = get_all_holdings(client)

        # ── 09:00~09:10 시가 매도 ──
        if MARKET_OPEN <= t <= SELL_WINDOW_END and not sold_at_open:
            if holdings:
                print(f"\n[{now:%H:%M:%S}] === 시가 매도 ===")
                sell_holdings(client, holdings, universe_syms, "시가매도", dry_run)
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
                    tag = "ETF" if symbol in universe_syms else "급등주"
                    print(f"\n[{now:%H:%M:%S}] [리스크] {tag} {symbol} {qty}주 "
                          f"@ {price:,}원 — {reason}")
                    if not dry_run:
                        resp = client.order_cash(symbol, qty=qty, price=price, side="sell")
                        rt = resp.get("rt_cd")
                        print(f"  응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                        if rt == "0":
                            log_trade(symbol, tag, "sell", qty, price)
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

        # ── 15:20 이후 미매도 청산 ──
        if t > MARKET_CLOSE and holdings:
            print(f"\n[{now:%H:%M:%S}] === 장마감 청산 ===")
            sell_holdings(client, holdings, universe_syms, "장마감청산", dry_run)
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

        # ── 전략 체크 (매 5분) — 신규 매수 탐색 ──
        if (epoch_now - last_strategy_check >= STRATEGY_CHECK_INTERVAL
                and not bought_today
                and not is_turbulent
                and t > SELL_WINDOW_END):

            last_strategy_check = epoch_now
            cash = get_available_cash(client)
            if cash < 10000:
                time_mod.sleep(RISK_CHECK_INTERVAL)
                continue

            # Kelly + 기대값 기반 동적 배분
            try:
                expectancy = get_strategy_expectancy()
            except Exception:
                expectancy = {"etf": DEFAULT_ETF_RATIO, "surge": DEFAULT_SURGE_RATIO}
            etf_ratio = expectancy.get("etf", DEFAULT_ETF_RATIO)
            surge_ratio = expectancy.get("surge", DEFAULT_SURGE_RATIO)

            # Kelly Criterion: 전체 자본 대비 최적 투입 비율
            kelly_f = get_kelly_position_size("combined")
            kelly_cap = max(int(cash * kelly_f), int(cash * 0.10))  # 최소 10%

            etf_budget_cap = min(int(cash * etf_ratio), kelly_cap)
            surge_budget_cap = min(int(cash * surge_ratio), kelly_cap)

            # 시장 신뢰도 반영
            confidence = get_market_confidence()
            intraday = get_intraday_regime_adjustment(client)
            size_factor = max(0.3, confidence)
            if intraday.get("reduce_size"):
                size_factor *= 0.7

            print(f"\n[{now:%H:%M:%S}] === 전략 체크 ===")
            print(f"  예수금: {cash:,}원 | Kelly={kelly_f:.0%} "
                  f"| 신뢰도: {confidence:.0%} | {intraday['reason']}")

            etf_held = any(s in universe_syms for s in holdings)
            surge_held = any(s not in universe_syms for s in holdings)

            etf_budget = int(etf_budget_cap * size_factor) if not etf_held else 0
            remaining_cash = cash - etf_budget if not etf_held else cash
            surge_budget = int(min(surge_budget_cap, remaining_cash) * size_factor) if not surge_held else 0

            # 전략 A: ETF (TWAP 분할 매수)
            etf_used = run_etf_strategy(client, etf_budget, holdings, universe,
                                        dry_run, twap_engine=twap)
            if etf_used > 0:
                bought_today = True

            # 전략 B: 급등주 (TWAP 분할 매수)
            if surge_budget > 0 and not bought_today:
                actual_surge_budget = surge_budget if etf_used == 0 else max(0, cash - etf_used)
                if actual_surge_budget >= 10000:
                    surge_used = run_surge_strategy(client, actual_surge_budget,
                                                   holdings, universe_syms, dry_run,
                                                   twap_engine=twap)
                    if surge_used > 0:
                        bought_today = True

            if not bought_today and not etf_held:
                print("  돌파/급등 없음. 현금 보유.")

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
