"""수수료 인지 진입 게이트 — 왕복 거래비용을 못 넘을 얕은 거래를 차단.

검은 월요일 PSQ 사례: +0.54% 스캘핑이 미국 왕복수수료(~0.5%)에 거의 다 먹혀
본전. 데이트레이딩(미국 야간은 매일 청산)에서 수수료는 큰 허들이므로, 기대
변동폭이 왕복비용을 충분히 넘는 종목·상황에서만 진입하도록 거른다.

순수 함수 — 위험을 늘리지 않고 '얕은 거래'만 막는다(escalate-only).
"""
from __future__ import annotations

# 기본 거래비용(편도). config의 costs 섹션으로 덮어쓸 수 있다.
DEFAULT_COSTS = {
    "kr_fee_pct": 0.00015,   # 한국 위탁수수료 0.015%
    "kr_tax_pct": 0.0023,    # 한국 매도 증권거래세 0.23%
    "us_fee_pct": 0.0025,    # 미국 온라인 위탁수수료 ~0.25%
    "min_edge_buffer": 1.5,  # 기대변동 ≥ 왕복비용 × buffer 이어야 진입
}


def round_trip_cost(market: str, costs: dict | None = None) -> float:
    """왕복(매수+매도) 거래비용 비율. 미국: 수수료×2. 한국: 수수료×2 + 매도세."""
    c = {**DEFAULT_COSTS, **(costs or {})}
    if (market or "KR").upper() == "US":
        return c["us_fee_pct"] * 2
    return c["kr_fee_pct"] * 2 + c["kr_tax_pct"]


def required_edge(market: str, costs: dict | None = None) -> float:
    """진입에 필요한 최소 기대 변동폭 = 왕복비용 × buffer."""
    c = {**DEFAULT_COSTS, **(costs or {})}
    return round_trip_cost(market, c) * c.get("min_edge_buffer", 1.5)


def edge_clears_cost(expected_move_pct: float, market: str,
                     costs: dict | None = None) -> tuple[bool, str]:
    """기대 변동폭(예: ATR/price)이 왕복비용+버퍼를 넘는가.

    Args:
        expected_move_pct: 기대 가능 변동폭(0.01 = 1%). 보통 ATR%(일중 평균 진폭).
        market: "KR" / "US"
    Returns: (통과, 사유)
    """
    need = required_edge(market, costs)
    em = max(0.0, float(expected_move_pct or 0.0))
    if em < need:
        return False, (f"기대변동 {em:.2%} < 필요 {need:.2%}"
                       f"(왕복비용 {round_trip_cost(market, costs):.2%}×버퍼) — 수수료 못 넘어 스킵")
    return True, f"기대변동 {em:.2%} ≥ 필요 {need:.2%} — 진입 허용"


def atr_pct(atr_value: float, price: float) -> float:
    """ATR을 가격 대비 비율로. 기대 변동폭 프록시."""
    if not price or price <= 0:
        return 0.0
    return max(0.0, float(atr_value or 0.0) / float(price))
