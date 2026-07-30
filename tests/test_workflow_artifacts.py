"""워크플로 artifact 설정 회귀 테스트.

## 토큰 캐시를 artifact로 올리지 않는다 (2026-07-31)

원래 이 파일은 "토큰 캐시 업로드가 조용히 실패한다"를 고정한 테스트였다.
`logs/.kis_token_cache.json`이 **닷파일**이라 `upload-artifact@v4`가 기본으로
제외했고, 업로드가 매번 빈손이었다:

    ##[warning]No files were found with the provided path: logs/.kis_token_cache.json

`include-hidden-files: true`로 업로드는 고쳤는데(510 bytes 업로드 확인),
그 다음 두 가지가 드러났다.

1. **다운로드가 어차피 안 된다.** `download-artifact@v4`는 artifact를 **run
   단위로 스코프**해서, `run-id` 없이 이름만 주면 현재 run이 만든 것만 찾는다.
   실제 운영 로그에서 토큰 캐시뿐 아니라 7개 artifact 전부가 동일하게 실패했다:

       ##[error]Unable to download artifact(s): Artifact not found for name: kis-token-cache

2. **이 레포는 public이다.** artifact는 레포 읽기 권한자면 누구나 내려받을 수
   있으므로, 살아 있는 KIS 액세스 토큰(주문 권한, 24시간 유효)을 공개된 곳에
   올리고 있던 셈이다. 1번을 "고쳐서" run 간 재사용을 살리는 방향은 이 노출을
   더 길게 유지할 뿐이다.

그래서 토큰 캐시 artifact는 **업로드·다운로드 모두 제거**했다. run 안에서의
디스크 캐시(원자적 쓰기·파일락)는 그대로 두고, run마다 새로 발급받되
`kis_auth._is_reissue_throttled()`가 "1분당 1회" 제한을 잡아 재시도한다.

이 파일은 이제 그 결정을 되돌리지 못하게 고정한다.
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


def _download_steps(wf: Path):
    data = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
    out = []
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if "download-artifact" in str(step.get("uses", "")):
                out.append((step.get("name", "?"), step.get("with") or {}))
    return out


def test_token_cache_is_never_uploaded_as_artifact():
    """살아 있는 KIS 토큰을 public 레포 artifact에 올리면 안 된다.

    artifact는 레포 읽기 권한자면 누구나 내려받는다. 이 레포는 public이므로
    (워크플로 주석: "public repo라 분 한도 무제한") 업로드하는 순간 주문 권한이
    붙은 24시간짜리 자격증명이 공개된다.
    """
    from src.kis_auth import TOKEN_CACHE_PATH

    cache_name = TOKEN_CACHE_PATH.name
    offenders = []
    for wf in _workflows():
        for name, cfg in _upload_steps(wf):
            if cache_name in str(cfg.get("path", "")):
                offenders.append(f"{wf.name}: {name}")
    assert not offenders, (
        "토큰 캐시가 artifact로 업로드된다 — public 레포에 자격증명 노출:\n  "
        + "\n  ".join(offenders)
    )


def test_no_workflow_downloads_the_token_cache_artifact():
    """업로드를 지웠으면 다운로드도 지워야 한다.

    남겨두면 매 run "Artifact not found" 에러가 찍혀, 진짜 문제를 덮는 소음이 된다.
    (`download-artifact@v4`는 run 단위 스코프라 애초에 이전 run 것을 못 본다.)
    """
    offenders = []
    for wf in _workflows():
        for name, cfg in _download_steps(wf):
            if "kis-token-cache" in str(cfg.get("name", "")):
                offenders.append(f"{wf.name}: {name}")
    assert not offenders, (
        "토큰 캐시 artifact 다운로드 스텝이 남아 있다:\n  " + "\n  ".join(offenders)
    )
