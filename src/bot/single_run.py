"""단일 실행 봇 — GitHub Actions용.

매 실행마다 시세 확인 → 판단 → 주문 후 종료.
수익금은 자동으로 다음 매수에 재투자 (복리).

전략: 변동성 돌파 + 추세 필터 (ETF)
  - 목표가 돌파 시 예수금 전액으로 매수
  - 익일 시가에 전량 매도
  - 매도 대금이 다음날 매수 자금이 됨 (복리)
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
from src.bot.runner import fetch_recent_history, calc_buy_qty, get_holding_qty
from src.tracker import log_trade, get_summary
from src.utils.logger import log

MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 20)
MARKET_END = dtime(15, 30)
SELL_WINDOW_END = dtime(9, 10)

CONFIG_PATH = Path("configs/strategy.yaml")


def load_universe() -> list[dict]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("universe", {}).get("default", [])


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


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 단일 실행 봇 (GitHub Actions용)")
    parser.add_argument("--dry-run", action="store_true", help="주문 안 보냄")
    args = parser.parse_args()

    now = datetime.now()
    t = now.time()

    # 성과 요약
    summary = get_summary()
    print(f"[{now:%Y-%m-%d %H:%M:%S}] mode={settings.mode.value} | "
          f"누적 거래: {summary['total_trades']}건, PnL: {summary['pnl']:+,}원 ({summary['pnl_pct']:+.1f}%)")

    if t < MARKET_OPEN or t > MARKET_END:
        print(f"  장외 시간 ({t:%H:%M}). 스킵.")
        return

    universe = load_universe()
    if not universe:
        print("  universe 비어있음.")
        sys.exit(1)

    client = KISClient()
    strategy = VolatilityBreakoutStrategy()

    # universe에 있는 종목만 필터링
    universe_symbols = {s["symbol"] for s in universe}
    all_holdings = get_all_holdings(client)
    holdings = {s: q for s, q in all_holdings.items() if s in universe_symbols}
    cash = get_available_cash(client)

    print(f"  예수금: {cash:,}원 | 보유: {holdings if holdings else '없음'}")

    # ── 09:00~09:10 전일 보유분 전량 매도 (익일 시가 매도 = 복리의 핵심) ──
    if MARKET_OPEN <= t <= SELL_WINDOW_END and holdings:
        for symbol, qty in holdings.items():
            name = next((s["name"] for s in universe if s["symbol"] == symbol), symbol)
            price_resp = client.get_price(symbol)
            cur_price = int(price_resp["output"]["stck_prpr"]) if price_resp.get("rt_cd") == "0" else 0

            print(f"  [매도] {name} {qty}주 @ ~{cur_price:,}원 (예상 {qty * cur_price:,}원)")
            if not args.dry_run:
                resp = client.order_cash(symbol, qty=qty, side="sell")
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, name, "sell", qty, cur_price, cash + qty * cur_price)
                    log.info("morning_sell", symbol=symbol, qty=qty, price=cur_price)
            else:
                print("    (dry-run)")
        return

    # ── 15:20 이후 미매도 청산 ──
    if t > MARKET_CLOSE and holdings:
        for symbol, qty in holdings.items():
            name = next((s["name"] for s in universe if s["symbol"] == symbol), symbol)
            price_resp = client.get_price(symbol)
            cur_price = int(price_resp["output"]["stck_prpr"]) if price_resp.get("rt_cd") == "0" else 0

            print(f"  [청산] {name} {qty}주 @ ~{cur_price:,}원")
            if not args.dry_run:
                resp = client.order_cash(symbol, qty=qty, side="sell")
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, name, "sell", qty, cur_price, cash + qty * cur_price)
            else:
                print("    (dry-run)")
        return

    # ── 이미 보유 중이면 스킵 ──
    if holdings:
        syms = ", ".join(f"{s}({q}주)" for s, q in holdings.items())
        print(f"  보유 중: {syms}. 익일 시가 매도 대기.")
        return

    # ── 장중: 돌파 확인 → 매수 ──
    if t > MARKET_CLOSE:
        print(f"  매수 시간 지남 ({t:%H:%M}). 스킵.")
        return

    for stock in universe:
        symbol = stock["symbol"]
        name = stock["name"]
        try:
            history = fetch_recent_history(client, symbol)
            signal = strategy.generate_signal(symbol, history)
            cur_price = int(signal.price)

            print(f"  [{name}] {signal.type.value} @ {cur_price:,}원 — {signal.reason}")

            if signal.type.value != "BUY":
                continue

            # 복리: 예수금 전액으로 최대 수량 매수
            qty = int(cash * 0.999 // cur_price)
            if qty <= 0:
                print(f"    매수 불가 (예수금 {cash:,}원, 주가 {cur_price:,}원)")
                continue

            total = qty * cur_price
            print(f"    [매수] {name} {qty}주 @ {cur_price:,}원 = {total:,}원")

            if not args.dry_run:
                resp = client.order_cash(symbol, qty=qty, side="buy")
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, name, "buy", qty, cur_price, cash - total)
                    log.info("buy_order", symbol=symbol, qty=qty, price=cur_price)
                    return  # 1종목 매수 후 종료
            else:
                print("    (dry-run)")
                return

        except Exception as e:
            log.error("check_failed", symbol=symbol, error=str(e))
            print(f"    ERROR: {e}")

    print("  돌파 종목 없음. 현금 보유.")


if __name__ == "__main__":
    main()
