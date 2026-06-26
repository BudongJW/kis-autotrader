"""사용자 수동 설정(user_overrides.yaml)을 strategy.yaml 위에 deep-merge.

배경: 자동 워크플로(market-learn / optimize)가 strategy.yaml 전체를 재작성하며
사용자가 수동 추가한 universe.default(반도체 주도주 등)를 클로버하던 문제
(2026-06: 삼성·하이닉스·LGD·삼중·HMM이 '주간 파라미터 최적화' 자동커밋에 의해 삭제).

해결: user_overrides.yaml은 자동화가 절대 쓰지 않고, 모든 load_config가 strategy.yaml
로드 직후 이 파일을 deep-merge로 재적용한다. → 자동화가 strategy.yaml의 universe를
지워도 매 로드마다 사용자 설정이 복원되어 영구 보존된다(단일 진실원천=user_overrides).

병합 규칙: dict는 재귀 병합, **list·스칼라는 override가 교체**(universe.default 리스트는
사용자 값으로 완전 대체. universe의 다른 하위섹션(inverse/defensive/income/canary)은
override에 없으면 strategy.yaml 값 유지).
"""
from __future__ import annotations

from pathlib import Path

import yaml

USER_OVERRIDES_PATH = Path("configs/user_overrides.yaml")


def deep_merge(base: dict, override: dict) -> dict:
    """override를 base 위에 깊은 병합. dict는 재귀, list·스칼라는 교체."""
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_user_overrides(cfg: dict) -> dict:
    """strategy.yaml에서 로드한 cfg에 user_overrides.yaml을 병합해 반환.

    파일이 없거나 파싱 실패면 cfg를 그대로 반환(안전).
    """
    if not USER_OVERRIDES_PATH.exists():
        return cfg
    try:
        with USER_OVERRIDES_PATH.open(encoding="utf-8") as f:
            ov = yaml.safe_load(f) or {}
        return deep_merge(cfg or {}, ov)
    except Exception:
        return cfg
