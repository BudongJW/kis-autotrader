"""장중 구조 진입품질 — 꼭지/바닥 추격 차단 + VWAP 정렬 검증.

2026-07-06 아침 실패(range 꼭지에서 롱 → 손절)가 실제로 차단되는지, 현재 저점 부근
상황에서 롱/인버스 둘 다 막히는지 회귀 검증. 룰 기반이라 테스트 가능(CLAUDE.md).
"""
from src.strategies.intraday_structure import (
    compute_intraday_structure, entry_quality, IntradayStructure,
)

CFG = {"max_long_range_pos": 0.70, "min_inverse_range_pos": 0.30,
       "vwap_align": True, "vwap_tol_pct": 0.0}


def _struct(range_pos, vs_vwap_pct, hi=100.0, lo=90.0):
    cur = lo + range_pos * (hi - lo)
    return IntradayStructure(
        cur=cur, session_high=hi, session_low=lo, vwap=cur / (1 + vs_vwap_pct / 100),
        range_pos=range_pos,
        pullback_from_high_pct=(hi - cur) / hi * 100,
        bounce_from_low_pct=(cur - lo) / lo * 100,
        vs_vwap_pct=vs_vwap_pct, bars=20)


# ── compute 정확성 ──
def test_compute_basic():
    bars = [
        {"high": 102, "low": 98, "close": 100, "volume": 10},
        {"high": 105, "low": 100, "close": 104, "volume": 20},
        {"high": 104, "low": 101, "close": 101, "volume": 10},
    ]
    s = compute_intraday_structure(bars)
    assert s.session_high == 105 and s.session_low == 98
    assert s.cur == 101
    assert abs(s.range_pos - 3 / 7) < 1e-6      # (101-98)/(105-98)
    assert abs(s.vwap - 102.0) < 1e-6           # 가중평균
    assert s.vs_vwap_pct < 0                      # 101 < VWAP 102


def test_compute_empty_none():
    assert compute_intraday_structure([]) is None


# ── 오늘 아침 실패 재현: 꼭지에서 롱 → 차단 ──
def test_long_blocked_at_range_top():
    s = _struct(range_pos=0.95, vs_vwap_pct=+1.0)   # 상승했지만 꼭지
    ok, why = entry_quality(direction="long", s=s, cfg=CFG)
    assert not ok and "꼭지 추격" in why


def test_long_ok_on_pullback_above_vwap():
    s = _struct(range_pos=0.50, vs_vwap_pct=+0.3)   # 눌림목 + VWAP 위
    ok, why = entry_quality(direction="long", s=s, cfg=CFG)
    assert ok and "눌림목" in why


def test_long_blocked_below_vwap():
    s = _struct(range_pos=0.40, vs_vwap_pct=-0.5)   # range는 괜찮으나 VWAP 아래
    ok, why = entry_quality(direction="long", s=s, cfg=CFG)
    assert not ok and "VWAP 아래" in why


# ── 인버스: 바닥 추격 차단 / 되돌림에서 진입 ──
def test_inverse_blocked_at_range_bottom():
    s = _struct(range_pos=0.05, vs_vwap_pct=-1.0)   # 이미 바닥
    ok, why = entry_quality(direction="inverse", s=s, cfg=CFG)
    assert not ok and "바닥 추격" in why


def test_inverse_ok_on_bounce_below_vwap():
    s = _struct(range_pos=0.55, vs_vwap_pct=-0.3)   # 되돌림 + VWAP 아래
    ok, why = entry_quality(direction="inverse", s=s, cfg=CFG)
    assert ok and "되돌림" in why


def test_inverse_blocked_above_vwap():
    s = _struct(range_pos=0.60, vs_vwap_pct=+0.5)   # VWAP 위인데 인버스
    ok, why = entry_quality(direction="inverse", s=s, cfg=CFG)
    assert not ok and "VWAP 위" in why


# ── 오늘 현재(10:53): range 2%, VWAP 아래 → 롱·인버스 둘 다 차단(추격 회피) ──
def test_current_extreme_blocks_both():
    s = _struct(range_pos=0.02, vs_vwap_pct=-1.42)
    ok_l, _ = entry_quality(direction="long", s=s, cfg=CFG)
    ok_i, why_i = entry_quality(direction="inverse", s=s, cfg=CFG)
    assert not ok_l                       # VWAP 아래라 롱 금지
    assert not ok_i and "바닥 추격" in why_i  # 저점 추격 금지


# ── 폴백: 분봉 없으면 막지 않음 ──
def test_none_structure_passes():
    ok, _ = entry_quality(direction="long", s=None, cfg=CFG)
    assert ok
