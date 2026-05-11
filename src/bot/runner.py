"""실시간 봇 러너 — 모의/실전 분기 + 안전장치.

사용:
    # 모의투자 (.env의 MODE=paper)
    python -m src.bot.runner --strategy golden_cross --symbol 005930

    # 실전 (반드시 모의에서 검증 후, --live 명시적 확인)
    python -m src.bot.runner --strategy golden_cross --symbol 005930 --live

핵심 안전 원칙:
  - 기본 모드는 paper. --live 플래그 없으면 실전 진입 거부.
  - 실전 모드라도 .env의 MODE=live와 --live가 둘 다 일치해야 진입.
  - 매 분기 끝에 잔고·포지션·로그 기록.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import pandas as pd

from src.config import Mode, settings
from src.kis_client import KISClient
from src.strategies.base import SignalType
from src.strategies.golden_cross import GoldenCrossStrategy
from src.utils.logger import log

STRATEGY_REGISTRY = {
    "golden_cross": GoldenCrossStrategy,
}

POLL_INTERVAL_SEC = 60  # 1분마다 신호 평가 (테스트용. 실전은 봉 기준으로 조정)


def confirm_live_mode() -> bool:
    """실전 모드 진입 직전 사용자 명시 확인."""
    print("\n" + "=" * 60)
    print("⚠️  실전 계좌(LIVE) 모드로 진입하려 합니다.")
    print(f"   계좌:       {settings.account_full}")
    print(f"   Base URL:   {settings.base_url}")
    print(f"   이 봇은 실제 자금으로 매매합니다.")
    print("=" * 60)
    answer = input("정말로 실전 모드로 실행하시겠습니까? (yes 입력 시 진행): ").strip()
    return answer == "yes"


def fetch_recent_history(client: KISClient, symbol: str, days: int = 60) -> pd.DataFrame:
    """최근 N일치 일봉 시세를 DataFrame으로 반환.

    KIS API의 inquire-daily-price는 30영업일 정도만 반환하므로
    더 긴 기간이 필요하면 inquire-daily-itemchartprice 사용.
    """
    resp = client.get_daily_price(symbol)
    if resp.get("rt_cd") != "0":
        log.error("daily_price_failed", resp=resp)
        raise RuntimeError(f"일봉 조회 실패: {resp.get('msg1')}")

    rows = resp.get("output", [])
    if not rows:
        raise RuntimeError("일봉 데이터 비어있음")

    df = pd.DataFrame(rows)
    # KIS 응답 키 → 표준 OHLCV
    df = df.rename(
        columns={
            "stck_bsop_date": "date",
            "stck_oprc": "open",
            "stck_hgpr": "high",
            "stck_lwpr": "low",
            "stck_clpr": "close",
            "acml_vol": "volume",
        }
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").set_index("date")
    return df[["open", "high", "low", "close", "volume"]].tail(days)


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 실시간 자동매매 봇")
    parser.add_argument("--strategy", required=True, choices=list(STRATEGY_REGISTRY.keys()))
    parser.add_argument("--symbol", required=True, help="6자리 종목코드")
    parser.add_argument(
        "--live",
        action="store_true",
        help="실전 모드 (별도 확인 단계 통과 필요). 없으면 모의투자.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="신호 계산만 하고 주문은 안 보냄. 모니터링용.",
    )
    args = parser.parse_args()

    # 안전장치: --live와 환경변수 MODE 둘 다 일치해야 실전 모드
    if args.live and settings.mode != Mode.LIVE:
        log.error("live_flag_but_env_paper", env_mode=settings.mode.value)
        print(
            "❌ --live 플래그가 켜져있지만 .env의 MODE=live가 아닙니다.\n"
            f"   현재 MODE={settings.mode.value}\n"
            "   안전을 위해 두 가지가 모두 일치해야 실전 진입 가능합니다."
        )
        sys.exit(1)

    if settings.mode == Mode.LIVE:
        if not args.live:
            print("❌ .env의 MODE=live지만 --live 플래그가 없습니다. 모호하므로 종료.")
            sys.exit(1)
        if not confirm_live_mode():
            print("실전 모드 진입 취소.")
            sys.exit(0)

    strategy = STRATEGY_REGISTRY[args.strategy]()
    client = KISClient()

    log.info(
        "bot_started",
        strategy=strategy.name,
        symbol=args.symbol,
        mode=settings.mode.value,
        dry_run=args.dry_run,
    )
    print(f"🚀 봇 시작: {strategy.name} on {args.symbol} (mode={settings.mode.value})")

    try:
        while True:
            try:
                history = fetch_recent_history(client, args.symbol)
                signal = strategy.generate_signal(args.symbol, history)
                log.info(
                    "signal_generated",
                    type=signal.type.value,
                    price=signal.price,
                    reason=signal.reason,
                )
                print(f"[{datetime.now():%H:%M:%S}] {signal.type.value} @ {signal.price:.0f} — {signal.reason}")

                if args.dry_run:
                    pass
                elif signal.type == SignalType.BUY:
                    qty = 1  # TODO: 자본·종목별 사이징 모듈 추가
                    resp = client.order_cash(args.symbol, qty=qty, side="buy")
                    log.info("buy_order_sent", qty=qty, resp=resp)
                elif signal.type == SignalType.SELL:
                    qty = 1
                    resp = client.order_cash(args.symbol, qty=qty, side="sell")
                    log.info("sell_order_sent", qty=qty, resp=resp)

            except Exception as e:
                log.exception("loop_error", error=str(e))

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        log.info("bot_stopped_by_user")
        print("\n봇 정상 종료.")


if __name__ == "__main__":
    main()
