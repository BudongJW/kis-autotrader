"""수급 신호 — pykrx 기관/외인 순매수 데이터 기반.

기관·외국인 동시 순매수(쌍끌이)는 강한 매수 신호,
동시 순매도는 경고 신호로 활용.
개인 과매수(외인·기관 매도 + 개인 매수)는 역행 신호.

장후 학습에서 수급 데이터를 수집하고 signal_fusion에 반영.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.utils.logger import log

FLOW_CACHE_PATH = Path("logs/flow_cache.json")


@dataclass
class FlowSignal:
    """수급 분석 결과."""
    symbol: str
    inst_net: int       # 기관 순매수 금액 (억원)
    foreign_net: int    # 외국인 순매수 금액 (억원)
    inst_streak: int    # 기관 연속 순매수 일수 (음수=순매도)
    foreign_streak: int # 외인 연속 순매수 일수
    signal: float       # -1.0 ~ +1.0 정규화 신호
    detail: str


def fetch_flow_data(symbol: str, days: int = 5) -> list[dict] | None:
    """pykrx로 최근 N일 투자자별 순매수 데이터 조회."""
    try:
        from pykrx import stock as krx
    except ImportError:
        log.warning("pykrx_not_installed")
        return None

    end = datetime.now()
    start = end - timedelta(days=days * 2)

    try:
        df = krx.get_market_trading_value_by_date(
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            symbol,
        )
        if df is None or df.empty:
            return None

        result = []
        for date_idx, row in df.iterrows():
            result.append({
                "date": date_idx.strftime("%Y-%m-%d"),
                "institution": int(row.get("기관합계", 0)),
                "foreign": int(row.get("외국인합계", 0)),
                "individual": int(row.get("개인", 0)),
            })
        return result[-days:]
    except Exception as e:
        log.warning("flow_fetch_failed", symbol=symbol, error=str(e))
        return None


def _calc_streak(values: list[int]) -> int:
    """연속 양수/음수 일수 계산."""
    if not values:
        return 0
    streak = 0
    last_sign = 1 if values[-1] >= 0 else -1
    for v in reversed(values):
        sign = 1 if v >= 0 else -1
        if sign == last_sign:
            streak += sign
        else:
            break
    return streak


def compute_flow_signal(symbol: str, days: int = 5) -> FlowSignal | None:
    """수급 신호 계산."""
    data = fetch_flow_data(symbol, days)
    if not data or len(data) < 2:
        return None

    inst_values = [d["institution"] for d in data]
    foreign_values = [d["foreign"] for d in data]

    # 억원 단위로 변환
    inst_net = sum(inst_values) // 100_000_000
    foreign_net = sum(foreign_values) // 100_000_000

    inst_streak = _calc_streak(inst_values)
    foreign_streak = _calc_streak(foreign_values)

    # 신호 계산: -1.0 ~ +1.0
    signal = 0.0

    # 기관+외인 동시 순매수 = 쌍끌이 (강한 양)
    if inst_net > 0 and foreign_net > 0:
        signal = min(1.0, 0.3 + 0.1 * min(inst_streak, 5) + 0.1 * min(foreign_streak, 5))
    # 기관+외인 동시 순매도 = 위험 (강한 음)
    elif inst_net < 0 and foreign_net < 0:
        signal = max(-1.0, -0.3 + 0.1 * max(inst_streak, -5) + 0.1 * max(foreign_streak, -5))
    # 외인만 순매수, 기관 매도 = 약한 양
    elif foreign_net > 0 and inst_net <= 0:
        signal = min(0.5, 0.1 * min(foreign_streak, 5))
    # 기관만 순매수, 외인 매도 = 약한 양
    elif inst_net > 0 and foreign_net <= 0:
        signal = min(0.4, 0.08 * min(inst_streak, 5))
    # 그 외: 중립
    else:
        signal = 0.0

    parts = []
    if inst_net != 0:
        parts.append(f"기관 {inst_net:+,}억({inst_streak:+d}일)")
    if foreign_net != 0:
        parts.append(f"외인 {foreign_net:+,}억({foreign_streak:+d}일)")
    detail = " / ".join(parts) if parts else "수급 중립"

    return FlowSignal(
        symbol=symbol,
        inst_net=inst_net,
        foreign_net=foreign_net,
        inst_streak=inst_streak,
        foreign_streak=foreign_streak,
        signal=round(signal, 3),
        detail=detail,
    )


def save_flow_cache(flows: dict[str, FlowSignal]) -> None:
    """수급 데이터 캐시 저장 (일 1회)."""
    FLOW_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "data": {
            sym: {
                "inst_net": f.inst_net,
                "foreign_net": f.foreign_net,
                "signal": f.signal,
                "detail": f.detail,
            }
            for sym, f in flows.items()
        },
    }
    FLOW_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_flow_cache() -> dict:
    """캐시된 수급 데이터 로드."""
    if not FLOW_CACHE_PATH.exists():
        return {}
    try:
        cache = json.loads(FLOW_CACHE_PATH.read_text(encoding="utf-8"))
        if cache.get("date") == datetime.now().strftime("%Y-%m-%d"):
            return cache.get("data", {})
    except Exception:
        pass
    return {}
