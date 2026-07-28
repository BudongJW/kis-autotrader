"""시간대 안전장치 회귀 테스트.

이 프로젝트의 시각 계산은 cron(UTC)과 프로세스 벽시계(KST)의 이중 구조 위에
서 있다. 여기서 고정하는 것:

  1. 모든 GitHub Actions 워크플로에 `TZ: Asia/Seoul`이 있다.
     — 한 줄만 빠져도 예외 없이 전 시스템이 9시간 밀린다(조용한 실패).
  2. clock 헬퍼는 TZ 환경변수와 무관하게 KST를 준다.
  3. parse_kst()가 naive/aware 혼재를 흡수한다.
     — aware-naive 뺄셈 TypeError는 이미 한 번 터진 버그 클래스다.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from src.utils.clock import KST, kst_stamp, now_kst, parse_kst, today_kst

WORKFLOW_DIR = Path(".github/workflows")


# ──────────────────────────────────────────────────────────
# 1. 워크플로 TZ 설정
# ──────────────────────────────────────────────────────────

def _workflow_files():
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def test_workflow_dir_exists():
    assert _workflow_files(), f"{WORKFLOW_DIR}에 워크플로가 없다"


@pytest.mark.parametrize("wf", _workflow_files(), ids=lambda p: p.name)
def test_every_workflow_sets_kst(wf):
    """모든 워크플로가 TZ=Asia/Seoul을 설정해야 한다 (workflow env 또는 job env)."""
    data = yaml.safe_load(wf.read_text(encoding="utf-8"))

    if (data.get("env") or {}).get("TZ") == "Asia/Seoul":
        return
    for job in (data.get("jobs") or {}).values():
        if (job.get("env") or {}).get("TZ") == "Asia/Seoul":
            return
    pytest.fail(
        f"{wf.name}에 TZ: Asia/Seoul이 없다. naive datetime.now()가 UTC를 반환해 "
        f"일일 손실 한도·쿨다운·리포트 날짜가 9시간 어긋난다."
    )


@pytest.mark.parametrize("wf", _workflow_files(), ids=lambda p: p.name)
def test_cron_is_documented_as_utc(wf):
    """cron이 있는 워크플로는 UTC 해석임을 알 수 있게 KST 주석을 달아둔다.

    cron은 TZ와 무관하게 항상 UTC다. 주석 없이 두면 다음 사람이 KST로 착각하고
    9시간 틀린 스케줄을 넣는다.
    """
    text = wf.read_text(encoding="utf-8")
    if "cron:" not in text:
        pytest.skip("스케줄 없음")
    assert re.search(r"KST|UTC", text), f"{wf.name}의 cron에 KST/UTC 기준 주석이 없다"


# ──────────────────────────────────────────────────────────
# 2. TZ 환경변수와 무관한 KST 보장
# ──────────────────────────────────────────────────────────

def test_clock_is_kst_even_without_tz_env():
    """TZ가 UTC로 설정된 하위 프로세스에서도 now_kst()는 KST여야 한다.

    워크플로에서 TZ 한 줄이 빠진 상황의 시뮬레이션.
    """
    code = (
        "from src.utils.clock import now_kst, today_kst;"
        "n = now_kst();"
        "print(n.utcoffset().total_seconds(), n.date() == today_kst())"
    )
    env = {**os.environ, "TZ": "UTC"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, cwd=Path.cwd())
    assert out.returncode == 0, out.stderr
    offset, same_date = out.stdout.split()
    assert float(offset) == 9 * 3600      # KST = UTC+9
    assert same_date == "True"


def test_config_import_pins_timezone_when_tz_unset():
    """src.config import만으로 프로세스 TZ가 KST로 고정된다 (naive 호출부 2차 방어).

    방어 대상은 **TZ가 아예 없는** 환경이다 — 새 워크플로에서 `TZ: Asia/Seoul`
    한 줄을 빠뜨리면 GitHub 러너는 TZ 미설정(UTC)으로 뜬다.
    TZ를 명시적으로 지정한 경우는 의도로 보고 존중한다(아래 테스트).
    """
    code = (
        "import src.config, datetime;"
        "print(datetime.datetime.now().hour,"
        " datetime.datetime.now(datetime.timezone.utc).hour)"
    )
    env = {k: v for k, v in os.environ.items() if k != "TZ"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, cwd=Path.cwd())
    assert out.returncode == 0, out.stderr
    local_h, utc_h = (int(x) for x in out.stdout.split())
    assert (utc_h + 9) % 24 == local_h, "config import 후에도 naive now()가 KST가 아니다"


def test_explicit_tz_is_respected():
    """TZ를 명시적으로 지정했으면 덮어쓰지 않는다 (디버깅·재현 목적 보존).

    이 경우에도 clock 헬퍼는 KST를 주므로 매매 로직은 영향받지 않는다.
    """
    code = (
        "import src.config, os;"
        "from src.utils.clock import now_kst;"
        "print(os.environ['TZ'], now_kst().utcoffset().total_seconds())"
    )
    env = {**os.environ, "TZ": "UTC"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, cwd=Path.cwd())
    assert out.returncode == 0, out.stderr
    tz, offset = out.stdout.split()
    assert tz == "UTC"
    assert float(offset) == 9 * 3600      # 그래도 now_kst()는 KST


# ──────────────────────────────────────────────────────────
# 3. naive/aware 혼재 흡수
# ──────────────────────────────────────────────────────────

def test_parse_kst_normalizes_naive():
    """구버전이 남긴 naive 문자열도 aware KST로 정규화된다."""
    dt = parse_kst("2026-07-27T09:30:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=9)


def test_parse_kst_normalizes_aware():
    """us_session이 남긴 aware(+09:00) 문자열도 동일 시각으로 정규화된다."""
    naive = parse_kst("2026-07-27T09:30:00")
    aware = parse_kst("2026-07-27T09:30:00+09:00")
    assert naive == aware


def test_parse_kst_converts_other_offsets():
    """다른 오프셋으로 기록됐어도 절대시각을 유지한 채 KST로 변환한다."""
    utc = parse_kst("2026-07-27T00:30:00+00:00")
    assert utc == parse_kst("2026-07-27T09:30:00")


def test_mixed_timestamps_subtract_without_typeerror():
    """혼재 상태에서 now_kst()와 빼도 TypeError가 나지 않는다.

    positions.json은 KR(구 naive) / US(구 aware) 기록이 섞여 있었고, 토큰 캐시는
    artifact로 run 간에 넘어온다. 정규화 없이 빼면 바로 터진다.
    """
    now = now_kst()
    for stored in ["2026-07-27T09:30:00", "2026-07-27T09:30:00+09:00"]:
        delta = now - parse_kst(stored)
        assert isinstance(delta, timedelta)

    # 정규화를 빼먹으면 실제로 터진다는 것도 같이 고정
    with pytest.raises(TypeError):
        _ = now - datetime.fromisoformat("2026-07-27T09:30:00")


def test_kst_stamp_format_is_stable():
    """저장 포맷은 offset 없는 naive 표기를 유지한다.

    trades.csv·positions.json의 기존 기록과 외부 저널 소비자가 이 포맷에 의존한다.
    """
    stamp = kst_stamp(datetime(2026, 7, 27, 9, 30, 5, tzinfo=KST))
    assert stamp == "2026-07-27T09:30:05"
    assert "+" not in stamp
    # 날짜 prefix 매칭(당일 손익 집계 등)이 계속 동작해야 한다
    assert stamp.startswith("2026-07-27")


def test_kst_stamp_roundtrips_through_parse():
    stamp = kst_stamp()
    assert abs((parse_kst(stamp) - now_kst()).total_seconds()) < 5


def test_token_cache_accepts_legacy_naive():
    """구버전 캐시(naive)를 읽어도 is_valid 비교에서 터지지 않는다."""
    from src.kis_auth import TokenBundle

    legacy = {
        "access_token": "dummy",
        "expires_at": (now_kst() + timedelta(hours=5)).replace(tzinfo=None).isoformat(),
        "mode": "live",
    }
    bundle = TokenBundle.from_dict(legacy)
    assert bundle.is_valid is True
    assert TokenBundle.from_dict(bundle.to_dict()).is_valid is True


def test_no_naive_datetime_now_in_src():
    """src/ 전역에 naive datetime.now()가 다시 들어오지 않게 고정.

    now_kst()/today_kst()를 쓰거나, 명시적으로 tz를 넘겨야 한다.
    """
    import io
    import tokenize

    offenders = []
    for py in Path("src").rglob("*.py"):
        src_text = py.read_text(encoding="utf-8")
        # 주석·문자열(독스트링 포함)은 제외 — 설명문의 datetime.now() 언급을 잡지 않도록
        code_lines: dict[int, str] = {}
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src_text).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                code_lines.setdefault(tok.start[0], tok.line.rstrip())
        except tokenize.TokenError:
            continue
        for lineno, line in code_lines.items():
            if re.search(r"(?<![\w.])datetime\.now\(\s*\)", line):
                offenders.append(f"{py}:{lineno}: {line.strip()}")
    assert not offenders, (
        "naive datetime.now() 발견 — src.utils.clock의 now_kst()를 쓸 것:\n"
        + "\n".join(offenders)
    )
