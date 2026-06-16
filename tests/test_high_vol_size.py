"""high_vol_size_factor 단위 테스트.

변동성 급등(터뷸런스/VIX) 시 전면차단(block)이 아니라 사이즈 축소(size_down)로
여전히 진입하게 하는 게 핵심. config로 동작 선택.
"""
from src.risk_manager import high_vol_size_factor


def test_no_high_vol_passthrough():
    f, blocked = high_vol_size_factor(0.8, False, {})
    assert f == 0.8 and blocked is False


def test_default_is_size_down_not_block():
    # 기본(config 없음) = size_down, 배수 0.5
    f, blocked = high_vol_size_factor(0.8, True, None)
    assert blocked is False
    assert abs(f - 0.4) < 1e-9


def test_custom_mult():
    f, blocked = high_vol_size_factor(1.0, True, {"high_vol_size_mult": 0.3})
    assert blocked is False
    assert abs(f - 0.3) < 1e-9


def test_block_mode_preserves_old_behavior():
    f, blocked = high_vol_size_factor(0.8, True, {"high_vol_action": "block"})
    assert blocked is True
    assert f == 0.0


def test_block_mode_only_when_high_vol():
    # block 모드여도 변동성 정상이면 차단 안 함
    f, blocked = high_vol_size_factor(0.8, False, {"high_vol_action": "block"})
    assert blocked is False and f == 0.8
