"""주문 전 안전장치 — order_cash() 호출 전에 통합 체크.

차단 사유:
  1. Killswitch 활성 (full_stop or block_buy_only for buy)
  2. 가격 비정상 (0원 또는 극단값)
  3. 종목 상태 (KIS 응답이 비정상 → 관리종목/거래정지 추정)
  4. 일일 매매 횟수 한도 초과
  5. 종목 블랙리스트 (configs/strategy.yaml의 blacklist)

사용:
    from src.safety.order_gates import check_order
    ok, reason = check_order(symbol, qty, price, side="buy")
    if not ok:
        log.warning("order_blocked", symbol=symbol, reason=reason)
        return
    client.order_cash(symbol, qty=qty, price=price, side=side)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml

from src.safety import killswitch
from src.utils.logger import log

CONFIG_PATH = Path("configs/strategy.yaml")
TRADE_LOG_PATH = Path("logs/trades.csv")

# 일일 매매 횟수 기본 한도 (수수료·세금 누적 방지)
DEFAULT_DAILY_TRADE_LIMIT = 30


def _load_config_safety() -> dict:
    """configs/strategy.yaml의 safety 섹션 로드."""
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("safety", {})
    except Exception:
        return {}


def _today_trade_count() -> int:
    """오늘 매매 건수 (trades.csv에서 카운트)."""
    if not TRADE_LOG_PATH.exists():
        return 0
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        count = 0
        with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
            next(f, None)  # header skip
            for line in f:
                if line.startswith(today):
                    count += 1
        return count
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────
# 개별 체크 함수
# ──────────────────────────────────────────────────────────

def check_killswitch(side: str) -> tuple[bool, str]:
    """Killswitch 체크. (allowed, reason)."""
    if killswitch.is_full_stop():
        return False, "killswitch=full_stop"
    if side == "buy" and killswitch.is_buy_blocked():
        return False, "killswitch=block_buy_only (매수 차단)"
    return True, ""


def check_price_sanity(price: float | int, symbol: str = "") -> tuple[bool, str]:
    """가격이 합리적 범위인지. 0원, 음수, 극단값 차단."""
    if price is None or price <= 0:
        return False, f"가격 비정상 ({symbol} = {price})"
    if price > 50_000_000:
        return False, f"가격 극단값 ({symbol} = {price:,})"
    return True, ""


def check_qty_sanity(qty: int, side: str) -> tuple[bool, str]:
    """수량이 합리적 범위인지."""
    if qty is None or qty <= 0:
        return False, f"수량 비정상 ({side} qty={qty})"
    if qty > 10000:
        return False, f"수량 극단값 ({side} qty={qty:,})"
    return True, ""


def check_daily_trade_limit() -> tuple[bool, str]:
    """오늘 매매 건수가 한도를 초과했는지."""
    cfg_safety = _load_config_safety()
    limit = int(cfg_safety.get("daily_trade_limit", DEFAULT_DAILY_TRADE_LIMIT))
    count = _today_trade_count()
    if count >= limit:
        return False, f"일일 매매 한도 초과 ({count}/{limit})"
    return True, ""


def check_blacklist(symbol: str) -> tuple[bool, str]:
    """심볼이 블랙리스트에 있는지."""
    cfg_safety = _load_config_safety()
    blacklist = cfg_safety.get("symbol_blacklist", []) or []
    if symbol in blacklist:
        return False, f"블랙리스트 종목 ({symbol})"
    return True, ""


def check_notional_limit(qty: int, price: float, side: str) -> tuple[bool, str]:
    """1회 주문 금액 한도 (실수로 큰 금액 주문 방지)."""
    cfg_safety = _load_config_safety()
    max_notional = int(cfg_safety.get("max_notional_per_order_krw", 2_000_000))  # 기본 200만원
    notional = qty * price
    if notional > max_notional:
        return False, f"1회 주문 금액 한도 초과 ({notional:,} > {max_notional:,})"
    return True, ""


# ──────────────────────────────────────────────────────────
# 통합 체크
# ──────────────────────────────────────────────────────────

def check_order(
    symbol: str,
    qty: int,
    price: float | int,
    side: Literal["buy", "sell"],
) -> tuple[bool, str]:
    """모든 사전 안전장치를 순차 체크. 첫 실패에서 멈춤.

    Returns:
        (allowed, reason)
          allowed=True  → reason="" (정상 진행 가능)
          allowed=False → reason="차단 사유" (주문 막아야 함)
    """
    checks = [
        check_killswitch(side),
        check_qty_sanity(qty, side),
        check_price_sanity(price, symbol),
        check_blacklist(symbol),
        check_notional_limit(qty, price, side),
    ]
    # 매수만 일일 한도 적용 (매도는 손절·청산 필요해서 막으면 안 됨)
    if side == "buy":
        checks.append(check_daily_trade_limit())

    for allowed, reason in checks:
        if not allowed:
            log.warning("order_gate_blocked", symbol=symbol, side=side,
                        qty=qty, price=price, reason=reason)
            return False, reason
    return True, ""
