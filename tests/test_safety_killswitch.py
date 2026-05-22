"""Killswitch 모듈 단위 테스트."""

import json
from pathlib import Path

import pytest

from src.safety import killswitch


@pytest.fixture
def tmp_killswitch_path(tmp_path, monkeypatch):
    """Killswitch 파일 경로를 임시 디렉토리로 교체."""
    tmp_file = tmp_path / "killswitch.json"
    monkeypatch.setattr(killswitch, "KILLSWITCH_PATH", tmp_file)
    return tmp_file


def test_no_file_means_off(tmp_killswitch_path):
    assert killswitch.get_mode() == "off"
    assert killswitch.is_full_stop() is False
    assert killswitch.is_buy_blocked() is False


def test_set_active_full_stop(tmp_killswitch_path, monkeypatch):
    # 부가 효과 (ledger/notifier) 차단
    monkeypatch.setattr("src.safety.ledger.log_event", lambda *a, **kw: None)
    monkeypatch.setattr("src.safety.notifier.notify_killswitch", lambda *a, **kw: None)

    killswitch.set_active(mode="full_stop", reason="test", set_by="pytest")

    assert tmp_killswitch_path.exists()
    data = json.loads(tmp_killswitch_path.read_text(encoding="utf-8"))
    assert data["active"] is True
    assert data["mode"] == "full_stop"

    assert killswitch.get_mode() == "full_stop"
    assert killswitch.is_full_stop() is True
    assert killswitch.is_buy_blocked() is True


def test_set_active_block_buy_only(tmp_killswitch_path, monkeypatch):
    monkeypatch.setattr("src.safety.ledger.log_event", lambda *a, **kw: None)
    monkeypatch.setattr("src.safety.notifier.notify_killswitch", lambda *a, **kw: None)

    killswitch.set_active(mode="block_buy_only", reason="test")

    assert killswitch.is_full_stop() is False
    assert killswitch.is_buy_blocked() is True


def test_clear(tmp_killswitch_path, monkeypatch):
    monkeypatch.setattr("src.safety.ledger.log_event", lambda *a, **kw: None)
    monkeypatch.setattr("src.safety.notifier.notify_killswitch", lambda *a, **kw: None)

    killswitch.set_active(mode="full_stop")
    assert killswitch.is_full_stop() is True

    killswitch.clear()
    assert killswitch.get_mode() == "off"
    assert killswitch.is_full_stop() is False
    assert killswitch.is_buy_blocked() is False


def test_corrupt_file_returns_off(tmp_killswitch_path):
    tmp_killswitch_path.write_text("not json", encoding="utf-8")
    assert killswitch.get_mode() == "off"


def test_invalid_mode_treated_as_full_stop(tmp_killswitch_path):
    tmp_killswitch_path.write_text(
        json.dumps({"active": True, "mode": "garbage"}),
        encoding="utf-8",
    )
    # 잘못된 모드는 안전하게 full_stop으로 fallback
    assert killswitch.get_mode() == "full_stop"
