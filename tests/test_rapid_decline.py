"""급락 조기 대응 트리거 테스트.

핵심 검증:
- 임계값별 레벨 판정 (NONE/CAUTION/BEAR/CRISIS)
- whipsaw 방지: 1일 단발 급락은 CAUTION까지만(인버스 X), 1일 극단은 CRISIS(현금)
- 인버스(BEAR)는 3일 지속 급락에서만
- escalate-only: 레짐을 더 방어적으로만 격상 (덜 방어적으로 안 바꿈)
"""
from __future__ import annotations

import pandas as pd

from src.strategies.bear_strategy import detect_rapid_decline, more_defensive

CFG = {"rapid_decline": {
    "enabled": True,
    "caution_1d": -0.03, "caution_3d": -0.05,
    "bear_3d": -0.08, "crisis_1d": -0.08, "crisis_3d": -0.12,
}}


def _hist(closes):
    return pd.DataFrame({"close": closes})


# ── 레벨 판정 ──────────────────────────────────────────────

def test_none_when_flat():
    assert detect_rapid_decline(_hist([100, 100.5, 100.2, 100.3, 100.1]), CFG)["level"] == "NONE"


def test_caution_on_1d_moderate_drop():
    # 1일 -4% → CAUTION (인버스 아님)
    assert detect_rapid_decline(_hist([100, 100, 100, 100, 96]), CFG)["level"] == "CAUTION"


def test_1d_drop_is_caution_not_bear_whipsaw_guard():
    """1일 -5% 단발은 CAUTION이지 BEAR(인버스) 아님 — 떨어지는 칼날 회피."""
    out = detect_rapid_decline(_hist([100, 100, 100, 100, 95]), CFG)
    assert out["level"] == "CAUTION", "1일 단발 급락으로 인버스(BEAR) 진입하면 안 됨"


def test_bear_only_on_3d_persistent():
    """3일 누적 -9%(지속) → BEAR (인버스 허용). 1일은 완만(-4%대)."""
    out = detect_rapid_decline(_hist([100, 100, 100, 100, 97, 95, 91]), CFG)
    assert out["level"] == "BEAR"


def test_crisis_on_1d_extreme():
    # 1일 -9% → CRISIS (현금, 인버스도 회피)
    assert detect_rapid_decline(_hist([100, 100, 100, 100, 91]), CFG)["level"] == "CRISIS"


def test_crisis_on_3d_crash():
    # 3일 누적 -13% → CRISIS
    assert detect_rapid_decline(_hist([100, 100, 100, 100, 95, 90, 87]), CFG)["level"] == "CRISIS"


def test_disabled_returns_none():
    cfg = {"rapid_decline": {"enabled": False}}
    assert detect_rapid_decline(_hist([100, 100, 100, 100, 80]), cfg)["level"] == "NONE"


def test_insufficient_data():
    assert detect_rapid_decline(_hist([100]), CFG)["level"] == "NONE"


# ── escalate-only 병합 ─────────────────────────────────────

def test_more_defensive_escalates():
    assert more_defensive("BULL", "BEAR") == "BEAR"
    assert more_defensive("CAUTION", "CRISIS") == "CRISIS"
    assert more_defensive("BULL", "CAUTION") == "CAUTION"


def test_more_defensive_never_deescalates():
    """이미 더 방어적이면 빠른 트리거가 약해도 유지 (덜 방어적으로 안 감)."""
    assert more_defensive("BEAR", "CAUTION") == "BEAR"
    assert more_defensive("CRISIS", "BULL") == "CRISIS"
    assert more_defensive("BEAR", "NONE") == "BEAR"


# ── detect_market_regime 통합 (escalate-only 적용) ─────────

def _patch_regime_deps(monkeypatch, rapid_level):
    import src.strategies.bear_strategy as bs
    monkeypatch.setattr(bs, "check_canary", lambda *a, **k: (0, {}))
    monkeypatch.setattr(bs, "_load_bear_state", lambda: {})
    monkeypatch.setattr(bs, "_save_bear_state", lambda d: None)
    monkeypatch.setattr(bs, "detect_rapid_decline",
                        lambda *a, **k: {"level": rapid_level, "ret_1d": -0.09,
                                         "ret_3d": None, "detail": "test"})
    return bs


def test_regime_escalates_on_rapid_crisis(monkeypatch):
    """평상시 BULL이라도 급락 트리거 CRISIS면 레짐이 CRISIS로 격상."""
    bs = _patch_regime_deps(monkeypatch, "CRISIS")
    uptrend = pd.DataFrame({"close": [100 + i * 0.1 for i in range(60)]})
    res = bs.detect_market_regime(uptrend, {}, hmm_state="unknown",
                                  hmm_confidence=0.5, cfg={})
    assert res.regime == "CRISIS"


def test_regime_unchanged_when_rapid_none(monkeypatch):
    """급락 트리거 NONE이면 레짐 그대로(BULL 유지)."""
    bs = _patch_regime_deps(monkeypatch, "NONE")
    uptrend = pd.DataFrame({"close": [100 + i * 0.1 for i in range(60)]})
    res = bs.detect_market_regime(uptrend, {}, hmm_state="unknown",
                                  hmm_confidence=0.5, cfg={})
    assert res.regime == "BULL"


# ── 레버리지 진입 게이트 (CLAUDE.md #6 가드) ────────────────

def test_leveraged_allowed_only_in_strong_uptrend():
    """BULL + bull + 고확신 + 급락無일 때만 허용."""
    from src.strategies.bear_strategy import leveraged_entry_allowed
    ok, _ = leveraged_entry_allowed("BULL", "NONE", "bull", 0.8)
    assert ok is True


def test_leveraged_blocked_in_sideways():
    from src.strategies.bear_strategy import leveraged_entry_allowed
    ok, reason = leveraged_entry_allowed("BULL", "NONE", "sideways", 0.9)
    assert ok is False and "횡보" in reason or "bull 아님" in reason


def test_leveraged_blocked_in_non_bull_regime():
    from src.strategies.bear_strategy import leveraged_entry_allowed
    for regime in ("CAUTION", "BEAR", "CRISIS"):
        ok, _ = leveraged_entry_allowed(regime, "NONE", "bull", 0.9)
        assert ok is False, f"{regime}에서 레버리지 허용되면 안 됨"


def test_leveraged_blocked_on_rapid_decline():
    from src.strategies.bear_strategy import leveraged_entry_allowed
    ok, _ = leveraged_entry_allowed("BULL", "CAUTION", "bull", 0.9)
    assert ok is False  # 급락 트리거 있으면 차단


def test_leveraged_blocked_on_low_confidence():
    from src.strategies.bear_strategy import leveraged_entry_allowed
    ok, _ = leveraged_entry_allowed("BULL", "NONE", "bull", 0.5)
    assert ok is False  # 확신 부족
