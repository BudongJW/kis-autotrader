"""주문 전 안전장치 단위 테스트."""

import pytest
import yaml

from src.safety import order_gates


@pytest.fixture(autouse=True)
def disable_killswitch(monkeypatch):
    """기본적으로 killswitch off로 가정 — 각 check 함수 격리 테스트."""
    monkeypatch.setattr(
        "src.safety.killswitch.is_full_stop", lambda: False
    )
    monkeypatch.setattr(
        "src.safety.killswitch.is_buy_blocked", lambda: False
    )


@pytest.fixture
def empty_safety_config(monkeypatch):
    """configs/strategy.yaml의 safety 섹션이 비어있다고 가정."""
    monkeypatch.setattr(order_gates, "_load_config_safety", lambda: {})


@pytest.fixture
def tmp_trades_csv(tmp_path, monkeypatch):
    """일일 매매 한도 테스트용 임시 trades.csv 경로."""
    p = tmp_path / "trades.csv"
    monkeypatch.setattr(order_gates, "TRADE_LOG_PATH", p)
    return p


# ───── check_price_sanity ─────

def test_price_zero_rejected():
    ok, reason = order_gates.check_price_sanity(0, "ABC")
    assert not ok
    assert "가격 비정상" in reason


def test_price_negative_rejected():
    ok, reason = order_gates.check_price_sanity(-100, "ABC")
    assert not ok


def test_price_normal_allowed():
    ok, reason = order_gates.check_price_sanity(10000, "ABC")
    assert ok
    assert reason == ""


def test_price_extreme_rejected():
    ok, reason = order_gates.check_price_sanity(100_000_000, "ABC")
    assert not ok
    assert "극단값" in reason


# ───── check_qty_sanity ─────

def test_qty_zero_rejected():
    ok, _ = order_gates.check_qty_sanity(0, "buy")
    assert not ok


def test_qty_normal():
    ok, _ = order_gates.check_qty_sanity(10, "buy")
    assert ok


def test_qty_extreme_rejected():
    ok, _ = order_gates.check_qty_sanity(100_000, "buy")
    assert not ok


# ───── check_killswitch ─────

def test_killswitch_full_stop_blocks_all(monkeypatch):
    monkeypatch.setattr("src.safety.killswitch.is_full_stop", lambda: True)
    monkeypatch.setattr("src.safety.killswitch.is_buy_blocked", lambda: True)

    ok, reason = order_gates.check_killswitch("buy")
    assert not ok
    ok, reason = order_gates.check_killswitch("sell")
    assert not ok


def test_killswitch_block_buy_only_allows_sell(monkeypatch):
    monkeypatch.setattr("src.safety.killswitch.is_full_stop", lambda: False)
    monkeypatch.setattr("src.safety.killswitch.is_buy_blocked", lambda: True)

    ok_buy, _ = order_gates.check_killswitch("buy")
    assert not ok_buy

    ok_sell, _ = order_gates.check_killswitch("sell")
    assert ok_sell  # 매도는 손절·청산 필요해서 허용


# ───── check_blacklist ─────

def test_blacklist_empty_allows_all(monkeypatch):
    monkeypatch.setattr(
        order_gates, "_load_config_safety",
        lambda: {"symbol_blacklist": []},
    )
    ok, _ = order_gates.check_blacklist("070480")
    assert ok


def test_blacklist_blocks_listed_symbol(monkeypatch):
    monkeypatch.setattr(
        order_gates, "_load_config_safety",
        lambda: {"symbol_blacklist": ["070480", "152550"]},
    )
    ok, reason = order_gates.check_blacklist("070480")
    assert not ok
    assert "블랙리스트" in reason


# ───── check_notional_limit ─────

def test_notional_default_limit(monkeypatch):
    monkeypatch.setattr(order_gates, "_load_config_safety", lambda: {})

    ok, _ = order_gates.check_notional_limit(10, 100_000, "buy")  # 100만원
    assert ok

    ok, reason = order_gates.check_notional_limit(100, 50_000, "buy")  # 500만원
    assert not ok
    assert "한도 초과" in reason


def test_notional_custom_limit(monkeypatch):
    monkeypatch.setattr(
        order_gates, "_load_config_safety",
        lambda: {"max_notional_per_order_krw": 500_000},
    )
    ok, _ = order_gates.check_notional_limit(10, 100_000, "buy")  # 100만원
    assert not ok  # 50만원 한도 초과


# ───── 통합 check_order ─────

def test_check_order_all_pass(empty_safety_config, tmp_trades_csv):
    ok, reason = order_gates.check_order("070480", 10, 1000, "buy")
    assert ok
    assert reason == ""


def test_check_order_blocked_by_price_zero(empty_safety_config, tmp_trades_csv):
    """070480 가격 0원 매도 시도 회귀 테스트 (오늘 발견된 버그)."""
    ok, reason = order_gates.check_order("070480", 1, 0, "sell")
    assert not ok
    assert "가격" in reason


def test_check_order_blocked_by_blacklist(monkeypatch, tmp_trades_csv):
    monkeypatch.setattr(
        order_gates, "_load_config_safety",
        lambda: {"symbol_blacklist": ["070480"]},
    )
    ok, reason = order_gates.check_order("070480", 1, 1000, "buy")
    assert not ok
    assert "블랙리스트" in reason
