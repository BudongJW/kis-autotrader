"""trades.csv union-merge 테스트 — 거래 기록 누적·중복제거 검증."""
from __future__ import annotations

import csv
from pathlib import Path

from src.merge_trades import merge_rows, merge_files, HEADER


def _row(ts, sym, side, qty=1, price=100):
    return {"timestamp": ts, "symbol": sym, "name": sym, "side": side,
            "qty": str(qty), "price": str(price), "amount": str(qty * price),
            "balance_after": "0"}


def test_union_dedup():
    """동일 키(시각·종목·방향·수량·가격)는 한 번만."""
    a = [_row("2026-06-04T09:13:38", "091160", "buy")]
    b = [_row("2026-06-04T09:13:38", "091160", "buy"),  # 중복
         _row("2026-06-04T11:16:38", "091160", "sell")]
    merged = merge_rows(a, b)
    assert len(merged) == 2


def test_accumulation_across_runs():
    """오전 run + 오후 run 기록이 모두 누적된다(오늘 유실됐던 케이스)."""
    morning = [_row("2026-06-04T09:13:38", "091160", "buy", price=166100),
               _row("2026-06-04T11:16:38", "091160", "sell", price=168510)]
    afternoon = [_row("2026-06-04T14:44:06", "091160", "buy", price=169405),
                 _row("2026-06-04T15:21:08", "091160", "sell", price=168400)]
    merged = merge_rows(morning, afternoon)
    assert len(merged) == 4


def test_sorted_by_timestamp():
    merged = merge_rows(
        [_row("2026-06-04T15:21:08", "091160", "sell")],
        [_row("2026-06-04T09:13:38", "091160", "buy")],
    )
    assert [r["timestamp"] for r in merged] == [
        "2026-06-04T09:13:38", "2026-06-04T15:21:08"]


def test_empty_inputs():
    assert merge_rows([], []) == []
    assert merge_rows([]) == []


def test_merge_files_round_trip(tmp_path):
    base = tmp_path / "state" / "trades.csv"
    inc = tmp_path / "logs" / "trades.csv"
    base.parent.mkdir(parents=True)
    inc.parent.mkdir(parents=True)

    # canonical(오전), incoming(오후)
    with base.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerow(_row("2026-06-04T09:13:38", "091160", "buy", price=166100))
        w.writerow(_row("2026-06-04T11:16:38", "091160", "sell", price=168510))
    with inc.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerow(_row("2026-06-04T11:16:38", "091160", "sell", price=168510))  # 중복
        w.writerow(_row("2026-06-04T14:44:06", "091160", "buy", price=169405))

    total, added = merge_files(base, inc)
    assert total == 3 and added == 1  # 오전2 + 오후신규1, 중복1 제거

    rows = list(csv.DictReader(base.open(encoding="utf-8")))
    assert len(rows) == 3
    assert rows[0]["timestamp"] == "2026-06-04T09:13:38"  # 정렬 유지


def test_merge_files_missing_base(tmp_path):
    """canonical이 아직 없으면 incoming 그대로 생성."""
    base = tmp_path / "state" / "trades.csv"
    inc = tmp_path / "logs" / "trades.csv"
    inc.parent.mkdir(parents=True)
    with inc.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerow(_row("2026-06-04T09:13:38", "091160", "buy"))
    total, added = merge_files(base, inc)
    assert total == 1 and added == 1
    assert base.exists()


# ── 보유분 귀속(manual 오분류 해소) ──────────────────────────

def test_net_bought_symbols_attribution():
    """순매수>0인 종목만 봇 보유로 식별 (라운드트립 완료분은 제외)."""
    from src.journal_quick import _net_bought_symbols
    trades = [
        _row("2026-06-04T09:13:38", "091160", "buy"),
        _row("2026-06-04T11:16:38", "091160", "sell"),   # 091160 라운드트립 → net 0
        _row("2026-06-02T09:53:00", "498400", "buy"),      # net +1 → 봇 보유
        _row("2026-06-04T10:00:00", "069500", "buy", qty=2),
        _row("2026-06-04T13:00:00", "069500", "sell", qty=1),  # net +1 → 봇 보유
    ]
    held = _net_bought_symbols(trades)
    assert held == {"498400", "069500"}
    assert "091160" not in held  # 매수=매도면 미보유
