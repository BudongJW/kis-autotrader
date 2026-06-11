"""수수료 인지 진입 게이트 테스트."""
from __future__ import annotations

from src.strategies.cost_gate import (
    round_trip_cost, required_edge, edge_clears_cost, atr_pct,
)


def test_round_trip_cost_us_vs_kr():
    # 미국: 0.25% × 2 = 0.5%
    assert abs(round_trip_cost("US") - 0.005) < 1e-9
    # 한국: 0.015%×2 + 0.23% = 0.26%
    assert abs(round_trip_cost("KR") - 0.0026) < 1e-9


def test_required_edge_includes_buffer():
    # 미국 필요 기대변동 = 0.5% × 1.5 = 0.75%
    assert abs(required_edge("US") - 0.0075) < 1e-9


def test_thin_move_blocked_us():
    """검은월요일 PSQ류: 기대변동이 미국 왕복비용 못 넘으면 차단."""
    ok, reason = edge_clears_cost(0.004, "US")   # 0.4% < 0.75% 필요
    assert not ok and "스킵" in reason


def test_sufficient_move_allowed_us():
    ok, _ = edge_clears_cost(0.012, "US")        # 1.2% ≥ 0.75%
    assert ok


def test_kr_threshold_lower_than_us():
    """한국은 세금 포함해도 왕복비용이 미국보다 낮아 문턱이 낮다."""
    assert required_edge("KR") < required_edge("US")
    assert edge_clears_cost(0.005, "KR")[0]      # 0.5% ≥ 0.39% 필요 → 허용
    assert not edge_clears_cost(0.005, "US")[0]  # 0.5% < 0.75% 필요 → 미국은 차단


def test_atr_pct():
    assert atr_pct(2.0, 100.0) == 0.02
    assert atr_pct(0.0, 100.0) == 0.0
    assert atr_pct(2.0, 0) == 0.0


def test_config_override():
    """costs config로 요율·버퍼 덮어쓰기."""
    cheap = {"us_fee_pct": 0.0007, "min_edge_buffer": 1.0}  # 이벤트 저수수료
    assert round_trip_cost("US", cheap) == 0.0014
    assert edge_clears_cost(0.002, "US", cheap)[0]  # 0.2% ≥ 0.14%


# ── 재진입 쿨다운 (US 일일 churn 방지) ──

def _sell(sym, date, reason="매도: 미국장 마감 청산"):
    return {"symbol": sym, "side": "sell", "date": date, "reason": reason}


def test_reentry_blocked_after_recent_force_close():
    from src.strategies.cost_gate import recently_force_closed
    sells = [_sell("SCHG", "2026-06-11"), _sell("XLF", "2026-06-08")]
    # 6-12 기준 2일 쿨다운: SCHG(6-11) 차단, XLF(6-08)는 만료
    assert recently_force_closed("SCHG", sells, "2026-06-12", 2)
    assert not recently_force_closed("XLF", sells, "2026-06-12", 2)


def test_reentry_allowed_if_not_force_close():
    """일반 손절·익절 매도는 쿨다운 대상 아님(마감청산만)."""
    from src.strategies.cost_gate import recently_force_closed
    sells = [_sell("SCHG", "2026-06-12", reason="매도: 손절매 -2%")]
    assert not recently_force_closed("SCHG", sells, "2026-06-12", 2)


def test_reentry_cooldown_disabled():
    from src.strategies.cost_gate import recently_force_closed
    sells = [_sell("SCHG", "2026-06-12")]
    assert not recently_force_closed("SCHG", sells, "2026-06-12", 0)  # 0=끄기


def test_reentry_works_with_raw_timestamp_rows():
    """trades.csv 원본 행(timestamp 키)도 인식 — 하니스가 잡은 inert 버그 회귀."""
    from src.strategies.cost_gate import recently_force_closed
    raw = [{"symbol": "SCHG", "side": "sell", "timestamp": "2026-06-12T04:45:21",
            "reason": "매도: 미국장 마감 청산"}]
    assert recently_force_closed("SCHG", raw, "2026-06-12", 2)
