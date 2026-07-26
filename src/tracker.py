"""성과 추적기 — 모든 거래 기록 + 누적 수익률 계산.

거래 기록을 CSV에 저장하고, 주간 성과 리포트를 생성한다.
GitHub Actions에서 artifact로 보관하거나 커밋으로 저장 가능.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from src.utils.clock import kst_stamp

TRADE_LOG_PATH = Path("logs/trades.csv")
FIELDS = ["timestamp", "symbol", "name", "side", "qty", "price", "amount",
          "balance_after", "reason"]


def is_kr_symbol(symbol: str) -> bool:
    """국내 종목이면 True.

    trades.csv에는 KR·US 체결이 한 파일에 섞여 기록되는데, **통화 단위가 다르다**
    — KR은 원, US는 센트(int(price*100)). market 파라미터는 log_trade가 받기만 하고
    CSV에는 쓰지 않으므로(FIELDS에 컬럼 없음), 당일 손익을 집계할 땐 반드시 시장을
    갈라야 한다. 안 그러면 US 체결의 센트 값이 원으로 둔갑해 KR 일일 손실 한도가
    엉뚱하게 계산된다.

    KRX 종목코드는 6자리 숫자, 미국 티커는 알파벳이라 형태로 구분 가능하다.
    (레거시 행에도 그대로 적용되므로 스키마 변경이 필요 없다)
    """
    s = str(symbol or "").strip()
    return s.isdigit() and len(s) == 6


def _ensure_file() -> None:
    TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TRADE_LOG_PATH.exists():
        with TRADE_LOG_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDS)
        return
    # 구 스키마(reason 컬럼 없음) 복원분이면 신 스키마로 마이그레이션.
    # (append는 위치 기반이라 헤더가 9컬럼이어야 reason이 올바르게 읽힘)
    try:
        with TRADE_LOG_PATH.open("r", encoding="utf-8", newline="") as f:
            header = next(csv.reader(f), [])
        if header and "reason" not in header:
            with TRADE_LOG_PATH.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            with TRADE_LOG_PATH.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)
    except Exception:
        pass


def log_trade(
    symbol: str,
    name: str,
    side: str,
    qty: int,
    price: float,
    balance_after: float = 0,
    market: str = "KR",
    reason: str = "",
) -> None:
    """거래 1건을 CSV에 기록 + 텔레그램 알람 (설정된 경우)."""
    _ensure_file()
    with TRADE_LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            kst_stamp(),
            symbol, name, side, qty, int(price),
            int(qty * price), int(balance_after), reason,
        ])
    # SQLite 원장에도 체결 기록 (CSV와 병행)
    try:
        from src.safety.ledger import record_execution
        record_execution(side, symbol, int(qty), float(price), name=name, market=market)
    except Exception:
        pass
    # 텔레그램 알람 (실패해도 거래 기록은 보존)
    try:
        from src.safety.notifier import notify_trade
        notify_trade(side, symbol, name, qty, price, reason=reason, market=market)
    except Exception:
        pass


def get_summary() -> dict:
    """누적 성과 요약 반환."""
    _ensure_file()
    buys = []
    sells = []
    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            amount = int(row["amount"])
            if row["side"] == "buy":
                buys.append(amount)
            elif row["side"] == "sell":
                sells.append(amount)

    total_invested = sum(buys)
    total_returned = sum(sells)
    total_trades = len(buys) + len(sells)
    pnl = total_returned - total_invested

    return {
        "total_trades": total_trades,
        "total_invested": total_invested,
        "total_returned": total_returned,
        "pnl": pnl,
        "pnl_pct": (pnl / total_invested * 100) if total_invested > 0 else 0,
    }
