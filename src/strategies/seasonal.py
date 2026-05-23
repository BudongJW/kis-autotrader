"""시즌 필터 — 할로윈 전략 (Sell in May) 기반 신뢰도 조정.

통계적으로 KOSPI/S&P500 모두 11월~4월이 5월~10월보다 수익률이 높다.
이 필터는 매수 금지가 아니라, 비수기에 confidence를 줄여 포지션 크기를 축소한다.
"""

from __future__ import annotations

from datetime import datetime


def get_seasonal_adjustment() -> dict:
    """현재 월 기준 시즌 조정값 반환.

    Returns:
        dict with keys:
            - season: "favorable" (11~4월) or "unfavorable" (5~10월)
            - confidence_mult: 신뢰도 곱 (0.7~1.1)
            - reason: 설명 문자열
    """
    month = datetime.now().month

    if month in (11, 12, 1, 2, 3, 4):
        return {
            "season": "favorable",
            "confidence_mult": 1.05,
            "max_positions_adj": 0,
            "reason": f"{month}월: 유리한 시즌 (11~4월), 정상 운영",
        }
    elif month in (5, 6, 9, 10):
        return {
            "season": "unfavorable",
            "confidence_mult": 0.75,
            "max_positions_adj": -1,
            "reason": f"{month}월: 비수기 (Sell in May), 포지션 축소",
        }
    else:  # 7, 8월 (여름 바닥권 — 약간 보수적)
        return {
            "season": "unfavorable",
            "confidence_mult": 0.80,
            "max_positions_adj": -1,
            "reason": f"{month}월: 여름 비수기, 보수적 운영",
        }
