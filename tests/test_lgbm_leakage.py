"""LGBM 학습 누수 회귀 테스트 (2026-08-03).

배경: 학습 파이프라인이 정확도 86%·AUC 0.95를 보고했으나 실거래는 승률 58%·손익
-620원이었다. 원인은 예측력이 아니라 **평가 설계 결함** 3가지였다.
  1) 종목별 데이터를 세로 concat 후 행 인덱스로 80% 분할 → 테스트셋이 사실상
     '마지막 종목'이고 학습셋에 같은 날짜의 다른 종목이 들어감. 069500과 114800은
     서로 역방향 ETF라 라벨을 그대로 알려주는 누수.
  2) 테스트셋으로 early stopping 한 뒤 같은 셋에 정확도 보고 → 낙관 편향.
  3) warm-start + 99% 겹치는 롤링 윈도 → 어제 학습한 행이 오늘 테스트셋.
이 테스트들이 재발을 막는다.
"""
from __future__ import annotations

import inspect
import re

import pandas as pd

import src.strategies.lgbm_predictor as lp


def _src() -> str:
    return inspect.getsource(lp.daily_retrain)


def test_no_row_index_split():
    """행 인덱스 비율 분할 금지 — 날짜 기준으로 잘라야 한다."""
    s = _src()
    assert "int(len(X) * 0.8)" not in s, "행 인덱스 80% 분할은 종목 혼합 누수를 만든다"
    assert "dates" in s and "uniq" in s, "날짜 단위 분할이어야 한다"


def test_has_embargo_between_splits():
    """분할 경계에 공백일(embargo)이 있어야 인접일 상관 누수가 끊긴다."""
    assert lp.EMBARGO_DAYS >= 1
    assert "embargo" in _src()


def test_early_stopping_not_on_test_set():
    """early stopping은 검증셋으로. 테스트셋으로 조기종료 후 같은 셋 보고 = 편향."""
    s = _src()
    assert re.search(r"valid_data\s*=\s*lgb\.Dataset\(\s*X_valid", s), \
        "early stopping용 Dataset은 X_valid여야 한다"
    # 보고 지표는 테스트셋에서
    assert "predictor.model.predict(X_test)" in s


def test_no_warm_start():
    """warm-start + 겹치는 윈도 = 이미 본 데이터를 평가. 매일 새로 학습한다."""
    s = _src()
    assert "init_model=init_model" not in s, "warm-start는 평가 누수를 만든다"


def test_mode_label_updated():
    """저장되는 mode 라벨이 옛 누수 버전(daily_warm_start)이면 안 된다."""
    s = _src()
    assert '"daily_warm_start"' not in s


# ── 전방검증(실측) 기록 ──────────────────────────────────────────────
def test_live_accuracy_grades_predictions(tmp_path, monkeypatch):
    """예측을 적어두고 다음날 종가로 채점하는지 — 이게 유일한 진짜 성적표."""
    monkeypatch.setattr(lp, "LIVE_ACCURACY_PATH", tmp_path / "live.json")
    idx = pd.to_datetime(["2026-08-01", "2026-08-02"])
    hist1 = pd.DataFrame({"close": [100.0, 101.0]}, index=idx)
    # 08-02 시점에 '오를 것(0.8)'이라 예측 기록
    lp.record_live_prediction("069500", 0.8, hist1)

    import json
    d = json.loads((tmp_path / "live.json").read_text(encoding="utf-8"))
    assert d["records"][-1]["realized"] is None      # 아직 미채점

    # 다음날 종가가 올라간 이력이 들어오면 채점된다
    idx2 = pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-03"])
    hist2 = pd.DataFrame({"close": [100.0, 101.0, 103.0]}, index=idx2)
    lp.record_live_prediction("069500", 0.6, hist2)
    d2 = json.loads((tmp_path / "live.json").read_text(encoding="utf-8"))
    graded = [r for r in d2["records"] if r.get("hit") is not None]
    assert graded and graded[0]["hit"] == 1          # 상승 예측 + 실제 상승 = 적중
    assert d2["summary"]["hit_rate"] == 1.0


def test_live_accuracy_marks_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "LIVE_ACCURACY_PATH", tmp_path / "live2.json")
    idx = pd.to_datetime(["2026-08-01", "2026-08-02"])
    lp.record_live_prediction("069500", 0.9, pd.DataFrame({"close": [100.0, 101.0]}, index=idx))
    idx2 = pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-03"])
    # 다음날 하락 → 상승 예측은 빗나감
    lp.record_live_prediction("069500", 0.5,
                              pd.DataFrame({"close": [100.0, 101.0, 99.0]}, index=idx2))
    import json
    d = json.loads((tmp_path / "live2.json").read_text(encoding="utf-8"))
    graded = [r for r in d["records"] if r.get("hit") is not None]
    assert graded[0]["hit"] == 0
    assert d["summary"]["hit_rate"] == 0.0


# ── 실측 적중률 게이트: 동전던지기면 예측을 매매에 안 쓴다 ──
def test_filter_disabled_when_live_accuracy_is_coinflip(tmp_path, monkeypatch):
    import json
    p = tmp_path / "live3.json"
    p.write_text(json.dumps({"records": [],
                             "summary": {"graded": 40, "hit_rate": 0.48}}), encoding="utf-8")
    monkeypatch.setattr(lp, "LIVE_ACCURACY_PATH", p)
    assert lp.live_hit_rate() == 0.48

    class _M:  # 모델이 있는 것처럼
        pass
    monkeypatch.setattr(lp.LGBMPredictor, "__init__", lambda self: None)
    monkeypatch.setattr(lp.LGBMPredictor, "model", _M(), raising=False)
    out = lp.get_prediction_filter(None, "069500")
    assert out["up_prob"] == 0.5 and "미반영" in out["reason"]


def test_live_hit_rate_holds_judgement_on_small_sample(tmp_path, monkeypatch):
    import json
    p = tmp_path / "live4.json"
    p.write_text(json.dumps({"records": [], "summary": {"graded": 5, "hit_rate": 0.2}}),
                 encoding="utf-8")
    monkeypatch.setattr(lp, "LIVE_ACCURACY_PATH", p)
    assert lp.live_hit_rate() is None   # 표본 부족 → 판정 보류
