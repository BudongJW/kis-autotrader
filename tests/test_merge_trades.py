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


def test_merge_backfills_empty_reason():
    """같은 거래가 reason 없이 먼저, reason과 함께 나중에 등장하면 근거를 채운다."""
    no_reason = dict(_row("2026-06-08T12:50:18", "091160", "sell"), reason="")
    with_reason = dict(_row("2026-06-08T12:50:18", "091160", "sell"),
                       reason="매도: 손절매 (-12.4% ≤ -3%)")
    merged = merge_rows([no_reason], [with_reason])
    assert len(merged) == 1
    assert merged[0]["reason"] == "매도: 손절매 (-12.4% ≤ -3%)"


def test_reason_preserved_through_merge(tmp_path):
    """AI 판단 근거(reason)가 머지 후에도 보존돼야 한다 (왜 샀나/팔았나)."""
    base = tmp_path / "trades.csv"
    inc = tmp_path / "inc.csv"
    with base.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        r = _row("2026-06-08T09:40:00", "114800", "buy")
        r["reason"] = "인버스 매수: CRISIS 레짐 하락대응 — 돌파(K=0.6) + TA +20"
        w.writerow(r)
    with inc.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        r = _row("2026-06-08T13:00:00", "114800", "sell")
        r["reason"] = "매도: [손절] -4%"
        w.writerow(r)
    merge_files(base, inc)
    rows = list(csv.DictReader(base.open(encoding="utf-8")))
    assert "reason" in HEADER
    by_side = {r["side"]: r["reason"] for r in rows}
    assert by_side["buy"].startswith("인버스 매수")
    assert by_side["sell"] == "매도: [손절] -4%"


def test_old_8col_row_migrates_safely(tmp_path):
    """reason 컬럼 없는 기존(8컬럼) trades.csv도 머지 시 빈 reason으로 안전 승격."""
    base = tmp_path / "trades.csv"
    inc = tmp_path / "inc.csv"
    # 구 스키마(8컬럼, reason 없음)
    with base.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "symbol", "name", "side", "qty", "price",
                    "amount", "balance_after"])
        w.writerow(["2026-06-01T09:00:00", "114800", "KODEX인버스", "buy",
                    "3", "5000", "15000", "0"])
    with inc.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        r = _row("2026-06-08T09:40:00", "114800", "buy", price=5100)
        r["reason"] = "인버스 매수: 돌파"
        w.writerow(r)
    total, added = merge_files(base, inc)
    assert total == 2 and added == 1
    rows = list(csv.DictReader(base.open(encoding="utf-8")))
    old = [r for r in rows if r["timestamp"].startswith("2026-06-01")][0]
    assert old["reason"] == ""  # 구행은 빈 reason으로 안전 승격


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


def test_traded_symbols(tmp_path):
    from src.merge_trades import traded_symbols
    p = tmp_path / "trades.csv"
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerow(_row("2026-06-04T09:13:38", "091160", "buy"))
        w.writerow(_row("2026-06-04T11:16:38", "091160", "sell"))
        w.writerow(_row("2026-06-02T09:53:39", "498400", "buy"))
    assert traded_symbols(p) == {"091160", "498400"}


# ── 캐리 포지션 흡수(손절 복구) ──────────────────────────────

def test_adopt_carried_positions(tmp_path, monkeypatch):
    """유니버스∩거래이력 캐리분만 흡수, 진짜 수동분은 보호."""
    import src.risk_manager as rm
    monkeypatch.setattr(rm, "POSITIONS_PATH", tmp_path / "positions.json")
    broker = {
        "091160": {"qty": 1, "buy_price": 166100, "current_price": 155000},
        "498400": {"qty": 2, "buy_price": 27670, "current_price": 26600},
        "005930": {"qty": 10, "buy_price": 70000, "current_price": 71000},  # 유니버스 밖
    }
    n = rm.adopt_carried_positions(broker, {"091160", "498400"}, {"091160", "498400"})
    assert n == 2
    pos = rm.load_positions()
    assert "091160" in pos and "498400" in pos
    assert "005930" not in pos              # 진짜 수동분 → 보호
    assert pos["091160"]["adopted"] is True
    assert pos["091160"]["qty"] == 1


def test_adopt_out_of_universe_but_traded(tmp_path, monkeypatch):
    """유니버스에서 빠진 레거시도 봇 거래이력이 있으면 흡수해 손절 관리(6-08 091160).

    유니버스 개편으로 091160이 유니버스 밖이 됐지만, 봇이 과거 거래했고 아직
    보유 중이면 청산까지 봇이 관리해야 한다(방치 손실 방지).
    """
    import src.risk_manager as rm
    monkeypatch.setattr(rm, "POSITIONS_PATH", tmp_path / "positions.json")
    broker = {
        "091160": {"qty": 1, "buy_price": 166100, "current_price": 142000},  # 유니버스 밖
        "005930": {"qty": 10, "buy_price": 70000, "current_price": 71000},   # 수동(미거래)
    }
    # universe엔 091160 없음, 하지만 traded엔 있음 → 흡수돼야
    n = rm.adopt_carried_positions(broker, universe_symbols=set(),
                                   traded_symbols={"091160"})
    assert n == 1
    pos = rm.load_positions()
    assert "091160" in pos and pos["091160"]["adopted"] is True
    assert "005930" not in pos  # 거래이력 없는 수동분 → 보호


def test_adopt_skips_already_tracked(tmp_path, monkeypatch):
    """이미 positions에 있으면 흡수 안 함(덮어쓰기 방지)."""
    import src.risk_manager as rm
    monkeypatch.setattr(rm, "POSITIONS_PATH", tmp_path / "positions.json")
    rm.save_positions({"091160": {"buy_price": 166100, "qty": 1}})
    broker = {"091160": {"qty": 1, "buy_price": 999999, "current_price": 155000}}
    n = rm.adopt_carried_positions(broker, {"091160"}, {"091160"})
    assert n == 0
    assert rm.load_positions()["091160"]["buy_price"] == 166100  # 원본 유지


def test_adopt_requires_trade_history(tmp_path, monkeypatch):
    """유니버스 안이어도 봇 거래이력 없으면 흡수 안 함(수동 보호)."""
    import src.risk_manager as rm
    monkeypatch.setattr(rm, "POSITIONS_PATH", tmp_path / "positions.json")
    broker = {"091160": {"qty": 1, "buy_price": 166100, "current_price": 155000}}
    n = rm.adopt_carried_positions(broker, {"091160"}, traded_symbols=set())
    assert n == 0


# ── 누적 손익 계산 (자금흐름 혼입 버그 수정) ──────────────────

def test_total_pnl_excludes_deposits():
    """6-05 실제 사례: 실현 소액 + 미실현 -14k, 총평가 909,610 → ~-1.4% (≠ -13.43%)."""
    from src.journal_quick import compute_total_pnl
    pnl, pct = compute_total_pnl(realized_pnl=1405, unrealized_pnl=-14000,
                                 total_value=909610)
    assert pnl == -12595
    assert -2.0 < pct < -1.0, f"실제 ~-1.4%여야 함 (현재 {pct}%)"


def test_total_pnl_positive():
    from src.journal_quick import compute_total_pnl
    pnl, pct = compute_total_pnl(50000, 30000, 1080000)
    assert pnl == 80000
    assert 7.5 < pct < 8.5  # 80000/(1080000-80000)=8%


def test_total_pnl_zero():
    from src.journal_quick import compute_total_pnl
    pnl, pct = compute_total_pnl(0, 0, 922856)
    assert pnl == 0 and pct == 0.0


# ── 시장별 거래비용(수수료) — 미국을 한국요율로 과소표시하던 버그 ──

def test_fee_us_vs_kr_rate():
    """미국 거래는 ~0.25%, 한국은 0.015%(+매도세). 시장 자동 판별."""
    from src.journal_quick import _trade_cost, _is_us_symbol
    assert _is_us_symbol("PSQ") and _is_us_symbol("SPLG")
    assert not _is_us_symbol("214980") and not _is_us_symbol("091160")
    # 미국 매수 $181.44(=18144 cents) → 0.25% = 45 cents
    assert _trade_cost({"symbol": "PSQ", "side": "buy", "amount": 18144}) == 45
    # 한국 매수 100만원 → 0.015% = 150원
    assert _trade_cost({"symbol": "214980", "side": "buy", "amount": 1_000_000}) == 150
    # 한국 매도 100만원 → 수수료 150 + 거래세 2300 = 2450
    assert _trade_cost({"symbol": "214980", "side": "sell", "amount": 1_000_000}) == 2450


def test_fee_us_round_trip_exceeds_thin_profit():
    """검은월요일 PSQ 사례: +0.54% 스캘핑은 미국 왕복수수료(~0.5%)에 거의 다 먹힘."""
    from src.journal_quick import _trade_cost
    gross = 7 * (2606 - 2592)  # +98 cents
    cost = (_trade_cost({"symbol": "PSQ", "side": "buy", "amount": 7 * 2592})
            + _trade_cost({"symbol": "PSQ", "side": "sell", "amount": 7 * 2606}))
    assert cost >= 90            # 왕복 ~91 cents
    assert gross - cost <= 10    # 실제 net은 거의 0 (본전)


# ── 체결 미확인 매도(phantom) 감지 ───────────────────────────

def test_net_position():
    from src.merge_trades import net_position
    trades = [
        _row("2026-06-04T09:13:38", "091160", "buy"),
        _row("2026-06-04T11:16:38", "091160", "sell"),   # net 0
        _row("2026-06-02T09:53:39", "498400", "buy"),      # net +1
        _row("2026-06-04T10:00:00", "069500", "buy", qty=2),
        _row("2026-06-04T13:00:00", "069500", "sell", qty=3),  # net -1 (과매도)
    ]
    net = net_position(trades)
    assert net["091160"] == 0
    assert net["498400"] == 1
    assert net["069500"] == -1


def test_find_unfilled_sells():
    """기록상 청산(net<=0)인데 broker엔 보유 = 미체결 매도(091160 사례)."""
    from src.merge_trades import find_unfilled_sells
    net = {"091160": 0, "498400": 1, "069500": -1}
    broker = {"091160": 1, "498400": 2, "069500": 2}
    phantom = find_unfilled_sells(net, broker)
    assert phantom == {"091160": 1, "069500": 2}  # 498400은 정상 보유(net+1)라 제외


def test_find_unfilled_sells_none():
    from src.merge_trades import find_unfilled_sells
    # 모두 net>0 정상 보유 → phantom 없음
    assert find_unfilled_sells({"498400": 2}, {"498400": 2}) == {}
    # broker 빈 잔고 → phantom 없음
    assert find_unfilled_sells({"091160": 0}, {}) == {}
