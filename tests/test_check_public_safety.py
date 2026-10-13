"""공개 저장소 가드(scripts/check_public_safety.py) 회귀 테스트.

가짜 비밀값은 문자열 연결로 만든다 — 이 파일 자체가 가드에 걸리지 않도록.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from scripts.check_public_safety import (
    Finding,
    check,
    is_placeholder,
    main,
    mask,
    parse_added_lines,
    path_violation,
    path_warning,
    report,
    scan_line,
)

FAKE_KIS_KEY = "PS" + "a1B2c3D4e5" * 3 + "f6G7"           # PS + 34자
FAKE_GHP = "ghp" + "_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Jh1Gf0Ed9Cb8"
FAKE_PAT = "github" + "_pat_" + "11ABCDEFG0" * 3 + "_" + "qWeRtY1234" * 6
FAKE_DISCORD = "https://discord.com/api/" + "webhooks/1234567890123/" + "aB3_dE6-gH9" * 6
FAKE_TELEGRAM = "123456789" + ":" + "AAH" + "x7Y8z9W0v1" * 3 + "Qr"   # 9자리:35자
FAKE_ACCOUNT = "1357" + "2468" + "-01"


# ── 경로 규칙 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", [
    ".env",
    "configs/.env.local",
    ".env.production",
    "logs/trades.json",
    "src/bot/logs/run.json",
    "bot.log",
    "ledger.db",
    "state/cache.sqlite3",
    "x.sqlite-wal",
    "private/plan.md",
    "internal/memo.txt",
    "research_notes/2026-10.md",
    "reports/backtest.csv",
    "data/raw.parquet",
    "trades.csv",
    "journal/state/positions.json",
    "positions.csv",
    "secrets/kis.txt",
    "deploy/server.pem",
    ".kis_token_cache.json",
])
def test_forbidden_paths(path):
    assert path_violation(path) is not None


@pytest.mark.parametrize("path", [
    ".env.example",
    "src/strategies/golden_cross.py",
    "scripts/dry_run_report.py",          # 파일명에 report — 디렉터리가 아니면 허용
    "docs/DATA_POLICY.md",
    "tests/test_merge_trades.py",
    "src/database.py",
    "configs/strategy.yaml",
    "state/learning/experience.json",     # 경고 대상이지 금지는 아님
])
def test_allowed_paths(path):
    assert path_violation(path) is None


def test_state_learning_is_warning_only():
    assert path_warning("state/learning/experience.json") is not None
    assert path_warning("src/state.py") is None
    findings = check(["state/learning/lgbm_model.pkl"], [])
    assert [f.level for f in findings] == ["warning"]


# ── 비밀값 규칙 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("line, kind", [
    (f'KIS_APPKEY="{FAKE_KIS_KEY}"', "KIS 앱키"),
    (f"token = '{FAKE_GHP}'", "GitHub 토큰"),
    (f"Authorization: Bearer {FAKE_PAT}", "GitHub 토큰"),
    (f"DISCORD_WEBHOOK_URL={FAKE_DISCORD}", "Discord 웹훅"),
    (f"TELEGRAM_BOT_TOKEN={FAKE_TELEGRAM}", "Telegram 봇 토큰"),
    (f'print("잔고 조회 ({FAKE_ACCOUNT})")', "계좌번호"),
    ("KIS_ACCOUNT_NO=" + "13572468", "계좌번호"),
    ('{"CANO": "' + "13572468" + '"}', "계좌번호"),
])
def test_secrets_detected(line, kind):
    hits = scan_line(line)
    assert hits and hits[0].startswith(kind)


@pytest.mark.parametrize("line", [
    "KIS_APPKEY=your_appkey_here",
    "KIS_APPKEY=PS" + "x" * 34,                      # 자리표시
    "- 계좌번호는 `12345678-01` 형식 (8자리 + 상품코드 2자리)",
    "KIS_ACCOUNT_NO=" + "0" * 8,
    "run_id = 20260513-01",                          # 날짜 일련번호
    "uuid 12345678-1234-5678-1234-567812345678",
    "`github_pat_xxxxxxxxxxxx...` 토큰을 복사",
    "Authorization: Bearer github_pat_여기에_토큰_붙여넣기",
    "DISCORD_WEBHOOK_URL=",
    "phone 010-1234-5678",
    "price = 71500  # 삼성전자",
])
def test_non_secrets_pass(line):
    assert scan_line(line) == []


def test_allow_marker_suppresses():
    assert scan_line(f"FIXTURE = '{FAKE_ACCOUNT}'  # public-safety: allow") == []


def test_placeholder_rules():
    assert is_placeholder("12345678", account=True)
    assert is_placeholder("99999999", account=True)
    assert is_placeholder("20261013", account=True)
    assert not is_placeholder("13572468", account=True)
    assert is_placeholder("x" * 34, account=False)
    assert not is_placeholder("a1B2c3D4e5" * 3, account=False)


def test_report_never_prints_secret(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    findings = check([], [("x.py", 3, f"k = '{FAKE_KIS_KEY}'"), ("y.py", 1, FAKE_ACCOUNT)])
    buf = io.StringIO()
    report(findings, out=buf)
    text = buf.getvalue()
    assert "::error file=x.py,line=3::" in text
    assert FAKE_KIS_KEY not in text
    assert FAKE_KIS_KEY[4:20] not in text
    assert "13572468" not in text


def test_mask_keeps_only_prefix():
    assert mask(FAKE_ACCOUNT) == "1357*******"
    assert mask("ab") == "ab****"


# ── diff 파싱 ──────────────────────────────────────────────────────────────
def test_parse_added_lines_tracks_paths_and_numbers():
    diff = "\n".join([
        "diff --git a/src/a.py b/src/a.py",
        "index 111..222 100644",
        "--- a/src/a.py",
        "+++ b/src/a.py",
        "@@ -3,0 +4,2 @@ def f():",
        "+first",
        "++++ looks like a header but is content",
        "@@ -10 +12 @@",
        "-old",
        "+replaced",
        "diff --git a/my file.txt b/my file.txt",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/my file.txt\t",
        "@@ -0,0 +1 @@",
        "+hello",
        "diff --git a/gone.txt b/gone.txt",
        "deleted file mode 100644",
        "--- a/gone.txt",
        "+++ /dev/null",
        "@@ -1 +0,0 @@",
        "-bye",
        "diff --git a/img.png b/img.png",
        "Binary files a/img.png and b/img.png differ",
    ])
    assert list(parse_added_lines(diff)) == [
        ("src/a.py", 4, "first"),
        ("src/a.py", 5, "+++ looks like a header but is content"),
        ("src/a.py", 12, "replaced"),
        ("my file.txt", 1, "hello"),
    ]


# ── git 연동 (임시 저장소) ──────────────────────────────────────────────────
def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / "trades.csv").write_text("legacy\n", encoding="utf-8")  # 과거에 들어간 파일
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_staged_clean_change_passes(repo, capsys):
    (repo / "README.md").write_text("# demo\n12345678-01 형식\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    assert main(["--staged"], cwd=repo) == 0


def test_staged_env_file_fails(repo, capsys):
    (repo / ".env").write_text("MODE=paper\n", encoding="utf-8")
    _git(repo, "add", "-f", ".env")
    assert main(["--staged"], cwd=repo) == 1
    assert ".env" in capsys.readouterr().out


def test_staged_secret_line_fails_with_line_number(repo, capsys):
    (repo / "README.md").write_text(f"# demo\nkey: {FAKE_KIS_KEY}\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    assert main(["--staged"], cwd=repo) == 1
    out = capsys.readouterr().out
    assert "README.md:2" in out and FAKE_KIS_KEY not in out


def test_staged_learning_state_only_warns(repo, capsys):
    (repo / "state" / "learning").mkdir(parents=True)
    (repo / "state" / "learning" / "experience.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "state")
    assert main(["--staged"], cwd=repo) == 0
    assert "WARN" in capsys.readouterr().out


def test_deleting_forbidden_file_passes(repo):
    _git(repo, "rm", "-q", "trades.csv")
    assert main(["--staged"], cwd=repo) == 0


def test_untouched_legacy_file_is_not_flagged(repo):
    (repo / "notes.md").write_text("ok\n", encoding="utf-8")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-q", "-m", "notes")
    assert main(["--base", "HEAD~1"], cwd=repo) == 0


def test_base_range_catches_committed_secret(repo, capsys):
    (repo / "bot.py").write_text(f'ACCT = "{FAKE_ACCOUNT}"\n', encoding="utf-8")
    _git(repo, "add", "bot.py")
    _git(repo, "commit", "-q", "-m", "oops")
    assert main(["--base", "HEAD~1"], cwd=repo) == 1
    assert "bot.py:1" in capsys.readouterr().out


def test_all_mode_audits_tracked_files(repo):
    assert main(["--all"], cwd=repo) == 1   # 과거 trades.csv가 남아 있음


def test_unknown_base_is_usage_error(repo):
    assert main(["--base", "does-not-exist"], cwd=repo) == 2


def test_finding_is_value_object():
    assert Finding("error", "a", 1, "m") == Finding("error", "a", 1, "m")
