"""VIX (시카고 변동성지수) 기반 시장 환경 필터.

VIX = S&P500 옵션 내재 변동성 → 시장 공포 지수.

해석 기준:
  - VIX < 15: 안정 (적극 매수 가능)
  - 15 ~ 20: 보통 (정상 운영)
  - 20 ~ 30: 변동성 확대 (사이즈 축소)
  - 30 ~ 40: 공포 (보수적 모드, 새 매수 신중)
  - > 40: 극심 (매수 차단, 손절 가속)

yfinance로 ^VIX 종가 fetch. 캐시 1시간 (실시간 갱신 불필요).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.utils.logger import log

VIX_CACHE_PATH = Path("logs/vix_cache.json")
CACHE_TTL_SEC = 3600  # 1시간


@dataclass
class VIXLevel:
    value: float
    band: str                # 'calm', 'normal', 'elevated', 'fear', 'panic'
    confidence_multiplier: float  # 0.0~1.2, 시장 신뢰도 곱셈
    size_multiplier: float        # 0.0~1.0, 매수 사이즈 곱셈
    skip_buy: bool                # True면 신규 매수 차단
    detail: str


def _load_cache() -> Optional[dict]:
    if not VIX_CACHE_PATH.exists():
        return None
    try:
        with VIX_CACHE_PATH.open("r", encoding="utf-8") as f:
            cache = json.load(f)
        if time.time() - cache.get("fetched_at", 0) < CACHE_TTL_SEC:
            return cache
    except Exception:
        pass
    return None


def _save_cache(value: float) -> None:
    VIX_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with VIX_CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump({"value": value, "fetched_at": time.time()}, f)
    except Exception:
        pass


def fetch_vix() -> Optional[float]:
    """yfinance로 ^VIX 최근 종가 가져오기. 캐시 1시간."""
    cached = _load_cache()
    if cached:
        return cached["value"]

    try:
        import yfinance as yf
        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if hist.empty:
            return None
        value = float(hist["Close"].iloc[-1])
        _save_cache(value)
        return value
    except Exception as e:
        log.warning("vix_fetch_failed", error=str(e))
        return None


def classify_vix(value: float) -> VIXLevel:
    """VIX 값에서 시장 환경 분류."""
    if value < 15:
        return VIXLevel(value, "calm", 1.15, 1.0, False,
                        f"안정 (VIX={value:.1f}) — 적극 매수 가능")
    if value < 20:
        return VIXLevel(value, "normal", 1.0, 1.0, False,
                        f"보통 (VIX={value:.1f}) — 정상 운영")
    if value < 30:
        return VIXLevel(value, "elevated", 0.85, 0.7, False,
                        f"변동성 확대 (VIX={value:.1f}) — 사이즈 30% 축소")
    if value < 40:
        return VIXLevel(value, "fear", 0.6, 0.4, False,
                        f"공포 (VIX={value:.1f}) — 보수 매수, 사이즈 60% 축소")
    return VIXLevel(value, "panic", 0.3, 0.0, True,
                    f"극심 (VIX={value:.1f}) — 매수 차단")


def get_vix_filter() -> Optional[VIXLevel]:
    """현재 VIX 기반 필터. fetch 실패 시 None (기본 동작 유지)."""
    value = fetch_vix()
    if value is None or value <= 0:
        return None
    return classify_vix(value)
