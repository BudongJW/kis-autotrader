"""체결 검증기가 **국내 거래만** 대조하는지 검증.

`fetch_today_ccld()`는 `/uapi/domestic-stock/v1/trading/inquire-daily-ccld`
(TTTC8001R) — 국내 체결 조회다. 해외 체결은 이 응답에 절대 들어오지 않는다.
그런데 trades.csv에는 KR·US가 한 파일에 섞여 기록되므로, 예전엔 **모든 미국
거래가 영구적으로 `ccld_not_found`로 잡혔다.**

실제 운영 로그(2026-07-28 16:47):

    [체결 검증] reviewed=9, executed=7, rejected=0, pending=0
      ⚠️ {'symbol': 'PSQ', 'time': '003211', 'side': 'buy',  'issue': 'ccld_not_found'}
      ⚠️ {'symbol': 'PSQ', 'time': '044507', 'side': 'sell', 'issue': 'ccld_not_found'}

PSQ는 미국 인버스 ETF로 정상 체결됐는데도 오탐이 났다. 오탐이 일상이 되면 진짜
불일치가 묻히고, rejected/pending이 잡히면 텔레그램 오류 알림까지 나간다.
"""

import csv
from datetime import date

import pytest

from src.safety.execution_verifier import reconcile_trades


@pytest.fixture
def trade_log(tmp_path, monkeypatch):
    """trades.csv를 임시 파일로 바꿔치고, 오늘 날짜를 고정한다."""
    import src.safety.execution_verifier as ev
    import src.tracker as tracker

    today = date(2026, 7, 28)
    path = tmp_path / "trades.csv"
    monkeypatch.setattr(ev, "today_kst", lambda: today)

    def write(rows):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(tracker.FIELDS)
            for r in rows:
                w.writerow(r)
        monkeypatch.setattr(ev, "TRADE_LOG_PATH", path)

    return write


def _row(hhmmss, symbol, side, qty=10, price=100):
    ts = f"2026-07-28T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}"
    return [ts, symbol, symbol, side, qty, price, qty * price, 0, ""]


def _ccld(entries):
    """fetch_today_ccld 반환 형태: {(sym, HHMMSS, side): {...}}"""
    out = {}
    for sym, hhmmss, side, status in entries:
        out[(sym, hhmmss, side)] = {
            "ord_qty": 10, "ccld_qty": 10 if status == "executed" else 0,
            "status": status, "rjct_qty": "0", "ord_unpr": 100, "avg_price": 100,
        }
    return out


def _patch_ccld(monkeypatch, entries):
    import src.safety.execution_verifier as ev
    monkeypatch.setattr(ev, "fetch_today_ccld", lambda _c: _ccld(entries))


# ──────────────────────────────────────────────────────────
# 실제 운영에서 난 오탐 재현
# ──────────────────────────────────────────────────────────

def test_us_trades_are_not_false_flagged(trade_log, monkeypatch):
    """2026-07-28 운영 사례 재현: 국내 7건 정상 + 미국 PSQ 2건.

    예전엔 PSQ 2건이 ccld_not_found로 잡혀 경고가 나갔다.
    """
    kr = [_row(f"09{i:02d}00", "069500", "buy") for i in range(7)]
    us = [_row("003211", "PSQ", "buy"), _row("044507", "PSQ", "sell")]
    trade_log(kr + us)
    _patch_ccld(monkeypatch, [("069500", f"09{i:02d}00", "buy", "executed")
                              for i in range(7)])

    r = reconcile_trades(object())
    assert r["reviewed"] == 7            # 국내만 검증
    assert r["executed"] == 7
    assert r["skipped_overseas"] == 2    # PSQ는 검증 대상에서 제외
    assert r["mismatches"] == [], "미국 거래가 오탐으로 잡혔다"


def test_overseas_only_day_raises_no_alarm(trade_log, monkeypatch):
    """미국 거래만 있는 날(국내 휴장 등)에도 경고가 없어야 한다."""
    trade_log([_row("003211", "PSQ", "buy"), _row("044507", "QQQM", "sell")])
    _patch_ccld(monkeypatch, [])

    r = reconcile_trades(object())
    assert r["reviewed"] == 0
    assert r["skipped_overseas"] == 2
    assert r["mismatches"] == []


# ──────────────────────────────────────────────────────────
# 국내 검증 기능은 그대로 살아 있어야 한다
# ──────────────────────────────────────────────────────────

def test_domestic_missing_fill_still_detected(trade_log, monkeypatch):
    """국내 거래가 ccld에 없으면 여전히 잡아야 한다 (기능 유지)."""
    trade_log([_row("091500", "069500", "buy")])
    _patch_ccld(monkeypatch, [])          # ccld 비어 있음

    r = reconcile_trades(object())
    # ccld 자체가 비면 조회 실패로 보고 조기 반환 — 오탐을 만들지 않는다
    assert r["mismatches"] == []

    # ccld는 있는데 해당 주문만 없는 경우 → 진짜 불일치
    _patch_ccld(monkeypatch, [("005930", "100000", "buy", "executed")])
    r2 = reconcile_trades(object())
    assert r2["reviewed"] == 1
    assert len(r2["mismatches"]) == 1
    assert r2["mismatches"][0]["issue"] == "ccld_not_found"
    assert r2["mismatches"][0]["symbol"] == "069500"


def test_domestic_pending_and_executed_counted(trade_log, monkeypatch):
    trade_log([_row("091500", "069500", "buy"), _row("093000", "005930", "buy")])
    _patch_ccld(monkeypatch, [
        ("069500", "091500", "buy", "executed"),
        ("005930", "093000", "buy", "pending"),
    ])

    r = reconcile_trades(object())
    assert r["reviewed"] == 2
    assert r["executed"] == 1
    assert r["pending"] == 1
    assert r["skipped_overseas"] == 0


def test_no_trades_today(trade_log, monkeypatch):
    trade_log([])
    _patch_ccld(monkeypatch, [])
    r = reconcile_trades(object())
    assert r["reviewed"] == 0 and r["skipped_overseas"] == 0
