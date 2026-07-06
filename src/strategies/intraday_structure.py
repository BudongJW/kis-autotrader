"""장중 구조 분석 — 분봉으로 VWAP·세션고저·range내위치를 계산해 '진입 자리'의 품질을 본다.

봇이 진입에 쓰던 데이터는 전일종가/시가/현재가 3개뿐이라 '꼭지냐 눌림이냐'를 못 봤다.
그 결과가 갭 꼭지 추격 매수(2026-07-06 아침 조간 롱 2번 손절). 이 모듈은 분봉으로
세션 구조를 계산해, 방향(long/inverse)이 '좋은 자리'인지 판정한다.

원칙(순수 함수, 테스트 가능):
  - 롱은 상승추세(VWAP 위)의 '눌림목'에서. range 상단(꼭지) 추격 금지.
  - 인버스(숏)는 하락추세(VWAP 아래)의 '되돌림'에서. range 하단(바닥) 추격 금지.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IntradayStructure:
    cur: float                     # 현재가(최근 분봉 종가)
    session_high: float
    session_low: float
    vwap: float
    range_pos: float               # 0=세션저점, 1=세션고점
    pullback_from_high_pct: float  # 고점 대비 되돌림 %(양수 = 내려옴)
    bounce_from_low_pct: float     # 저점 대비 반등 %(양수 = 올라옴)
    vs_vwap_pct: float             # (현재-VWAP)/VWAP*100
    bars: int


def compute_intraday_structure(bars: list[dict]) -> IntradayStructure | None:
    """분봉 리스트로 세션 구조 계산.

    Args:
        bars: 각 원소 {"high","low","close","volume"} (키 이름 무관하게 아래 접근).
              시간순/역순 무관(고저·VWAP는 순서 불변).
    Returns: IntradayStructure. 유효 데이터 없으면 None.
    """
    if not bars:
        return None
    pv = tv = 0.0
    hi = lo = None
    last_close = 0.0
    for b in bars:
        h = float(b.get("high", 0) or 0)
        l = float(b.get("low", 0) or 0)
        c = float(b.get("close", 0) or 0)
        v = float(b.get("volume", 0) or 0)
        if c <= 0:
            continue
        last_close = c  # 호출측이 시간순 정렬해 넘기면 마지막이 최신
        typ = (h + l + c) / 3 if (h > 0 and l > 0) else c
        pv += typ * v
        tv += v
        hi = h if hi is None else max(hi, h)
        lo = l if lo is None else min(lo, l)
    if hi is None or lo is None or last_close <= 0:
        return None
    vwap = pv / tv if tv > 0 else last_close
    rng = hi - lo
    range_pos = (last_close - lo) / rng if rng > 0 else 0.5
    return IntradayStructure(
        cur=last_close,
        session_high=hi,
        session_low=lo,
        vwap=vwap,
        range_pos=range_pos,
        pullback_from_high_pct=(hi - last_close) / hi * 100 if hi > 0 else 0.0,
        bounce_from_low_pct=(last_close - lo) / lo * 100 if lo > 0 else 0.0,
        vs_vwap_pct=(last_close - vwap) / vwap * 100 if vwap > 0 else 0.0,
        bars=sum(1 for b in bars if float(b.get("close", 0) or 0) > 0),
    )


def entry_quality(*, direction: str, s: IntradayStructure, cfg: dict) -> tuple[bool, str]:
    """방향이 '좋은 진입 자리'인지 판정 — 꼭지/바닥 추격 차단 + VWAP 정렬.

    long   : VWAP 위(추세 상방) + range_pos <= max_long_range_pos(꼭지 아님)
    inverse: VWAP 아래(추세 하방) + range_pos >= min_inverse_range_pos(바닥 아님)

    Args:
        cfg: intraday_entry 설정.
          max_long_range_pos(기본 0.70): 롱은 range 이 값 이하에서만(꼭지추격 금지)
          min_inverse_range_pos(기본 0.30): 인버스는 range 이 값 이상에서만(바닥추격 금지)
          vwap_align(기본 True): VWAP 정렬 요구
          vwap_tol_pct(기본 0.0): VWAP 판정 여유(%). 롱은 vs_vwap >= -tol, 인버스는 <= +tol
    Returns: (진입 좋은 자리?, 사유)
    """
    if s is None:
        return True, "분봉 없음 — 구조 게이트 미적용(통과)"  # 데이터 없으면 막지 않음(폴백)
    max_long = float(cfg.get("max_long_range_pos", 0.70))
    min_inv = float(cfg.get("min_inverse_range_pos", 0.30))
    align = bool(cfg.get("vwap_align", True))
    tol = float(cfg.get("vwap_tol_pct", 0.0))
    pos_pct = s.range_pos * 100

    if direction == "long":
        if s.range_pos > max_long:
            return False, (f"꼭지 추격 차단 (range {pos_pct:.0f}% > {max_long*100:.0f}%, "
                           f"고점대비 -{s.pullback_from_high_pct:.2f}%뿐)")
        if align and s.vs_vwap_pct < -tol:
            return False, f"VWAP 아래에서 롱 금지 (VWAP대비 {s.vs_vwap_pct:+.2f}%)"
        return True, (f"눌림목 롱 자리 (range {pos_pct:.0f}%, VWAP대비 {s.vs_vwap_pct:+.2f}%, "
                      f"고점대비 -{s.pullback_from_high_pct:.2f}%)")

    if direction == "inverse":
        if s.range_pos < min_inv:
            return False, (f"바닥 추격 차단 (range {pos_pct:.0f}% < {min_inv*100:.0f}%, "
                           f"저점대비 +{s.bounce_from_low_pct:.2f}%뿐)")
        if align and s.vs_vwap_pct > tol:
            return False, f"VWAP 위에서 인버스 금지 (VWAP대비 {s.vs_vwap_pct:+.2f}%)"
        return True, (f"되돌림 인버스 자리 (range {pos_pct:.0f}%, VWAP대비 {s.vs_vwap_pct:+.2f}%)")

    return True, "방향 없음"
