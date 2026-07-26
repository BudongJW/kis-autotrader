"""루프 실행시간 예산이 **개장 시점**부터 시작하는지 검증.

pre-market/pre-open cron은 개장 한참 전에 뜬다 (KR 08:00, US 동절기 21:30 →
개장까지 각각 60분·120분). 그 대기가 340분 예산을 먹으면 세션 한복판에서
불필요한 핸드오프가 일어난다. 예산은 실제 장이 열린 뒤부터 세야 한다.
"""

import re
from pathlib import Path

from src.bot.night_run import MAX_LOOP_RUNTIME_SEC as US_BUDGET
from src.bot.single_run import (
    MARKET_END, MARKET_OPEN, MAX_LOOP_RUNTIME_SEC as KR_BUDGET,
    _runtime_exceeded,
)


def test_runtime_exceeded_boundary():
    start = 1_000_000.0
    assert not _runtime_exceeded(start, start + KR_BUDGET - 1)
    assert _runtime_exceeded(start, start + KR_BUDGET)


def test_budget_stays_under_github_hard_timeout():
    """GitHub 하드 타임아웃(360분) 전에 스스로 끝나야 정리 스텝이 보장된다."""
    for budget in (KR_BUDGET, US_BUDGET):
        assert budget < 360 * 60
        assert (360 * 60 - budget) >= 15 * 60   # 셋업+정리 여유


def _loop_source(module_path: str, func: str) -> str:
    text = Path(module_path).read_text(encoding="utf-8")
    start = text.index(f"def {func}(")
    nxt = text.find("\ndef ", start + 1)
    return text[start:nxt if nxt != -1 else len(text)]


def test_kr_loop_resets_budget_at_open():
    src = _loop_source("src/bot/single_run.py", "run_loop")
    assert "session_started" in src, "개장 시점 예산 리셋 플래그가 없다"
    assert re.search(r"session_started\s*=\s*True", src)
    assert re.search(r"loop_start_epoch\s*=\s*epoch_now", src), \
        "개장 시 loop_start_epoch을 현재 시각으로 리셋해야 한다"


def test_us_loop_resets_budget_at_open():
    src = _loop_source("src/bot/night_run.py", "run_loop")
    assert "session_started" in src
    assert re.search(r"loop_start_epoch\s*=\s*epoch_now", src)


def test_kr_budget_covers_session_from_open():
    """개장부터 재면 09:00~15:30 세션에서 핸드오프가 최대 1회로 줄어든다."""
    session_min = ((MARKET_END.hour * 60 + MARKET_END.minute)
                   - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute))
    assert session_min == 390
    # 390분 세션 > 340분 예산이라 핸드오프 1회는 불가피하지만,
    # 08:00 기동분(=450분)일 때보다 늦게 일어난다.
    assert KR_BUDGET / 60 > session_min - 60
