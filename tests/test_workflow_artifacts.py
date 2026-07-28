"""워크플로 artifact 설정 회귀 테스트.

토큰 캐시(`logs/.kis_token_cache.json`)는 **닷파일**이라
`actions/upload-artifact@v4`가 기본으로 제외한다. 그래서 다운로드 스텝은
멀쩡한데 업로드가 매번 빈손("No files were found")이 되어, 캐시가 run 간에
전혀 보존되지 않고 **봇 실행마다 KIS 토큰을 재발급**받고 있었다.

실제 운영 로그에서 확인된 증상:
    ##[warning]No files were found with the provided path: logs/.kis_token_cache.json

kis_auth.py의 원자적 쓰기·파일락·race 폴백은 전부 run **안**에서만 의미가 있고,
run **간** 재사용은 이 artifact에 의존한다. 조용히 실패하는 종류라 테스트로 고정한다.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(".github/workflows")


def _workflows():
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def _upload_steps(wf: Path):
    """(스텝 이름, with 딕셔너리) 목록 — upload-artifact 스텝만."""
    data = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
    out = []
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if "upload-artifact" in str(step.get("uses", "")):
                out.append((step.get("name", "?"), step.get("with") or {}))
    return out


def _has_hidden_path(path_value: str) -> bool:
    """업로드 경로에 닷파일이 하나라도 있는지 (path는 여러 줄일 수 있다)."""
    for line in str(path_value).splitlines():
        line = line.strip()
        if line and Path(line).name.startswith("."):
            return True
    return False


@pytest.mark.parametrize("wf", _workflows(), ids=lambda p: p.name)
def test_hidden_file_uploads_opt_in(wf):
    """닷파일을 올리는 upload-artifact 스텝은 include-hidden-files: true여야 한다."""
    offenders = []
    for name, cfg in _upload_steps(wf):
        if _has_hidden_path(cfg.get("path", "")) and cfg.get("include-hidden-files") is not True:
            offenders.append(f"{name} (path={cfg.get('path')!r})")
    assert not offenders, (
        f"{wf.name}: 닷파일 업로드에 include-hidden-files: true 누락 → "
        f"업로드가 조용히 빈손이 된다:\n  " + "\n  ".join(offenders)
    )


def test_token_cache_is_actually_uploaded():
    """토큰 캐시를 다운로드하는 워크플로는 업로드도 제대로 해야 한다.

    다운로드만 있고 업로드가 (조용히) 실패하면 캐시가 영원히 비어 있어
    매 run 토큰을 재발급받는다 — KIS '1분 내 재발급 금지'에 걸릴 여지가 있다.
    """
    from src.kis_auth import TOKEN_CACHE_PATH

    cache_name = TOKEN_CACHE_PATH.name
    assert cache_name.startswith("."), (
        "토큰 캐시가 더 이상 닷파일이 아니다 — 이 테스트의 전제를 재확인할 것"
    )

    checked = 0
    for wf in _workflows():
        text = wf.read_text(encoding="utf-8")
        if cache_name not in text:
            continue
        uploads = [(n, c) for n, c in _upload_steps(wf)
                   if cache_name in str(c.get("path", ""))]
        if not uploads:
            continue
        checked += 1
        for name, cfg in uploads:
            assert cfg.get("include-hidden-files") is True, (
                f"{wf.name}의 '{name}' 스텝이 토큰 캐시를 올리지 못한다"
            )
    assert checked >= 1, "토큰 캐시를 업로드하는 워크플로를 찾지 못했다"
