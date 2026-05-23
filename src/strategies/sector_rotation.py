"""섹터 로테이션 — 모멘텀 기반 자동 순환 + 집중도 제한.

섹터 간 상대 모멘텀으로 자금을 배분하되,
단일 섹터에 과도하게 집중되지 않도록 가중치 상한을 둔다.

로테이션 주기: 주 1회 (월요일 장전 학습)
평가 지표: 5일/20일 수익률 + 거래량 비율 + 수급 신호
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

ROTATION_STATE_PATH = Path("logs/sector_rotation.json")

MAX_SECTOR_WEIGHT = 0.35
MIN_SECTORS = 3


@dataclass
class SectorScore:
    """섹터별 종합 점수."""
    name: str
    symbol: str
    momentum_score: float   # 모멘텀 복합 점수 (0~100)
    ret_5d: float
    ret_20d: float
    vol_ratio: float
    flow_score: float       # 수급 신호 (-1~+1)
    rank: int = 0
    weight: float = 0.0     # 배분 비중 (0~1)


@dataclass
class RotationSignal:
    """로테이션 결과."""
    date: str
    sectors: list[SectorScore]
    top_sectors: list[str]
    excluded_sectors: list[str]
    detail: str


def compute_sector_scores(
    sector_data: dict[str, dict],
    flow_data: dict[str, dict] | None = None,
) -> list[SectorScore]:
    """섹터 모멘텀 + 수급 → 종합 점수 계산.

    Args:
        sector_data: analyze_sector_momentum() 결과
        flow_data: load_flow_cache() 결과 (있으면 반영)
    """
    if flow_data is None:
        flow_data = {}

    scores = []
    for name, data in sector_data.items():
        ret_5d = data.get("ret_5d", 0)
        ret_20d = data.get("ret_20d", 0)
        vol_ratio = data.get("vol_ratio", 1.0)
        symbol = data.get("symbol", "")

        # 모멘텀 복합 점수: 단기(40%) + 중기(40%) + 거래량(20%)
        # 각 요소를 0~100으로 정규화
        mom_5d = max(0, min(100, 50 + ret_5d * 10))      # -5%→0, 0%→50, +5%→100
        mom_20d = max(0, min(100, 50 + ret_20d * 5))      # -10%→0, 0%→50, +10%→100
        vol_score = max(0, min(100, (vol_ratio - 0.5) * 100))  # 0.5→0, 1.5→100

        momentum_score = mom_5d * 0.4 + mom_20d * 0.4 + vol_score * 0.2

        # 수급 반영 (있으면 ±10 보정)
        flow_score = 0.0
        if symbol in flow_data:
            flow_score = flow_data[symbol].get("signal", 0.0)
            momentum_score += flow_score * 10

        momentum_score = max(0, min(100, momentum_score))

        scores.append(SectorScore(
            name=name,
            symbol=symbol,
            momentum_score=round(momentum_score, 1),
            ret_5d=ret_5d,
            ret_20d=ret_20d,
            vol_ratio=vol_ratio,
            flow_score=round(flow_score, 3),
        ))

    scores.sort(key=lambda s: s.momentum_score, reverse=True)
    for i, s in enumerate(scores):
        s.rank = i + 1

    return scores


def compute_rotation_weights(scores: list[SectorScore]) -> list[SectorScore]:
    """상위 섹터에 가중치 배분 (집중도 제한 적용).

    상위 50% 섹터에만 배분, 단일 섹터 최대 35%.
    """
    if not scores:
        return scores

    # 상위 50% 또는 최소 MIN_SECTORS개
    n_selected = max(MIN_SECTORS, len(scores) // 2)
    selected = scores[:n_selected]
    excluded = scores[n_selected:]

    # 모멘텀 점수 비례 배분
    total_score = sum(s.momentum_score for s in selected)
    if total_score <= 0:
        for s in selected:
            s.weight = round(1.0 / len(selected), 3)
    else:
        for s in selected:
            raw_weight = s.momentum_score / total_score
            s.weight = round(min(MAX_SECTOR_WEIGHT, raw_weight), 3)

    # 상한 초과분 재분배
    excess = sum(max(0, s.weight - MAX_SECTOR_WEIGHT) for s in selected)
    if excess > 0:
        uncapped = [s for s in selected if s.weight < MAX_SECTOR_WEIGHT]
        if uncapped:
            add_per = excess / len(uncapped)
            for s in uncapped:
                s.weight = round(min(MAX_SECTOR_WEIGHT, s.weight + add_per), 3)

    # 합이 1.0이 되도록 정규화
    w_sum = sum(s.weight for s in selected)
    if w_sum > 0:
        for s in selected:
            s.weight = round(s.weight / w_sum, 3)

    for s in excluded:
        s.weight = 0.0

    return scores


def run_sector_rotation(
    sector_data: dict[str, dict],
    flow_data: dict[str, dict] | None = None,
) -> RotationSignal:
    """섹터 로테이션 시그널 생성."""
    scores = compute_sector_scores(sector_data, flow_data)
    scores = compute_rotation_weights(scores)

    top = [s.name for s in scores if s.weight > 0]
    excluded = [s.name for s in scores if s.weight == 0]

    top_detail = ", ".join(f"{s.name}({s.weight:.0%})" for s in scores if s.weight > 0)
    detail = f"로테이션: {top_detail}" if top_detail else "로테이션 대상 없음"

    signal = RotationSignal(
        date=datetime.now().strftime("%Y-%m-%d"),
        sectors=scores,
        top_sectors=top,
        excluded_sectors=excluded,
        detail=detail,
    )

    _save_rotation_state(signal)
    return signal


def is_rotation_day() -> bool:
    """월요일인지 판단 (주 1회 로테이션)."""
    return datetime.now().weekday() == 0


def check_sector_concentration(
    holdings: dict[str, int],
    universe: list[dict],
    new_symbol: str,
) -> tuple[bool, str]:
    """신규 매수 시 섹터 집중도 확인.

    Args:
        holdings: {심볼: 수량}
        universe: 유니버스 목록 (name 포함)
        new_symbol: 매수 대상 심볼

    Returns:
        (매수 허용 여부, 사유)
    """
    sym_to_sector = {}
    for asset in universe:
        sym = asset["symbol"]
        name = asset.get("name", "")
        # 이름에서 섹터 키워드 추출
        for keyword in ["반도체", "2차전지", "바이오", "자동차", "금융", "IT",
                        "철강", "나스닥", "S&P", "골드", "채권"]:
            if keyword in name:
                sym_to_sector[sym] = keyword
                break
        else:
            sym_to_sector[sym] = name

    new_sector = sym_to_sector.get(new_symbol, "기타")

    # 현재 보유 종목 중 같은 섹터 수
    same_sector_count = 0
    total_held = len(holdings)
    for sym in holdings:
        if sym_to_sector.get(sym) == new_sector:
            same_sector_count += 1

    if total_held >= 2 and same_sector_count >= 2:
        return False, f"섹터 '{new_sector}' 이미 {same_sector_count}종목 보유 — 집중도 초과"

    return True, f"섹터 '{new_sector}' 집중도 정상 ({same_sector_count}/{total_held})"


def get_sector_priority_order(rotation_state: dict | None = None) -> list[str]:
    """로테이션 가중치 기반 섹터 우선순위 반환."""
    if rotation_state is None:
        rotation_state = _load_rotation_state()

    current = rotation_state.get("current", {})
    sectors = current.get("sectors", [])
    if not sectors:
        return []

    sorted_sectors = sorted(sectors, key=lambda s: s.get("weight", 0), reverse=True)
    return [s["name"] for s in sorted_sectors if s.get("weight", 0) > 0]


def _load_rotation_state() -> dict:
    if ROTATION_STATE_PATH.exists():
        try:
            return json.loads(ROTATION_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_rotation_state(signal: RotationSignal) -> None:
    ROTATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = _load_rotation_state()
    state["current"] = {
        "date": signal.date,
        "sectors": [asdict(s) for s in signal.sectors],
        "top_sectors": signal.top_sectors,
        "detail": signal.detail,
    }
    history = state.get("history", [])
    history.append({
        "date": signal.date,
        "top": signal.top_sectors,
        "excluded": signal.excluded_sectors,
    })
    if len(history) > 52:
        history = history[-52:]
    state["history"] = history
    ROTATION_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
