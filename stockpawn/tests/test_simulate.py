"""리플레이 시뮬레이터 — 합성 시나리오로 오프라인 실행."""

from datetime import date
from decimal import Decimal

from scripts import simulate


def _demo(**kw):
    bars, info = simulate.demo_bars(date(2026, 1, 5))
    return simulate.run_simulation(info.code, 1000, date(2026, 1, 5), bars, info, **kw)


def test_demo_crash_triggers_margin_call_then_partial_liquidation():
    r = _demo()
    assert r.first("MARGIN_CALL") < r.first("LIQUIDATION_PENDING") < r.first("FILL")
    assert 0 < r.liquidated_qty < 1000  # 부분 처분
    assert r.loan.shortfall == 0
    filled_rows = [row for row in r.rows if "FILL" in row.events]
    assert all(row.ratio >= Decimal("1.40") for row in filled_rows)  # 처분 후 비율 회복
    report = simulate.format_report(r)
    assert "마진콜 최초" in report and "실현 손익" in report


def test_demo_maturity_expiry_closes_with_surplus():
    r = _demo(amount=Decimal("20000000"), days=105)
    assert r.loan.status == "CLOSED" and r.first("MATURITY_NOTICE") is not None
    assert r.loan.repaid_principal == Decimal("20000000") and r.loan.accrued_interest == 0
    assert r.released_qty > 0 and r.liquidated_qty + r.released_qty == 1000
    assert r.loan.surplus_returned > 0


def test_cli_demo_prints_report(capsys):
    assert simulate.main(["--demo", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "리플레이" in out and "대주(대부업자) 손익" in out


def test_csv_mode(tmp_path):
    bars, _ = simulate.demo_bars(date(2026, 1, 5))
    path = tmp_path / "bars.csv"
    path.write_text("date,close,volume\n" + "\n".join(f"{b.date},{b.close},{b.volume}" for b in bars),
                    encoding="utf-8")
    loaded, info = simulate.load_csv(path, "005930", "", "KOSPI", None)
    assert len(loaded) == len(bars) and info.market_cap is None
    r = simulate.run_simulation("005930", 1000, date(2026, 1, 5), loaded, info, days=10)
    assert r.tier == "C"  # 시가총액 미상 → 최고 C
