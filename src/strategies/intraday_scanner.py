"""장중 모멘텀 스캐너 — 실시간 급등/급락 감지.

장중 루프에서 5분 간격으로 실행.
거래량 급증 + 가격 돌파 조합으로 장중 기회를 포착.
기존 변동성 돌파와 독립적으로 동작하며, 보조 시그널로 활용.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.utils.logger import log


@dataclass
class IntradaySurge:
    """장중 급등 감지 결과."""
    symbol: str
    name: str
    change_pct: float      # 장중 등락률
    volume_ratio: float    # 거래량 비율 (오늘/5일 평균)
    signal: str            # "SURGE" / "NORMAL" / "DUMP"
    strength: float        # 0~1 신호 강도
    detail: str


# 감지 캐시 (중복 알림 방지)
_surge_cache: dict[str, float] = {}  # {symbol: last_alert_time}
SURGE_COOLDOWN = 600  # 같은 종목 10분 내 재알림 방지


def scan_intraday_momentum(
    client,
    universe: list[dict],
    min_change_pct: float = 2.0,
    min_volume_ratio: float = 2.0,
) -> list[IntradaySurge]:
    """유니버스 전체를 스캔하여 장중 급등 종목 탐색.

    Args:
        client: KISClient
        universe: [{"symbol": ..., "name": ...}, ...]
        min_change_pct: 장중 등락률 최소 기준 (%)
        min_volume_ratio: 거래량 비율 최소 기준

    Returns:
        급등/급락 감지된 종목 리스트
    """
    from src.bot.single_run import get_price

    results = []
    now = time.time()

    for stock in universe:
        symbol = stock["symbol"]
        name = stock.get("name", symbol)

        # 쿨다운 체크
        if symbol in _surge_cache and now - _surge_cache[symbol] < SURGE_COOLDOWN:
            continue

        try:
            resp = client.get_daily_price(symbol)
            if resp.get("rt_cd") != "0":
                continue

            output = resp.get("output", [])
            if not output:
                continue

            today = output[0]  # 가장 최근 (오늘)
            cur_price = int(today.get("stck_clpr", 0))
            open_price = int(today.get("stck_oprc", 0))
            today_vol = int(today.get("acml_vol", 0))

            if open_price <= 0 or cur_price <= 0:
                continue

            change_pct = (cur_price - open_price) / open_price * 100

            # 5일 평균 거래량
            if len(output) >= 6:
                avg_vol = sum(int(o.get("acml_vol", 0)) for o in output[1:6]) / 5
            else:
                avg_vol = today_vol

            volume_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

            # 급등 판별
            if change_pct >= min_change_pct and volume_ratio >= min_volume_ratio:
                strength = min(1.0, (change_pct / 5.0 + volume_ratio / 5.0) / 2)
                results.append(IntradaySurge(
                    symbol=symbol, name=name,
                    change_pct=round(change_pct, 2),
                    volume_ratio=round(volume_ratio, 1),
                    signal="SURGE",
                    strength=round(strength, 2),
                    detail=f"{name} +{change_pct:.1f}% (거래량 {volume_ratio:.1f}x)",
                ))
                _surge_cache[symbol] = now

            # 급락 감지 (경고용)
            elif change_pct <= -min_change_pct and volume_ratio >= min_volume_ratio:
                strength = min(1.0, (abs(change_pct) / 5.0 + volume_ratio / 5.0) / 2)
                results.append(IntradaySurge(
                    symbol=symbol, name=name,
                    change_pct=round(change_pct, 2),
                    volume_ratio=round(volume_ratio, 1),
                    signal="DUMP",
                    strength=round(strength, 2),
                    detail=f"{name} {change_pct:.1f}% (거래량 {volume_ratio:.1f}x)",
                ))
                _surge_cache[symbol] = now

        except Exception as e:
            log.debug("intraday_scan_error", symbol=symbol, error=str(e))
            continue

    results.sort(key=lambda s: abs(s.change_pct), reverse=True)
    return results


def get_surge_buy_candidates(
    surges: list[IntradaySurge],
    holdings: dict[str, int],
) -> list[IntradaySurge]:
    """급등 중 매수 후보만 필터링.

    이미 보유 중이거나 DUMP 신호는 제외.
    """
    return [
        s for s in surges
        if s.signal == "SURGE"
        and s.symbol not in holdings
        and s.strength >= 0.4
    ]
