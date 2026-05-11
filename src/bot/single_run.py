"""단일 실행 봇 — GitHub Actions용.

매 실행마다 한 번 시세 확인 → 판단 → 주문 후 종료.
상태(보유 여부)는 KIS API 잔고 조회로 매번 확인하므로 별도 저장 불필요.

사용:
    python -m src.bot.single_run --symbol 005930
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time as dtime

from src.config import settings
from src.kis_client import KISClient
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.bot.runner import fetch_recent_history, calc_buy_qty, get_holding_qty
from src.utils.logger import log

MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 20)
MARKET_END = dtime(15, 30)
SELL_WINDOW_END = dtime(9, 10)


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 단일 실행 봇 (GitHub Actions용)")
    parser.add_argument("--symbol", required=True, help="6자리 종목코드")
    parser.add_argument("--dry-run", action="store_true", help="주문 안 보냄")
    args = parser.parse_args()

    now = datetime.now()
    t = now.time()
    symbol = args.symbol

    print(f"[{now:%Y-%m-%d %H:%M:%S}] single_run | {symbol} | mode={settings.mode.value}")

    # 장외 시간 체크
    if t < MARKET_OPEN or t > MARKET_END:
        print(f"  장외 시간 ({t:%H:%M}). 스킵.")
        return

    client = KISClient()
    strategy = VolatilityBreakoutStrategy()

    # 현재가 조회
    price_resp = client.get_price(symbol)
    if price_resp.get("rt_cd") != "0":
        print(f"  시세 조회 실패: {price_resp.get('msg1')}")
        sys.exit(1)
    cur_price = int(price_resp["output"]["stck_prpr"])
    held_qty = get_holding_qty(client, symbol)

    print(f"  현재가: {cur_price:,}원 | 보유: {held_qty}주")

    # ── 09:00~09:10 전일 보유분 매도 ──
    if MARKET_OPEN <= t <= SELL_WINDOW_END and held_qty > 0:
        print(f"  [매도] 전일 보유 {held_qty}주 시가 매도")
        if not args.dry_run:
            resp = client.order_cash(symbol, qty=held_qty, side="sell")
            log.info("morning_sell", qty=held_qty, price=cur_price, resp=resp)
            rt = resp.get("rt_cd")
            msg = resp.get("msg1", "")
            print(f"  주문 응답: rt_cd={rt}, msg={msg}")
            if rt != "0":
                sys.exit(1)
        else:
            print("  (dry-run: 주문 생략)")
        return  # 매도 후 이번 실행 종료 (다음 5분에 매수 판단)

    # ── 15:20 이후 미매도 청산 ──
    if t > MARKET_CLOSE and held_qty > 0:
        print(f"  [청산] 장 마감 전 {held_qty}주 청산")
        if not args.dry_run:
            resp = client.order_cash(symbol, qty=held_qty, side="sell")
            log.info("eod_sell", qty=held_qty, price=cur_price, resp=resp)
            print(f"  주문 응답: rt_cd={resp.get('rt_cd')}, msg={resp.get('msg1', '')}")
        else:
            print("  (dry-run: 주문 생략)")
        return

    # ── 장중: 이미 보유 중이면 스킵 ──
    if held_qty > 0:
        print(f"  이미 {held_qty}주 보유 중. 추가 매수 안 함.")
        return

    # ── 장중: 목표가 돌파 확인 ──
    if t > MARKET_CLOSE:
        print(f"  매수 시간 지남 ({t:%H:%M} > 15:20). 스킵.")
        return

    history = fetch_recent_history(client, symbol)
    signal = strategy.generate_signal(symbol, history)
    print(f"  신호: {signal.type.value} @ {cur_price:,}원 — {signal.reason}")

    if signal.type.value != "BUY":
        log.info("no_buy_signal", type=signal.type.value, price=cur_price, reason=signal.reason)
        return

    qty = calc_buy_qty(client, cur_price)
    if qty <= 0:
        print("  매수 가능 수량 0 (예수금 부족)")
        return

    print(f"  [매수] {qty}주 @ {cur_price:,}원")
    if not args.dry_run:
        resp = client.order_cash(symbol, qty=qty, side="buy")
        log.info("buy_order", qty=qty, price=cur_price, resp=resp)
        rt = resp.get("rt_cd")
        msg = resp.get("msg1", "")
        print(f"  주문 응답: rt_cd={rt}, msg={msg}")
        if rt != "0":
            sys.exit(1)
    else:
        print("  (dry-run: 주문 생략)")


if __name__ == "__main__":
    main()
