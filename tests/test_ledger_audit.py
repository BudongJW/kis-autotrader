"""장부 정합성 감사 테스트 — 2026-08 미장 기록 파손(PSQ -11주) 재발 방지."""
from __future__ import annotations

from src.ledger_audit import audit, find_duplicate_sells, net_quantities


def _r(ts, sym, side, qty):
    return {"ts": ts, "symbol": sym, "side": side, "qty": qty}


def test_clean_ledger_has_no_issues():
    rows = [
        _r("2026-08-01T00:00:00", "PSQ", "buy", 10),
        _r("2026-08-01T04:45:00", "PSQ", "sell", 10),
    ]
    res = audit(rows)
    assert res.ok and res.net_by_symbol["PSQ"] == 0


def test_detects_phantom_sell_negative_net():
    """산 것보다 판 기록이 많으면 잡아낸다 — 실제 PSQ가 -11주였다."""
    rows = [
        _r("2026-08-01T00:00:00", "PSQ", "buy", 10),
        _r("2026-08-01T04:45:00", "PSQ", "sell", 10),
        _r("2026-08-02T04:45:00", "PSQ", "sell", 11),   # 보유 0인데 매도
    ]
    res = audit(rows)
    assert not res.ok
    neg = [i for i in res.issues if i.kind == "negative_net"]
    assert neg and neg[0].symbol == "PSQ" and neg[0].net_qty == -11


def test_detects_duplicate_sell_within_window():
    """2026-07-30 사례: 2분 간격 10주 매도 2건(보유는 10주)."""
    rows = [
        _r("2026-07-29T23:42:03", "PSQ", "buy", 10),
        _r("2026-07-30T02:03:58", "PSQ", "sell", 10),
        _r("2026-07-30T02:05:00", "PSQ", "sell", 10),
    ]
    dups = find_duplicate_sells(rows)
    assert dups and dups[0].symbol == "PSQ"
    assert "중복" in dups[0].detail


def test_separate_sells_far_apart_are_not_duplicates():
    rows = [
        _r("2026-08-01T00:00:00", "PSQ", "buy", 20),
        _r("2026-08-01T01:00:00", "PSQ", "sell", 10),
        _r("2026-08-01T03:00:00", "PSQ", "sell", 10),   # 2시간 간격 — 정상 분할매도
    ]
    assert not find_duplicate_sells(rows)


def test_mismatch_against_broker_holdings():
    """장부 순수량과 실보유가 다르면 잡는다."""
    rows = [_r("2026-08-01T00:00:00", "QQQM", "buy", 3)]
    res = audit(rows, holdings={"QQQM": 1})
    mm = [i for i in res.issues if i.kind == "mismatch"]
    assert mm and mm[0].net_qty == 3 and mm[0].held_qty == 1


def test_net_quantities_ignores_non_trades():
    rows = [_r("t", "PSQ", "buy", 5), {"ts": "t", "symbol": "PSQ",
                                       "side": "dividend", "qty": 9}]
    assert net_quantities(rows) == {"PSQ": 5}
