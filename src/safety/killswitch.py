"""Killswitch — 봇 즉시 정지/매수 차단 메커니즘.

3가지 모드:
  - "off" (기본): 정상 운영
  - "block_buy_only": 신규 매수만 차단. 매도/리스크관리는 계속.
  - "full_stop": 봇 즉시 종료 (loop break).

사용:
  KILLSWITCH_PATH 파일 존재 + active=true → 활성화.
  GitHub Actions의 workflow_dispatch로 토글 가능 (.github/workflows/killswitch.yml).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from src.utils.logger import log

KILLSWITCH_PATH = Path("logs/killswitch.json")

Mode = Literal["off", "block_buy_only", "full_stop"]


def _load() -> dict:
    if not KILLSWITCH_PATH.exists():
        return {"active": False, "mode": "off"}
    try:
        with KILLSWITCH_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"active": False, "mode": "off"}
    except Exception:
        return {"active": False, "mode": "off"}


def get_mode() -> Mode:
    """현재 killswitch 모드 반환."""
    data = _load()
    if not data.get("active", False):
        return "off"
    mode = data.get("mode", "full_stop")
    if mode not in ("off", "block_buy_only", "full_stop"):
        return "full_stop"
    return mode  # type: ignore[return-value]


def get_status() -> dict:
    """killswitch 상태 + 메타 정보 반환."""
    data = _load()
    return {
        "active": data.get("active", False),
        "mode": data.get("mode", "off") if data.get("active") else "off",
        "reason": data.get("reason", ""),
        "set_at": data.get("set_at", ""),
        "set_by": data.get("set_by", ""),
    }


def is_full_stop() -> bool:
    """봇 즉시 종료 모드인지."""
    return get_mode() == "full_stop"


def is_buy_blocked() -> bool:
    """신규 매수 차단 모드인지 (block_buy_only or full_stop)."""
    return get_mode() in ("block_buy_only", "full_stop")


def set_active(mode: Mode = "full_stop", reason: str = "manual", set_by: str = "user") -> None:
    """killswitch 활성화."""
    KILLSWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "active": True,
        "mode": mode,
        "reason": reason,
        "set_at": datetime.now().isoformat(timespec="seconds"),
        "set_by": set_by,
    }
    with KILLSWITCH_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.warning("killswitch_activated", mode=mode, reason=reason, set_by=set_by)
    # SQLite 이벤트 + 텔레그램 알람 (사이드 이펙트, 실패해도 무시)
    try:
        from src.safety.ledger import log_event
        log_event("killswitch_on", "warning",
                  {"mode": mode, "reason": reason, "set_by": set_by})
    except Exception:
        pass
    try:
        from src.safety.notifier import notify_killswitch
        notify_killswitch("on", mode=mode, reason=reason)
    except Exception:
        pass


def clear() -> None:
    """killswitch 해제."""
    KILLSWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "active": False,
        "mode": "off",
        "reason": "",
        "set_at": "",
        "set_by": "",
    }
    with KILLSWITCH_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("killswitch_cleared")
    try:
        from src.safety.ledger import log_event
        log_event("killswitch_off", "info", {})
    except Exception:
        pass
    try:
        from src.safety.notifier import notify_killswitch
        notify_killswitch("off")
    except Exception:
        pass


# ──────────────────────────────────────────────────────────
# CLI for workflow_dispatch toggle
# ──────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Killswitch toggle")
    parser.add_argument("action", choices=["on", "off", "status"])
    parser.add_argument("--mode", choices=["block_buy_only", "full_stop"], default="full_stop")
    parser.add_argument("--reason", default="manual via workflow")
    parser.add_argument("--by", default="github-actions")
    args = parser.parse_args()

    if args.action == "on":
        set_active(mode=args.mode, reason=args.reason, set_by=args.by)
        print(f"Killswitch ACTIVE: mode={args.mode}, reason={args.reason}")
    elif args.action == "off":
        clear()
        print("Killswitch CLEARED")
    elif args.action == "status":
        status = get_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
