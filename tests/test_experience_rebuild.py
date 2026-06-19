"""rebuild_experience_from_trades 단위 테스트.

영속 trades.csv에서 경험(win/loss 라벨 + TA)이 결정적으로 재구성되는지 검증.
"""
import csv
from pathlib import Path

from src.experience import (rebuild_experience_from_trades, _ta_from_reason,
                            _strategy_from_reason)

HEADER = ["timestamp", "symbol", "name", "side", "qty", "price", "amount",
          "balance_after", "reason"]


def _write_trades(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_ta_parse():
    assert _ta_from_reason("매수: 융합 STRONG_BUY (확률 73%, TA +23, 돌파 O)")["total"] == 23.0
    assert _ta_from_reason("TA -20.5 약함")["total"] == -20.5
    assert _ta_from_reason("돌파 없음") is None


def test_strategy_parse():
    assert _strategy_from_reason("갭회복 진입: ...") == "gap_recovery"
    assert _strategy_from_reason("US 매수: 변동성 돌파") == "us_etf"
    assert _strategy_from_reason("인컴(커버드콜) 매수") == "income"
    assert _strategy_from_reason("매수: 융합 STRONG_BUY") == "etf"


def test_roundtrip_win(tmp_path):
    p = tmp_path / "trades.csv"
    _write_trades(p, [
        {"timestamp": "2026-06-16T09:11:05", "symbol": "117700", "name": "철강",
         "side": "buy", "qty": 2, "price": 8140, "amount": 16280,
         "balance_after": 0, "reason": "매수: 융합 STRONG_BUY (TA +23, 돌파 O)"},
        {"timestamp": "2026-06-16T09:31:05", "symbol": "117700", "name": "철강",
         "side": "sell", "qty": 1, "price": 8400, "amount": 8400,
         "balance_after": 0, "reason": "분할매도: 익절"},
        {"timestamp": "2026-06-16T09:45:05", "symbol": "117700", "name": "철강",
         "side": "sell", "qty": 1, "price": 8505, "amount": 8505,
         "balance_after": 0, "reason": "추적손절"},
    ])
    recs = rebuild_experience_from_trades(str(p))
    assert len(recs) == 2          # 매수 2주가 2번의 매도로 각각 청산 → 2 레코드
    assert all(r["action"] == "buy" and r["outcome"] == "win" for r in recs)
    assert recs[0]["ta_scores"]["total"] == 23.0
    assert round(recs[0]["pnl_pct"], 1) == 3.2   # (8400-8140)/8140
    assert recs[1]["pnl_pct"] > 4                  # (8505-8140)/8140 ≈ 4.5


def test_roundtrip_loss_and_us_symbol_norm(tmp_path):
    p = tmp_path / "trades.csv"
    _write_trades(p, [
        {"timestamp": "2026-06-10T00:39", "symbol": "XLF", "name": "금융",
         "side": "buy", "qty": 3, "price": 5213, "amount": 15639,
         "balance_after": 0, "reason": "US 매수: 변동성 돌파 + TA +22"},
        {"timestamp": "2026-06-10T04:45", "symbol": "US_XLF", "name": "US_XLF",
         "side": "sell", "qty": 3, "price": 5100, "amount": 15300,
         "balance_after": 0, "reason": "매도: 미국장 마감 청산"},
    ])
    recs = rebuild_experience_from_trades(str(p))
    assert len(recs) == 1
    assert recs[0]["symbol"] == "XLF"          # US_ 정규화로 매칭
    assert recs[0]["outcome"] == "loss"
    assert recs[0]["strategy"] == "us_etf"


def test_open_position_no_record(tmp_path):
    # 매수만 있고 미청산이면 경험 레코드 없음(결과 미확정)
    p = tmp_path / "trades.csv"
    _write_trades(p, [
        {"timestamp": "2026-06-19T13:24", "symbol": "069500", "name": "KOSPI200",
         "side": "buy", "qty": 1, "price": 146520, "amount": 146520,
         "balance_after": 0, "reason": "매수: 융합 STRONG_BUY"},
    ])
    assert rebuild_experience_from_trades(str(p)) == []


def test_empty_file(tmp_path):
    assert rebuild_experience_from_trades(str(tmp_path / "none.csv")) == []
