"""시세 지연 실측 재시도 + 체결가 괴리 표본 수집.

두 문제 모두 "조용히 실패해서 데이터가 안 남는" 부류다.

1) 시세 지연 실측이 개장 직후 **1회만** 돌았고, `_us_quote()`가 예외를 통째로
   삼켜 사유도 안 남았다. 2026-07-28·29 이틀 연속 무산:

       시세 지연 실측: 판정 불가 (kis_quote_empty)
       {"symbol": "SPLG", "kis_last": 0.0, "error": "kis_quote_empty", ...}

   같은 세션에서 PSQ(동일 AMEX) 시세는 멀쩡했다 — 단일 종목·단발 조회에 전부를
   걸어둔 게 문제였다. `execution.quote_lag_min`을 실측으로 채우려면 측정이
   실제로 성공해야 한다.

2) 매수 슬리피지가 이틀 연속 정확히 -0.96%였다 (기준 $27.45→체결 $27.19,
   $27.70→$27.44). 부호·크기가 반복되는 건 시장 슬리피지가 아니다. 원인을
   추정하지 말고, 갈라낼 데이터(체결 직후 재호가)를 남긴다.
"""

import json

import pytest

import src.bot.us_session as us


class QuoteClient:
    """get_overseas_price 응답을 심볼별로 지정하는 스텁."""

    def __init__(self, responses=None, raises=None):
        self.responses = responses or {}
        self.raises = raises or {}
        self.calls = []

    def get_overseas_price(self, symbol, exchange="NASD"):
        self.calls.append((symbol, exchange))
        if symbol in self.raises:
            raise self.raises[symbol]
        return self.responses.get(
            symbol, {"rt_cd": "0", "output": {"base": "100", "last": "0"}})


def _ok(last, base="100"):
    return {"rt_cd": "0", "output": {"base": base, "last": str(last)}}


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    us.reset_quote_lag_probe()
    monkeypatch.setattr(us, "US_SLIPPAGE_PATH", tmp_path / "us_slippage.json")
    yield
    us.reset_quote_lag_probe()


# ══════════════════════════════════════════════════════════
# _us_quote_err — 실패 사유가 남아야 한다
# ══════════════════════════════════════════════════════════

def test_quote_error_surfaces_rt_cd():
    c = QuoteClient({"SPLG": {"rt_cd": "1", "msg1": "모의투자 미지원"}})
    base, last, err = us._us_quote_err(c, "SPLG", "AMEX")
    assert (base, last) == (0.0, 0.0)
    assert "rt_cd=1" in err and "모의투자 미지원" in err


def test_quote_error_surfaces_exception():
    c = QuoteClient(raises={"SPLG": TimeoutError("read timeout")})
    _, last, err = us._us_quote_err(c, "SPLG", "AMEX")
    assert last == 0.0
    assert "TimeoutError" in err and "read timeout" in err


def test_quote_error_distinguishes_zero_last():
    """rt_cd=0인데 last가 0 — 예전엔 예외와 구분이 안 됐다."""
    c = QuoteClient({"SPLG": _ok(0)})
    _, last, err = us._us_quote_err(c, "SPLG", "AMEX")
    assert last == 0.0 and "last_is_zero" in err


def test_us_quote_keeps_old_signature():
    """기존 호출부는 그대로 (base, last) 2튜플을 받아야 한다."""
    c = QuoteClient({"PSQ": _ok(27.70, base="28.07")})
    assert us._us_quote(c, "PSQ", "AMEX") == (28.07, 27.70)


# ══════════════════════════════════════════════════════════
# 후보 폴백 — 한 종목이 비어도 측정이 죽지 않는다
# ══════════════════════════════════════════════════════════

def test_candidates_include_configured_symbols():
    cands = us.quote_lag_probe_candidates()
    assert cands, "후보가 비었다"
    assert len(cands) == len(set(cands)), "중복 후보"
    assert all(isinstance(s, str) and isinstance(e, str) for s, e in cands)


def test_probe_falls_back_to_next_candidate(monkeypatch):
    """첫 후보가 비면 다음 후보로 넘어가야 한다 — 여기서 포기하면 그 밤이 끝난다."""
    monkeypatch.setattr(us, "quote_lag_probe_candidates",
                        lambda: [("SPLG", "AMEX"), ("PSQ", "AMEX")])
    monkeypatch.setattr(us, "pd", None)     # yf 단계까지 안 가도 됨
    c = QuoteClient({"SPLG": _ok(0), "PSQ": _ok(27.70)})

    lag, detail = us.measure_quote_lag(c)

    assert detail["symbol"] == "PSQ", "첫 후보에서 멈췄다"
    assert detail["kis_last"] == 27.70
    assert ("SPLG", "AMEX") in c.calls and ("PSQ", "AMEX") in c.calls
    assert lag is None      # yfinance가 없으니 결론은 안 나지만 호가는 확보됐다


def test_all_candidates_failing_reports_what_was_tried(monkeypatch):
    monkeypatch.setattr(us, "quote_lag_probe_candidates",
                        lambda: [("SPLG", "AMEX"), ("PSQ", "AMEX")])
    c = QuoteClient({"SPLG": _ok(0), "PSQ": _ok(0)})

    lag, detail = us.measure_quote_lag(c)

    assert lag is None
    assert detail["tried"] == ["SPLG/AMEX", "PSQ/AMEX"], "시도 목록이 안 남았다"
    assert "last_is_zero" in detail["error"], "사유가 kis_quote_empty로 뭉개졌다"


def test_explicit_symbol_skips_candidates(monkeypatch):
    """심볼을 명시하면 그것만 본다 (디버그 스크립트 호환)."""
    c = QuoteClient({"TLT": _ok(0)})
    _, detail = us.measure_quote_lag(c, symbol="TLT", exchange="NASD")
    assert c.calls == [("TLT", "NASD")]
    assert detail["symbol"] == "TLT"


# ══════════════════════════════════════════════════════════
# report_quote_lag — 결론이 날 때까지 재시도
# ══════════════════════════════════════════════════════════

def test_inconclusive_probe_asks_for_retry(monkeypatch):
    """실패하면 False → 호출부가 다음 주기에 다시 부른다."""
    monkeypatch.setattr(us, "measure_quote_lag",
                        lambda *a, **k: (None, {"error": "kis_quote_empty"}))
    assert us.report_quote_lag(QuoteClient()) is False


def test_probe_gives_up_after_max_attempts(monkeypatch):
    """무한 재시도는 안 된다 — 상한에 닿으면 포기하고 True."""
    monkeypatch.setattr(us, "measure_quote_lag",
                        lambda *a, **k: (None, {"error": "kis_quote_empty"}))
    c = QuoteClient()
    results = [us.report_quote_lag(c) for _ in range(us.MAX_QUOTE_LAG_ATTEMPTS)]
    assert results[:-1] == [False] * (us.MAX_QUOTE_LAG_ATTEMPTS - 1)
    assert results[-1] is True, "상한에서 포기하지 않았다"


def test_probe_stops_measuring_after_success(monkeypatch):
    """한 번 성공하면 더 재지 않는다."""
    calls = []

    def _measure(*a, **k):
        calls.append(1)
        return 0.3, {"symbol": "PSQ", "kis_last": 27.7, "ref_last": 27.7,
                     "price_gap_pct": 0.0}

    monkeypatch.setattr(us, "measure_quote_lag", _measure)
    c = QuoteClient()
    assert us.report_quote_lag(c) is True
    assert us.report_quote_lag(c) is True
    assert len(calls) == 1, "성공 후에도 계속 측정했다"


def test_retry_then_success(monkeypatch):
    """2회 실패 후 성공 — 예전 구조라면 여기서 영영 측정이 없었다."""
    seq = [(None, {"error": "kis_quote_empty"}),
           (None, {"error": "rt_cd=1 msg=일시오류"}),
           (16.0, {"symbol": "PSQ", "kis_last": 27.7, "ref_last": 27.9,
                   "price_gap_pct": -0.7})]
    monkeypatch.setattr(us, "measure_quote_lag", lambda *a, **k: seq.pop(0))
    c = QuoteClient()
    assert [us.report_quote_lag(c) for _ in range(3)] == [False, False, True]
    assert seq == []


def test_reset_allows_a_fresh_session(monkeypatch):
    monkeypatch.setattr(us, "measure_quote_lag",
                        lambda *a, **k: (None, {"error": "x"}))
    c = QuoteClient()
    for _ in range(us.MAX_QUOTE_LAG_ATTEMPTS):
        us.report_quote_lag(c)
    us.reset_quote_lag_probe()
    assert us.report_quote_lag(c) is False, "리셋 후에도 포기 상태가 남았다"


# ══════════════════════════════════════════════════════════
# 체결가 괴리 표본
# ══════════════════════════════════════════════════════════

def test_slippage_sample_records_gap_and_requote():
    """기준가·한도·체결가·직후 재호가가 한 표본에 다 남아야 한다."""
    c = QuoteClient({"PSQ": _ok(27.45)})

    s = us.record_fill_slippage(c, "PSQ", "AMEX", "buy",
                                ref_price=27.70, limit_px=27.75, avg_price=27.44)

    assert s["gap_vs_ref_pct"] == pytest.approx(-0.94, abs=0.02)
    assert s["requote"] == 27.45
    assert s["gap_vs_requote_pct"] == pytest.approx(-0.04, abs=0.02)


def test_slippage_sample_survives_requote_failure():
    """재호가가 실패해도 표본 자체는 남아야 한다 (사유와 함께)."""
    c = QuoteClient(raises={"PSQ": RuntimeError("boom")})
    s = us.record_fill_slippage(c, "PSQ", "AMEX", "buy", 27.70, 27.75, 27.44)
    assert "requote" not in s
    assert "boom" in s["requote_error"]
    assert s["gap_vs_ref_pct"] == pytest.approx(-0.94, abs=0.02)


def test_slippage_samples_accumulate_across_calls():
    """run 간 누적이 목적이다 — 덮어쓰면 표본이 하나도 안 모인다."""
    c = QuoteClient({"PSQ": _ok(27.45)})
    us.record_fill_slippage(c, "PSQ", "AMEX", "buy", 27.70, 27.75, 27.44)
    us.record_fill_slippage(c, "PSQ", "AMEX", "buy", 27.45, 27.50, 27.19)

    saved = json.loads(us.US_SLIPPAGE_PATH.read_text(encoding="utf-8"))
    assert len(saved) == 2
    assert [x["avg_price"] for x in saved] == [27.44, 27.19]


def test_slippage_samples_are_capped(monkeypatch):
    """무한정 커지면 artifact가 부담된다."""
    monkeypatch.setattr(us, "MAX_SLIPPAGE_SAMPLES", 3)
    c = QuoteClient({"PSQ": _ok(27.45)})
    for i in range(6):
        us.record_fill_slippage(c, "PSQ", "AMEX", "buy", 27.70, 27.75, 27.0 + i)

    saved = json.loads(us.US_SLIPPAGE_PATH.read_text(encoding="utf-8"))
    assert len(saved) == 3
    assert [x["avg_price"] for x in saved] == [30.0, 31.0, 32.0], "최신이 남아야 한다"


def test_large_gap_is_flagged(monkeypatch):
    """0.5% 초과 괴리는 경고로 승격 — 실전의 -0.96%가 여기 걸린다."""
    warned = []
    monkeypatch.setattr(us.log, "warning",
                        lambda ev, **kw: warned.append((ev, kw)))
    c = QuoteClient({"PSQ": _ok(27.45)})

    us.record_fill_slippage(c, "PSQ", "AMEX", "buy", 27.70, 27.75, 27.44)

    assert any(ev == "us_fill_price_gap" for ev, _ in warned), "경고가 안 났다"


def test_small_gap_is_not_flagged(monkeypatch):
    """정상 범위 괴리까지 경고하면 경고가 무의미해진다."""
    warned = []
    monkeypatch.setattr(us.log, "warning",
                        lambda ev, **kw: warned.append((ev, kw)))
    c = QuoteClient({"PSQ": _ok(27.71)})

    us.record_fill_slippage(c, "PSQ", "AMEX", "buy", 27.70, 27.75, 27.72)

    assert not any(ev == "us_fill_price_gap" for ev, _ in warned)


def test_slippage_recording_never_raises(monkeypatch):
    """관측 코드가 매매를 죽이면 안 된다."""
    monkeypatch.setattr(us, "US_SLIPPAGE_PATH",
                        us.Path("/proc/nonexistent/us_slippage.json"))
    c = QuoteClient(raises={"PSQ": RuntimeError("quote down")})
    s = us.record_fill_slippage(c, "PSQ", "AMEX", "buy", 27.70, 27.75, 27.44)
    assert s["symbol"] == "PSQ"
