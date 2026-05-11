"""단일 실행 봇 — GitHub Actions용.

매 실행마다 전 종목 시세 확인 → 돌파 판단 → 주문 후 종료.
상태(보유 여부)는 KIS API 잔고 조회로 매번 확인하므로 별도 저장 불필요.

사용:
    python -m src.bot.single_run                  # 전체 universe
    python -m src.bot.single_run --dry-run         # 주문 없이 신호만
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
from src.utils.logger import log

MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 20)
MARKET_END = dtime(15, 30)
SELL_WINDOW_END = dtime(9, 10)

CONFIG_PATH = Path("configs/strategy.yaml")


def load_universe() -> list[dict]:
    """strategy.yaml에서 종목 목록을 로드."""
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("universe", {}).get("default", [])


def get_all_holdings(client: KISClient) -> dict[str, int]:
    """보유 종목 → {종목코드: 수량} 딕셔너리."""
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


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 단일 실행 봇 (GitHub Actions용)")
    parser.add_argument("--dry-run", action="store_true", help="주문 안 보냄")
    args = parser.parse_args()

    now = datetime.now()
    t = now.time()

    print(f"[{now:%Y-%m-%d %H:%M:%S}] single_run | mode={settings.mode.value}")

    # 장외 시간 체크
    if t < MARKET_OPEN or t > MARKET_END:
        print(f"  장외 시간 ({t:%H:%M}). 스킵.")
        return

    universe = load_universe()
    if not universe:
        print("  universe 비어있음. configs/strategy.yaml 확인.")
        sys.exit(1)

    client = KISClient()
    strategy = VolatilityBreakoutStrategy()
    holdings = get_all_holdings(client)

    print(f"  종목 수: {len(universe)} | 보유: {holdings if holdings else '없음'}")

    # ── 09:00~09:10 전일 보유분 전량 매도 ──
    if MARKET_OPEN <= t <= SELL_WINDOW_END and holdings:
        for symbol, qty in holdings.items():
            price_resp = client.get_price(symbol)
            cur_price = int(price_resp["output"]["stck_prpr"]) if price_resp.get("rt_cd") == "0" else 0
            print(f"  [매도] {symbol} {qty}주 @ ~{cur_price:,}원")
            if not args.dry_run:
                resp = client.order_cash(symbol, qty=qty, side="sell")
                log.info("morning_sell", symbol=symbol, qty=qty, price=cur_price, resp=resp)
                print(f"    응답: rt_cd={resp.get('rt_cd')}, msg={resp.get('msg1', '')}")
            else:
                print("    (dry-run)")
        return  # 매도 후 종료. 다음 실행에서 매수 판단.

    # ── 15:20 이후 미매도 청산 ──
    if t > MARKET_CLOSE and holdings:
        for symbol, qty in holdings.items():
            print(f"  [청산] {symbol} {qty}주")
            if not args.dry_run:
                resp = client.order_cash(symbol, qty=qty, side="sell")
                log.info("eod_sell", symbol=symbol, qty=qty, resp=resp)
                print(f"    응답: rt_cd={resp.get('rt_cd')}, msg={resp.get('msg1', '')}")
            else:
                print("    (dry-run)")
        return

    # ── 이미 보유 중이면 매수 안 함 ──
    if holdings:
        syms = ", ".join(f"{s}({q}주)" for s, q in holdings.items())
        print(f"  보유 중: {syms}. 추가 매수 안 함.")
        return

    # ── 장중: 전 종목 돌파 확인 → 첫 돌파 종목 매수 ──
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

            # 매수 가능 수량 확인
            qty = calc_buy_qty(client, cur_price)
            if qty <= 0:
                print(f"    매수 불가 (가격 {cur_price:,}원, 예수금 부족)")
                continue

            # 매수 실행
            print(f"    [매수] {name} {qty}주 @ {cur_price:,}원")
            if not args.dry_run:
                resp = client.order_cash(symbol, qty=qty, side="buy")
                log.info("buy_order", symbol=symbol, name=name, qty=qty, price=cur_price, resp=resp)
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    print(f"    매수 완료. 이번 실행 종료.")
                    return  # 1종목 매수 후 종료
            else:
                print(f"    (dry-run)")
                return  # dry-run도 1종목만

        except Exception as e:
            log.error("check_failed", symbol=symbol, error=str(e))
            print(f"    ERROR: {e}")
            continue

    print("  돌파 종목 없음.")


if __name__ == "__main__":
    main()
