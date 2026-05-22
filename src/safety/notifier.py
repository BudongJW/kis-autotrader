"""텔레그램 알람 — 체결·에러·일일요약·killswitch.

설정:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경변수 필요.
  미설정 시 모든 notify_* 함수는 no-op (graceful degradation).

GitHub Actions에서 secret으로 주입:
  env:
    TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
    TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}

봇 설정 방법:
  1. @BotFather에게 /newbot 명령 → 토큰 발급
  2. 본인 봇과 대화 시작 후 /start
  3. https://api.telegram.org/bot<TOKEN>/getUpdates 에서 chat_id 확인
"""

from __future__ import annotations

import os
import time as time_mod
from typing import Any

import requests

from src.utils.logger import log

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_TIMEOUT = 5


def _enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def _send(text: str, parse_mode: str = "HTML") -> bool:
    """Telegram에 메시지 전송. 미설정·실패 시 False 반환 (예외 안 던짐)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        url = TELEGRAM_API.format(token=token)
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text[:4000],  # Telegram 메시지 최대 4096자
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        log.warning("telegram_send_failed", status=resp.status_code, body=resp.text[:200])
        return False
    except Exception as e:
        log.warning("telegram_send_exception", error=str(e))
        return False


# ──────────────────────────────────────────────────────────
# 알람 카테고리
# ──────────────────────────────────────────────────────────

def notify_trade(
    side: str,
    symbol: str,
    name: str,
    qty: int,
    price: int | float,
    reason: str = "",
    market: str = "KR",
    extra: dict[str, Any] | None = None,
) -> None:
    """매수/매도 체결 알림."""
    if not _enabled():
        return
    emoji = "🟢 BUY" if side == "buy" else "🔴 SELL"
    currency = "$" if market == "US" else "₩"
    total = qty * price
    msg = [
        f"<b>{emoji}</b> {market}",
        f"<b>{name}</b> <code>({symbol})</code>",
        f"수량: <b>{qty}</b>주 @ {currency}{price:,}",
        f"총액: {currency}{total:,}",
    ]
    if reason:
        msg.append(f"사유: {reason}")
    if extra:
        for k, v in extra.items():
            msg.append(f"{k}: {v}")
    _send("\n".join(msg))


def notify_error(
    error: str,
    context: str = "",
    traceback_str: str = "",
) -> None:
    """봇 에러 알림."""
    if not _enabled():
        return
    msg = ["<b>🚨 봇 에러</b>"]
    if context:
        msg.append(f"위치: {context}")
    msg.append(f"<code>{error[:500]}</code>")
    if traceback_str:
        msg.append(f"<pre>{traceback_str[:2000]}</pre>")
    _send("\n".join(msg))


def notify_summary(
    total_value: int,
    cash: int,
    holdings_count: int,
    day_pnl: int,
    cumul_pnl: int,
    trades_today: int,
    regime: str = "",
    confidence: float = 0.0,
) -> None:
    """일일 요약 알림."""
    if not _enabled():
        return
    pnl_emoji = "📈" if day_pnl > 0 else ("📉" if day_pnl < 0 else "➖")
    msg = [
        f"<b>{pnl_emoji} 일일 요약</b>",
        f"총자산: ₩{total_value:,}",
        f"예수금: ₩{cash:,}",
        f"보유: {holdings_count}종목",
        f"오늘 손익: <b>₩{day_pnl:+,}</b>",
        f"누적 손익: ₩{cumul_pnl:+,}",
        f"오늘 매매: {trades_today}건",
    ]
    if regime:
        msg.append(f"레짐: {regime} ({confidence:.0%})")
    _send("\n".join(msg))


def notify_killswitch(action: str, mode: str = "", reason: str = "") -> None:
    """Killswitch 활성화·해제 알림."""
    if not _enabled():
        return
    if action == "on":
        msg = [
            f"<b>⚠️ Killswitch 활성</b>",
            f"모드: <b>{mode}</b>",
            f"사유: {reason}",
        ]
    else:
        msg = [
            f"<b>✅ Killswitch 해제</b>",
            f"정상 운영 재개",
        ]
    _send("\n".join(msg))


def notify_bot_start(market: str = "KR", mode: str = "live", dry_run: bool = False) -> None:
    """봇 시작 알림 (옵션)."""
    if not _enabled():
        return
    flag = " (dry-run)" if dry_run else ""
    _send(f"▶️ <b>{market} 봇 시작</b> [{mode}{flag}]")


def notify_bot_stop(market: str = "KR", trades: int = 0, day_pnl: int = 0) -> None:
    """봇 종료 알림 (옵션)."""
    if not _enabled():
        return
    _send(f"⏹️ <b>{market} 봇 종료</b> | 매매 {trades}건, 손익 ₩{day_pnl:+,}")
