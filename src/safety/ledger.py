"""SQLite 거래 원장 — 모든 주문 시도·체결·이벤트의 영구 기록.

기존 logs/trades.csv는 backwards 호환으로 병행 유지.
이 ledger는 추가로:
  - 주문 시도 (체결·차단 모두) 보존
  - 보유 포지션 스냅샷
  - 이벤트 (killswitch, 에러, 정합성 점검) 감사 로그
  - 정합성 점검 (KIS 잔고 vs 내부 원장)

테이블:
  orders      — 모든 주문 시도. attempted_at, side, symbol, name, qty, price, status, gate_reason
  executions  — 실제 체결. order_id, executed_at, qty, price, amount, broker_resp
  positions   — 보유 포지션 스냅샷 (정합성 점검용)
  events      — 감사 로그 (killswitch, error, reconciliation, sql migration 등)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils.logger import log

LEDGER_PATH = Path("logs/ledger.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'KR',
    side TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    notional REAL NOT NULL,
    status TEXT NOT NULL,          -- 'executed' / 'blocked' / 'rejected' / 'error'
    gate_reason TEXT,
    strategy TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(attempted_at);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id),
    executed_at TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'KR',
    side TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    broker_ref TEXT
);

CREATE INDEX IF NOT EXISTS idx_exec_date ON executions(executed_at);
CREATE INDEX IF NOT EXISTS idx_exec_symbol ON executions(symbol);

CREATE TABLE IF NOT EXISTS positions (
    snapshot_at TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'KR',
    symbol TEXT NOT NULL,
    name TEXT,
    qty INTEGER NOT NULL,
    avg_buy_price REAL,
    current_price REAL,
    source TEXT NOT NULL,          -- 'bot' or 'manual'
    PRIMARY KEY (snapshot_at, symbol)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    type TEXT NOT NULL,            -- 'killswitch_on', 'killswitch_off', 'error', 'reconcile', 'startup', 'shutdown'
    severity TEXT NOT NULL,        -- 'info', 'warning', 'error'
    payload TEXT                   -- JSON-encoded details
);

CREATE INDEX IF NOT EXISTS idx_events_date ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


def _ensure_db() -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(LEDGER_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def _connect():
    _ensure_db()
    conn = sqlite3.connect(LEDGER_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# Orders / Executions
# ──────────────────────────────────────────────────────────

def record_order_attempt(
    side: str,
    symbol: str,
    qty: int,
    price: float,
    status: str,                   # 'executed' / 'blocked' / 'rejected' / 'error'
    name: str = "",
    market: str = "KR",
    gate_reason: str = "",
    strategy: str = "",
    reason: str = "",
) -> int | None:
    """모든 주문 시도(체결·차단·거부 포함) 기록. order_id 반환."""
    try:
        with _connect() as conn:
            cur = conn.execute(
                """INSERT INTO orders
                   (attempted_at, market, side, symbol, name, qty, price, notional,
                    status, gate_reason, strategy, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    market, side, symbol, name, qty, price, qty * price,
                    status, gate_reason, strategy, reason,
                ),
            )
            return cur.lastrowid
    except Exception as e:
        log.warning("ledger_order_record_failed", error=str(e))
        return None


def record_execution(
    side: str,
    symbol: str,
    qty: int,
    price: float,
    name: str = "",
    market: str = "KR",
    order_id: int | None = None,
    broker_ref: str = "",
) -> None:
    """실제 체결 기록 (KIS rt_cd=0인 경우)."""
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO executions
                   (order_id, executed_at, market, side, symbol, name, qty, price, amount, broker_ref)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    datetime.now().isoformat(timespec="seconds"),
                    market, side, symbol, name, qty, price, qty * price, broker_ref,
                ),
            )
    except Exception as e:
        log.warning("ledger_exec_record_failed", error=str(e))


# ──────────────────────────────────────────────────────────
# Positions snapshot
# ──────────────────────────────────────────────────────────

def snapshot_positions(
    holdings: list[dict],
    market: str = "KR",
) -> None:
    """현재 보유 포지션 스냅샷 저장. journal_quick에서 호출."""
    if not holdings:
        return
    snapshot_at = datetime.now().isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            for h in holdings:
                conn.execute(
                    """INSERT OR REPLACE INTO positions
                       (snapshot_at, market, symbol, name, qty, avg_buy_price, current_price, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_at, market,
                        h.get("symbol", ""), h.get("name", ""),
                        h.get("qty", 0), h.get("buy_price", 0),
                        h.get("current_price", 0),
                        "manual" if h.get("manual", False) else "bot",
                    ),
                )
    except Exception as e:
        log.warning("ledger_snapshot_failed", error=str(e))


# ──────────────────────────────────────────────────────────
# Events / Audit log
# ──────────────────────────────────────────────────────────

def log_event(
    event_type: str,
    severity: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    """감사 로그 기록 (killswitch, error, reconciliation, startup 등)."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO events (occurred_at, type, severity, payload) VALUES (?, ?, ?, ?)",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    event_type, severity,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
    except Exception as e:
        log.warning("ledger_event_record_failed", error=str(e))


# ──────────────────────────────────────────────────────────
# Reconciliation: 계좌 잔고 vs 내부 원장
# ──────────────────────────────────────────────────────────

def reconcile(
    broker_holdings: dict[str, int],
    market: str = "KR",
) -> dict:
    """KIS 잔고 vs 원장 보유분 비교. 불일치 시 events에 기록.

    Args:
        broker_holdings: {symbol: qty} — KIS API 응답에서 추출
    Returns:
        {
            'matched': [...],         # 일치하는 종목
            'broker_only': [...],     # KIS에는 있는데 원장에 없음
            'ledger_only': [...],     # 원장에는 있는데 KIS에 없음
            'qty_mismatch': [...],    # 수량 차이
        }
    """
    result: dict[str, list[dict]] = {
        "matched": [], "broker_only": [], "ledger_only": [], "qty_mismatch": [],
    }
    try:
        with _connect() as conn:
            # 가장 최근 snapshot의 포지션
            row = conn.execute(
                "SELECT MAX(snapshot_at) as latest FROM positions WHERE market = ?",
                (market,),
            ).fetchone()
            latest = row["latest"] if row else None
            if not latest:
                # 첫 실행 — 원장에 데이터 없음. broker 그대로 정상으로 간주.
                result["broker_only"] = [{"symbol": s, "qty": q} for s, q in broker_holdings.items()]
                log_event("reconcile", "info", {"first_run": True, "market": market})
                return result

            ledger_positions: dict[str, int] = {}
            for r in conn.execute(
                "SELECT symbol, qty FROM positions WHERE snapshot_at = ? AND market = ?",
                (latest, market),
            ):
                ledger_positions[r["symbol"]] = int(r["qty"])

            all_symbols = set(broker_holdings.keys()) | set(ledger_positions.keys())
            for sym in all_symbols:
                bq = broker_holdings.get(sym, 0)
                lq = ledger_positions.get(sym, 0)
                if bq > 0 and lq == 0:
                    result["broker_only"].append({"symbol": sym, "broker_qty": bq})
                elif bq == 0 and lq > 0:
                    result["ledger_only"].append({"symbol": sym, "ledger_qty": lq})
                elif bq != lq:
                    result["qty_mismatch"].append({"symbol": sym, "broker_qty": bq, "ledger_qty": lq})
                else:
                    result["matched"].append({"symbol": sym, "qty": bq})

        # 불일치가 있으면 warning 이벤트
        if result["broker_only"] or result["ledger_only"] or result["qty_mismatch"]:
            log_event("reconcile_mismatch", "warning", {
                "market": market,
                "broker_only": result["broker_only"],
                "ledger_only": result["ledger_only"],
                "qty_mismatch": result["qty_mismatch"],
            })
        return result
    except Exception as e:
        log.warning("reconcile_failed", error=str(e))
        return result


# ──────────────────────────────────────────────────────────
# Query helpers (for journal_quick)
# ──────────────────────────────────────────────────────────

def get_recent_orders(limit: int = 50, status: str | None = None) -> list[dict]:
    try:
        with _connect() as conn:
            if status:
                rows = conn.execute(
                    """SELECT * FROM orders WHERE status = ?
                       ORDER BY attempted_at DESC LIMIT ?""",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM orders ORDER BY attempted_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_blocked_orders_today() -> list[dict]:
    """오늘 안전장치가 차단한 주문 목록."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT * FROM orders
                   WHERE status = 'blocked' AND attempted_at LIKE ?
                   ORDER BY attempted_at DESC""",
                (f"{today}%",),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_recent_events(limit: int = 30, severity: str | None = None) -> list[dict]:
    try:
        with _connect() as conn:
            if severity:
                rows = conn.execute(
                    """SELECT * FROM events WHERE severity = ?
                       ORDER BY occurred_at DESC LIMIT ?""",
                    (severity, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events ORDER BY occurred_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload", "{}"))
            except Exception:
                pass
            out.append(d)
        return out
    except Exception:
        return []
