"""공개 저장소 안전 가드 — 내부 데이터·비밀값이 다시 공개되는 것을 막는다.

이 저장소는 공개다. 커밋은 영구 기록이고 푸시 즉시 누구나 클론할 수 있다 (docs/DATA_POLICY.md).

검사 (표준 라이브러리만 사용):
  1. 금지 경로 — 추가·변경된 파일이 .env, DB, 로그, 비공개 디렉터리, 실거래 기록이면 실패.
     state/learning/ 변경은 경고만 (봇 운영 중지 상태 — DATA_POLICY.md TODO 참고).
  2. 비밀값 패턴 — 추가된 줄에 KIS 앱키, GitHub 토큰, Discord 웹훅, Telegram 봇 토큰,
     실계좌번호 형태가 있으면 실패. 출력에는 값을 마스킹해서만 보여 준다 (CI 로그도 공개).

사용:
  python scripts/check_public_safety.py --staged         # pre-commit 훅 (.githooks/pre-commit)
  python scripts/check_public_safety.py --base <ref>     # CI: <ref>...HEAD 변경분
  python scripts/check_public_safety.py --all            # 추적 중인 전체 파일 감사
  python scripts/check_public_safety.py FILE [FILE ...]  # 지정 파일 전체 검사

오탐이면 그 줄에 `public-safety: allow` 주석을 달아 예외 처리한다 (리뷰에서 사유 확인).
종료 코드: 0 통과(경고는 있을 수 있음) / 1 위반 / 2 사용법·git 오류.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

ALLOW_MARKER = "public-safety: allow"

# ── 1. 경로 규칙 ────────────────────────────────────────────────────────────
# 경로의 어느 단계에든 이 디렉터리가 있으면 금지
FORBIDDEN_DIRS = {
    "logs": "로그 디렉터리",
    "data": "데이터 디렉터리",
    "private": "비공개 디렉터리",
    "internal": "내부 디렉터리",
    "research_notes": "연구 노트",
    "reports": "리포트 산출물",
    "secrets": "비밀값 디렉터리",
    "tokens": "토큰 디렉터리",
    "token_cache": "토큰 캐시",
}
FORBIDDEN_NAMES = {
    "trades.csv": "실거래 기록",
    "positions.json": "실포지션",
    "positions.csv": "실포지션",
    ".kis_token_cache.json": "KIS 토큰 캐시",
}
DB_RE = re.compile(r"\.(?:db|sqlite\d*)(?:-(?:journal|wal|shm))?$", re.IGNORECASE)
KEY_FILE_RE = re.compile(r"\.(?:pem|key)$", re.IGNORECASE)
WARN_PREFIXES = {
    "state/learning/": "학습 상태 — 봇 재가동 전에 비공개 저장소로 옮길 대상 (docs/DATA_POLICY.md TODO)",
}


def path_violation(path: str) -> str | None:
    """금지 경로면 사유, 아니면 None. path는 저장소 루트 기준 POSIX 경로."""
    p = PurePosixPath(path)
    name = p.name
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "환경변수 파일(.env) — 비밀값"
    if DB_RE.search(name):
        return "DB 파일"
    if name.endswith(".log"):
        return "로그 파일"
    if KEY_FILE_RE.search(name):
        return "키 파일"
    if name in FORBIDDEN_NAMES:
        return FORBIDDEN_NAMES[name]
    for part in p.parts[:-1]:
        if part in FORBIDDEN_DIRS:
            return f"{FORBIDDEN_DIRS[part]}({part}/)"
    return None


def path_warning(path: str) -> str | None:
    for prefix, reason in WARN_PREFIXES.items():
        if path.startswith(prefix):
            return reason
    return None


# ── 2. 비밀값 규칙 ──────────────────────────────────────────────────────────
# 그룹 1 = 비밀 부분. 플레이스홀더 판정과 마스킹에 쓴다.
@dataclass(frozen=True)
class SecretRule:
    name: str
    regex: re.Pattern[str]
    account: bool = False


SECRET_RULES = (
    SecretRule("KIS 앱키", re.compile(r"(?<![A-Za-z0-9])PS([A-Za-z0-9]{34})(?![A-Za-z0-9])")),
    SecretRule("GitHub 토큰", re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_([A-Za-z0-9]{30,})")),
    SecretRule("GitHub 토큰", re.compile(r"(?<![A-Za-z0-9_])github_pat_([A-Za-z0-9_]{40,})")),
    SecretRule(
        "Discord 웹훅",
        re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/([A-Za-z0-9_-]{20,})", re.IGNORECASE),
    ),
    SecretRule(
        "Telegram 봇 토큰",
        re.compile(r"(?<![0-9])[0-9]{8,10}:([A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])"),
    ),
    # KIS 계좌: 8자리 + 상품코드 2자리
    SecretRule("계좌번호", re.compile(r"(?<![0-9-])([0-9]{8})-[0-9]{2}(?![0-9-])"), account=True),
    SecretRule(
        "계좌번호",
        re.compile(
            r"(?:ACCOUNT_NO|ACCT_NO|CANO)[\"']?\s*[=:]\s*[\"']?([0-9]{8})(?![0-9])", re.IGNORECASE
        ),
        account=True,
    ),
)

# 문서·예시에 쓰는 자리표시 계좌번호
PLACEHOLDER_ACCOUNTS = {"12345678", "87654321", "01234567", "23456789"}


def _looks_like_date(digits: str) -> bool:
    """YYYYMMDD 날짜(예: 20260513-01 같은 일련번호)는 계좌로 보지 않는다."""
    try:
        y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:])
        return 1900 <= y <= 2100 and bool(date(y, m, d))
    except ValueError:
        return False


def is_placeholder(secret: str, account: bool) -> bool:
    if account:
        return len(set(secret)) == 1 or secret in PLACEHOLDER_ACCOUNTS or _looks_like_date(secret)
    # xxxx…, 0000… 같은 자리표시. 실제 토큰은 문자 종류가 훨씬 많다.
    return len(set(secret.lower())) < 4


def mask(value: str) -> str:
    """앞 4자만 남긴다 — 위반 보고가 공개 CI 로그로 비밀값을 다시 퍼뜨리지 않게."""
    return value[:4] + "*" * max(len(value) - 4, 4)


def scan_line(text: str) -> list[str]:
    """한 줄에서 찾은 비밀값을 '종류: 마스킹값' 목록으로."""
    if ALLOW_MARKER in text:
        return []
    hits = []
    for rule in SECRET_RULES:
        for m in rule.regex.finditer(text):
            if not is_placeholder(m.group(1), rule.account):
                hits.append(f"{rule.name}: {mask(m.group(0))}")
    return hits


# ── 입력 수집 ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    path: str
    line: int | None
    message: str


def _git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args], cwd=cwd, capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"git {args} 실패")
    return proc.stdout.decode("utf-8", "replace")


def _diff_range(staged: bool, base: str | None) -> list[str]:
    return ["--cached"] if staged else [f"{base}...HEAD"]


def changed_paths(staged: bool, base: str | None, cwd: Path | None = None) -> list[str]:
    """추가·복사·수정·이름변경된 파일. 삭제는 검사 대상이 아니다(지우는 건 좋은 일)."""
    out = _git(
        ["diff", "--name-only", "-z", "--no-renames", "--diff-filter=ACM",
         *_diff_range(staged, base)],
        cwd,
    )
    return [p for p in out.split("\0") if p]


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_added_lines(diff_text: str) -> Iterator[tuple[str, int, str]]:
    """`git diff -U0` 출력에서 (경로, 새 줄번호, 추가된 줄)을 뽑는다."""
    path: str | None = None
    lineno = 0
    in_header = False
    for raw in diff_text.split("\n"):
        if raw.startswith("diff --git "):
            in_header, path = True, None
        elif in_header and raw.startswith("+++ "):
            target = raw[4:].rstrip("\t").strip('"')  # 공백 포함 경로는 끝에 TAB
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif raw.startswith("@@"):
            in_header = False
            m = HUNK_RE.match(raw)
            lineno = int(m.group(1)) if m else 0
        elif not in_header and path is not None and raw.startswith("+"):
            yield path, lineno, raw[1:]
            lineno += 1


def added_lines(staged: bool, base: str | None, cwd: Path | None = None) -> Iterator[tuple[str, int, str]]:
    out = _git(
        ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--diff-filter=ACM",
         *_diff_range(staged, base)],
        cwd,
    )
    return parse_added_lines(out)


def file_lines(paths: Iterable[str], root: Path) -> Iterator[tuple[str, int, str]]:
    """파일 전체 줄. 바이너리(앞부분에 NUL)·읽기 실패는 건너뛴다."""
    for rel in paths:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8000]:
            continue
        for i, line in enumerate(data.decode("utf-8", "replace").split("\n"), start=1):
            yield rel, i, line


# ── 판정 ────────────────────────────────────────────────────────────────────
def check(paths: Iterable[str], lines: Iterable[tuple[str, int, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if reason := path_violation(path):
            findings.append(Finding("error", path, None, f"금지 경로 — {reason}"))
        elif reason := path_warning(path):
            findings.append(Finding("warning", path, None, reason))
    for path, lineno, text in lines:
        for hit in scan_line(text):
            findings.append(Finding("error", path, lineno, f"비밀값 의심 — {hit}"))
    return findings


def _escape_annotation(value: str, prop: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def report(findings: list[Finding], out=None) -> None:
    out = out or sys.stdout
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for f in findings:
        where = f.path if f.line is None else f"{f.path}:{f.line}"
        print(f"{'ERROR' if f.level == 'error' else 'WARN '} {where}: {f.message}", file=out)
        if in_actions:
            props = f"file={_escape_annotation(f.path, prop=True)}"
            if f.line is not None:
                props += f",line={f.line}"
            print(f"::{f.level} {props}::{_escape_annotation(f.message)}", file=out)


def main(argv: list[str] | None = None, cwd: Path | None = None) -> int:
    ap = argparse.ArgumentParser(description="공개 저장소 안전 가드 (docs/DATA_POLICY.md)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="커밋 대기(index) 변경분 검사")
    mode.add_argument("--base", metavar="REF", help="REF...HEAD 변경분 검사 (CI)")
    mode.add_argument("--all", action="store_true", help="추적 중인 전체 파일 검사")
    ap.add_argument("files", nargs="*", help="지정 파일 전체 검사")
    args = ap.parse_args(argv)

    root = Path(cwd or Path.cwd())
    try:
        if args.staged or args.base:
            paths = changed_paths(args.staged, args.base, root)
            lines: Iterable[tuple[str, int, str]] = added_lines(args.staged, args.base, root)
        elif args.all:
            paths = [p for p in _git(["ls-files", "-z"], root).split("\0") if p]
            lines = file_lines(paths, root)
        elif args.files:
            paths = [Path(p).as_posix() for p in args.files]
            lines = file_lines(paths, root)
        else:
            ap.error("--staged, --base REF, --all, FILE 중 하나가 필요하다")
        findings = check(paths, lines)
    except RuntimeError as e:
        print(f"git 오류: {e}", file=sys.stderr)
        return 2

    report(findings)
    errors = sum(f.level == "error" for f in findings)
    warnings = len(findings) - errors
    if errors:
        print(
            f"\n공개 저장소 가드 실패: 위반 {errors}건, 경고 {warnings}건.\n"
            "  - 파일은 커밋에서 빼고(git rm --cached <파일>) .gitignore에 추가한다.\n"
            "  - 비밀값이 이미 푸시됐다면 이력 삭제보다 먼저 키를 폐기·재발급한다.\n"
            "  - 오탐이면 그 줄에 'public-safety: allow' 주석을 단다.\n"
            "  자세한 기준: docs/DATA_POLICY.md"
        )
        return 1
    print(f"공개 저장소 가드 통과 (경고 {warnings}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
