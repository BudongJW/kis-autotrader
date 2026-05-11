"""단일 실행 봇 — GitHub Actions용 (이중 전략).

전략 A: ETF 변동성 돌파 (예수금의 60%)
  - strategy.yaml의 universe ETF 대상
  - 목표가 돌파 시 매수, 익일 시가 매도

전략 B: 급등주 단타 (예수금의 40%)
  - KIS API 등락률 순위로 급등 종목 탐지
  - 상위 1종목 매수, 익일 시가 매도

자본 배분:
  - 예수금 기준 비율 배분 (수익 재투자 자동 반영)
  - ETF에 먼저 배정, 나머지를 급등주에 사용
  - 각 전략은 독립적으로 포지션 관리
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time as dtime
from pathlib import Path

import yaml

from src.config import settings
from src.kis_client import KISClient
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.strategies.surge_scanner import scan_surge_candidates
from src.strategies.ta_composite import compute_ta_score, TAScore
from src.bot.runner import fetch_recent_history, get_holding_qty
from src.tracker import log_trade, get_summary
from src.utils.logger import log

MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 20)
MARKET_END = dtime(15, 30)
SELL_WINDOW_END = dtime(9, 10)

CONFIG_PATH = Path("configs/strategy.yaml")

# 자본 배분 비율 (예수금 대비)
ETF_RATIO = 0.60   # 60% → ETF 변동성 돌파
SURGE_RATIO = 0.40  # 40% → 급등주 단타


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_universe() -> list[dict]:
    return load_config().get("universe", {}).get("default", [])


def load_strategy_params() -> dict:
    return load_config().get("strategies", {}).get("volatility_breakout", {})


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
                  label: str, dry_run: bool) -> None:
    """보유 종목 매도."""
    for symbol, qty in holdings.items():
        price = get_price(client, symbol)
        tag = "ETF" if symbol in universe_syms else "급등주"
        print(f"  [{label}] {tag} {symbol} {qty}주 @ ~{price:,}원")
        if not dry_run:
            resp = client.order_cash(symbol, qty=qty, side="sell")
            rt = resp.get("rt_cd")
            print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
            if rt == "0":
                log_trade(symbol, tag, "sell", qty, price)
                log.info(f"{label}_sell", symbol=symbol, qty=qty, price=price)
        else:
            print("    (dry-run)")


def run_etf_strategy(client: KISClient, budget: int, holdings: dict,
                     universe: list[dict], dry_run: bool) -> int:
    """ETF 변동성 돌파 전략. 사용한 금액을 반환."""
    universe_syms = {s["symbol"] for s in universe}

    # 이미 ETF 보유 중이면 스킵
    etf_held = {s: q for s, q in holdings.items() if s in universe_syms}
    if etf_held:
        syms = ", ".join(f"{s}({q}주)" for s, q in etf_held.items())
        print(f"  [ETF] 보유 중: {syms}. 익일 매도 대기.")
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

            # TA 복합 점수 계산
            ta = compute_ta_score(history)
            print(f"  [ETF] {name} {signal.type.value} @ {cur_price:,}원 — {signal.reason}")
            print(f"    TA분석: {ta.detail}")

            if signal.type.value != "BUY":
                continue

            # 변동성 돌파 + TA 확인: TA 점수가 -20 이하면 매수 거부
            if ta.total <= -20:
                print(f"    TA 거부 (점수 {ta.total:+.0f} ≤ -20). 매수 스킵.")
                continue

            qty = int(budget * 0.999 // cur_price)
            if qty <= 0:
                print(f"    매수 불가 (예산 {budget:,}원, 주가 {cur_price:,}원)")
                continue

            total = qty * cur_price
            print(f"    [매수] {name} {qty}주 @ {cur_price:,}원 = {total:,}원 (TA={ta.total:+.0f})")
            if not dry_run:
                resp = client.order_cash(symbol, qty=qty, side="buy")
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, name, "buy", qty, cur_price)
                    return total
            else:
                print("    (dry-run)")
                return total

        except Exception as e:
            print(f"    ERROR: {e}")

    print("  [ETF] 돌파 종목 없음 (또는 TA 거부).")
    return 0


def run_surge_strategy(client: KISClient, budget: int, holdings: dict,
                       universe_syms: set, dry_run: bool) -> int:
    """급등주 단타 전략. 사용한 금액을 반환."""
    # 이미 급등주 보유 중이면 스킵
    surge_held = {s: q for s, q in holdings.items() if s not in universe_syms}
    if surge_held:
        syms = ", ".join(f"{s}({q}주)" for s, q in surge_held.items())
        print(f"  [급등주] 보유 중: {syms}. 익일 매도 대기.")
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

    # 상위 5개 표시
    print(f"  [급등주] 후보 {len(candidates)}개 발견. 상위 5:")
    for c in candidates[:5]:
        print(f"    {c.name:<12} {c.price:>7,}원 +{c.change_pct:.1f}% "
              f"거래량:{c.volume:,} 점수={c.score:.1f}")

    # 상위 후보에 대해 TA 분석 후 매수 결정
    for c in candidates[:10]:  # 상위 10개만 분석 (API 호출 절약)
        qty = int(budget * 0.999 // c.price)
        if qty <= 0:
            continue

        # TA 복합 분석으로 거짓 급등 필터링
        try:
            hist = fetch_recent_history(client, c.symbol, days=70)
            ta = compute_ta_score(hist)
            print(f"    {c.name} TA분석: {ta.detail}")
            if ta.total < 0:
                print(f"    TA 거부 ({ta.total:+.0f} < 0). 스킵.")
                continue
        except Exception as e:
            print(f"    {c.name} TA 조회 실패: {e}. 기본 점수로 진행.")

        total = qty * c.price
        print(f"    [매수] {c.name} {qty}주 @ {c.price:,}원 = {total:,}원 (TA={ta.total:+.0f})")
        if not dry_run:
            # 실시간 가격 재확인
            live_price = get_price(client, c.symbol)
            if live_price <= 0:
                print(f"    실시간 가격 조회 실패. 스킵.")
                continue
            qty = int(budget * 0.999 // live_price)
            if qty <= 0:
                continue
            total = qty * live_price

            resp = client.order_cash(c.symbol, qty=qty, side="buy")
            rt = resp.get("rt_cd")
            print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
            if rt == "0":
                log_trade(c.symbol, c.name, "buy", qty, live_price)
                return total
        else:
            print("    (dry-run)")
            return total

    print("  [급등주] 예산 내 매수 가능 종목 없음 (또는 TA 거부).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 이중 전략 봇")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

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

    # 예수금 기반 동적 자본 배분
    etf_budget_cap = int(cash * ETF_RATIO)
    surge_budget_cap = int(cash * SURGE_RATIO)

    print(f"  예수금: {cash:,}원 | 보유: {holdings if holdings else '없음'}")
    print(f"  배분: ETF {etf_budget_cap:,}원({ETF_RATIO:.0%}) / "
          f"급등주 {surge_budget_cap:,}원({SURGE_RATIO:.0%})")

    # ── 09:00~09:10 전일 보유분 전량 매도 ──
    if MARKET_OPEN <= t <= SELL_WINDOW_END and holdings:
        sell_holdings(client, holdings, universe_syms, "시가매도", args.dry_run)
        return

    # ── 15:20 이후 미매도 청산 ──
    if t > MARKET_CLOSE and holdings:
        sell_holdings(client, holdings, universe_syms, "장마감청산", args.dry_run)
        return

    # ── 장중: 두 전략 실행 ──
    if t > MARKET_CLOSE:
        print(f"  매수 시간 지남 ({t:%H:%M}). 스킵.")
        return

    # ETF 보유 여부에 따라 예산 동적 배분
    etf_held = any(s in universe_syms for s in holdings)
    surge_held = any(s not in universe_syms for s in holdings)

    etf_budget = etf_budget_cap if not etf_held else 0
    remaining = cash - etf_budget if not etf_held else cash
    surge_budget = min(surge_budget_cap, remaining) if not surge_held else 0

    # 전략 A: ETF
    etf_used = run_etf_strategy(client, etf_budget, holdings, universe, args.dry_run)

    # 전략 B: 급등주 (ETF 매수 후 남은 예산으로)
    if surge_budget > 0:
        actual_surge_budget = surge_budget if etf_used == 0 else max(0, cash - etf_used)
        if actual_surge_budget >= 10000:  # 최소 1만원
            run_surge_strategy(client, actual_surge_budget, holdings,
                             universe_syms, args.dry_run)
    elif surge_held:
        print("  [급등주] 이미 보유 중.")

    if etf_used == 0 and not etf_held:
        print("  돌파/급등 없음. 현금 보유.")


if __name__ == "__main__":
    main()
