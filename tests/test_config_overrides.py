"""config_overrides 단위 테스트 — 사용자 설정이 strategy.yaml 위에 병합되는지.

자동 워크플로가 universe.default를 지워도 user_overrides가 매 로드 복원함을 검증.
"""
from src.config_overrides import deep_merge, apply_user_overrides


def test_deep_merge_dict_recurse():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    ov = {"a": {"y": 20, "z": 30}}
    out = deep_merge(base, ov)
    assert out == {"a": {"x": 1, "y": 20, "z": 30}, "b": 3}


def test_deep_merge_list_replaces():
    # 리스트는 병합이 아니라 교체 (universe.default 완전 대체)
    base = {"u": {"default": [1, 2, 3], "inverse": [9]}}
    ov = {"u": {"default": [1, 2, 3, 4, 5]}}
    out = deep_merge(base, ov)
    assert out["u"]["default"] == [1, 2, 3, 4, 5]
    assert out["u"]["inverse"] == [9]   # override에 없는 하위섹션은 유지


def test_apply_restores_clobbered_universe():
    # strategy.yaml이 default=[395160]으로 클로버된 상태를 시뮬레이션
    cfg = {"universe": {"default": [{"symbol": "395160"}],
                        "inverse": [{"symbol": "114800"}]}}
    # 실제 user_overrides.yaml을 적용 → 반도체 주도주가 복원돼야 함
    out = apply_user_overrides(cfg)
    syms = {s["symbol"] for s in out["universe"]["default"]}
    assert "005930" in syms   # 삼성전자 복원
    assert "000660" in syms   # SK하이닉스 복원
    assert "395160" in syms   # 기존 유지
    assert out["universe"]["inverse"][0]["symbol"] == "114800"  # 다른 섹션 보존
