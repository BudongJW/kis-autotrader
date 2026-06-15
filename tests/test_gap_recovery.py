"""gap_recovery_signal 단위 테스트.

핵심 불변식: 개장 윈도 한정(오후 추격 불가) + 촉발/갭/시가유지/레짐 가드.
"""
from src.strategies.gap_recovery import gap_recovery_signal


# 기본 발화 케이스: 09:05, 오버나이트 aggressive_buy, 시가갭 +2.5%, 현재가 시가 위
_BASE = dict(
    prev_close=10000.0, today_open=10250.0, cur_price=10300.0,
    now_hhmm="09:05", overnight_action="aggressive_buy", regime="CAUTION",
    blind=False, cfg=None,
)


def test_fires_on_catalyst_gap_in_window():
    sig = gap_recovery_signal(**_BASE)
    assert sig.is_buy is True
    assert sig.in_window is True
    assert round(sig.gap_open_pct, 1) == 2.5
    assert "갭회복 매수" in sig.reason


def test_rejects_after_window_anti_chase():
    """오후엔 갭이 충분해도 진입 금지(고점 추격 방지)."""
    sig = gap_recovery_signal(**{**_BASE, "now_hhmm": "13:30"})
    assert sig.is_buy is False
    assert sig.in_window is False
    assert "윈도" in sig.reason and "추격" in sig.reason


def test_rejects_before_open():
    sig = gap_recovery_signal(**{**_BASE, "now_hhmm": "08:55"})
    assert sig.is_buy is False
    assert sig.in_window is False


def test_rejects_small_gap():
    # 시가갭 +0.5% < 1.5% 임계
    sig = gap_recovery_signal(**{**_BASE, "today_open": 10050.0, "cur_price": 10060.0})
    assert sig.is_buy is False
    assert "갭 부족" in sig.reason


def test_rejects_below_open_gap_fading():
    # 시가는 갭상승했지만 현재가가 시가 아래로 무너짐 → 진입 안 함
    sig = gap_recovery_signal(**{**_BASE, "cur_price": 10100.0})
    assert sig.is_buy is False
    assert "시가 아래" in sig.reason


def test_rejects_without_catalyst():
    sig = gap_recovery_signal(**{**_BASE, "overnight_action": "normal"})
    assert sig.is_buy is False
    assert "촉발 없음" in sig.reason


def test_rejects_crisis_regime():
    sig = gap_recovery_signal(**{**_BASE, "regime": "CRISIS"})
    assert sig.is_buy is False
    assert "CRISIS" in sig.reason


def test_rejects_blind():
    sig = gap_recovery_signal(**{**_BASE, "blind": True})
    assert sig.is_buy is False
    assert "블라인드" in sig.reason


def test_rejects_missing_price_data():
    sig = gap_recovery_signal(**{**_BASE, "prev_close": 0.0})
    assert sig.is_buy is False
    assert "가격데이터 부족" in sig.reason


def test_custom_window_and_threshold():
    # 윈도를 09:30까지로 넓히고 갭 임계 3.0%로 올리면: 09:25는 윈도 내지만 2.5%<3.0% → 스킵
    cfg = {"window_end_kst": "09:30", "gap_min_pct": 3.0}
    sig = gap_recovery_signal(**{**_BASE, "now_hhmm": "09:25", "cfg": cfg})
    assert sig.in_window is True
    assert sig.is_buy is False
    assert "갭 부족" in sig.reason


def test_require_above_open_can_be_disabled():
    cfg = {"require_above_open": False}
    # 현재가가 시가 아래여도 require_above_open=False면 갭만 충족하면 진입
    sig = gap_recovery_signal(**{**_BASE, "cur_price": 10100.0, "cfg": cfg})
    assert sig.is_buy is True
