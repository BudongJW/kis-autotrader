"""config: 법정 최고금리 강제(import 시점 ValueError) + 구조 검증."""

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app import config
from app.models import DecimalType
from app.money import D

ROOT = Path(__file__).resolve().parent.parent


def _import_config_with(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "import app.config as c; print(c.ANNUAL_RATE)"],
        cwd=ROOT, env={**os.environ, **env}, capture_output=True, text=True,
    )


def test_defaults_are_decimal_and_legal():
    assert Decimal("0.20") == config.ANNUAL_RATE
    assert isinstance(config.ANNUAL_RATE, Decimal)
    assert config.LATE_INTEREST_SURCHARGE == 0  # 연체 가산이자 없음
    assert config.EARLY_REPAYMENT_FEE_RATE == 0  # 중도상환수수료 없음
    assert config.BROKER == "mock"  # 실주문 기본 비활성
    assert all(isinstance(v, Decimal) for v in config.MAX_LTV_BY_TIER.values())


def test_rate_above_20_percent_fails_at_import():
    r = _import_config_with({"ANNUAL_RATE": "0.21"})
    assert r.returncode != 0
    assert "ValueError" in r.stderr and "법정 최고금리" in r.stderr


def test_lower_rate_is_allowed():
    r = _import_config_with({"ANNUAL_RATE": "0.15"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0.15"


def test_unknown_broker_fails_at_import():
    r = _import_config_with({"BROKER": "real"})
    assert r.returncode != 0 and "BROKER" in r.stderr


@pytest.mark.parametrize(
    "rate,late,fee",
    [
        (Decimal("0.2000001"), Decimal("0"), Decimal("0")),
        (Decimal("0.20"), Decimal("0.03"), Decimal("0")),  # 약정 + 연체가산 3%p
        (Decimal("0.19"), Decimal("0"), Decimal("0.02")),  # 수수료도 이자로 간주
        (Decimal("0"), Decimal("0"), Decimal("0")),
    ],
)
def test_validate_rates_rejects(rate, late, fee):
    with pytest.raises(ValueError):
        config.validate_rates(rate, late, fee)


def test_validate_rates_accepts_exact_cap():
    config.validate_rates(Decimal("0.20"), Decimal("0"), Decimal("0"))
    config.validate_rates(Decimal("0.18"), Decimal("0.02"), Decimal("0"))


def test_new_loans_cannot_start_in_margin_call():
    # 최대 LTV에서 시작해도 담보비율 ≥ 1/LTV ≥ 유지비율
    for ltv in config.MAX_LTV_BY_TIER.values():
        assert ltv * config.MAINTENANCE_RATIO <= 1


def test_float_is_rejected_for_money():
    with pytest.raises(TypeError):
        D(0.1)
    with pytest.raises(TypeError):
        DecimalType().process_bind_param(0.1, dialect=type("d", (), {"name": "sqlite"})())
