"""US 체결 품질 — 마켓터블 지정가 + 실체결 확인.

증상: "미장에서 가끔 비싸게 사고 싸게 판다".

원인은 모든 US 주문이 **최종체결가(last)에 건 수동 지정가**였다는 것.
KIS 해외는 호가(bid/ask) 조회가 없어 스프레드를 볼 수 없는데, last에 건 지정가는
호가창 안쪽에 얹히는 주문이라 역선택에 그대로 노출된다:

  - 매수: ask가 내 가격까지 **내려와야** 체결 → 하락 중에만 체결 → 체결 직후 더 하락
  - 매도: bid가 내 가격까지 **올라와야** 체결 → 상승 중에만 체결 → 체결 직후 더 상승
  → 판단이 **틀렸을 때만** 체결되고, 맞았을 땐 미체결로 남는 구조.

여기에 rt_cd=="0"(주문 '접수')만 보고 요청가로 포지션·거래기록을 남기던 문제가
겹쳐, 손절 기준과 저널 손익까지 실제와 어긋났다.
"""

import math

import pytest

from src.bot.us_session import (
    DEFAULT_EOD_LIMIT_BUFFER_PCT, DEFAULT_LIMIT_BUFFER_PCT,
    confirm_us_fill, marketable_limit_price,
)


# ──────────────────────────────────────────────────────────
# 마켓터블 지정가
# ──────────────────────────────────────────────────────────

def test_buy_limit_is_above_last():
    """매수 한도는 last보다 높아야 ask를 건너 즉시 체결된다."""
    px = marketable_limit_price("buy", 100.00, 0.0015)
    assert px > 100.00
    assert px == pytest.approx(100.15, abs=0.011)


def test_sell_limit_is_below_last():
    """매도 한도는 last보다 낮아야 bid를 건너 즉시 체결된다."""
    px = marketable_limit_price("sell", 100.00, 0.0015)
    assert px < 100.00
    assert px == pytest.approx(99.85, abs=0.011)


def test_limits_are_cent_rounded_outward():
    """센트 단위로 **바깥쪽** 반올림 — 안쪽으로 깎으면 다시 수동 주문이 된다."""
    buy = marketable_limit_price("buy", 70.111, 0.0)
    sell = marketable_limit_price("sell", 70.119, 0.0)
    assert buy == 70.12       # 올림
    assert sell == 70.11      # 내림
    for px in (buy, sell):
        assert abs(px * 100 - round(px * 100)) < 1e-6, "센트 정규화 안 됨"


def test_zero_buffer_still_marketable_after_rounding():
    """buffer=0이어도 매수는 올림이라 last 아래로 내려가지 않는다."""
    assert marketable_limit_price("buy", 50.00, 0.0) >= 50.00
    assert marketable_limit_price("sell", 50.00, 0.0) <= 50.00


def test_buffer_caps_worst_case_price():
    """buffer는 지불 가격이 아니라 최악 체결가의 상한이다."""
    last = 200.0
    for buf in (0.0005, 0.0015, 0.004):
        buy = marketable_limit_price("buy", last, buf)
        assert buy <= last * (1 + buf) + 0.01     # 센트 올림 여유
        sell = marketable_limit_price("sell", last, buf)
        assert sell >= last * (1 - buf) - 0.01


def test_eod_buffer_is_more_aggressive():
    """마감청산은 미체결이 곧 오버나이트 캐리라 일반보다 공격적이어야 한다."""
    assert DEFAULT_EOD_LIMIT_BUFFER_PCT > DEFAULT_LIMIT_BUFFER_PCT
    last = 100.0
    normal = marketable_limit_price("sell", last, DEFAULT_LIMIT_BUFFER_PCT)
    eod = marketable_limit_price("sell", last, DEFAULT_EOD_LIMIT_BUFFER_PCT)
    assert eod < normal


def test_invalid_inputs():
    assert marketable_limit_price("buy", 0) == 0.0
    assert marketable_limit_price("sell", -1) == 0.0
    with pytest.raises(ValueError):
        marketable_limit_price("hold", 100.0, 0.001)


def test_adverse_selection_direction_is_fixed():
    """회귀 방지: 예전 동작(= last 그대로)과 방향이 반대인지 확인.

    예전엔 buy/sell 모두 last를 그대로 썼다. 이제 매수는 위로, 매도는 아래로
    벌어져야 한다 — 이 부호가 뒤집히면 증상이 그대로 재발한다.
    """
    last = 123.45
    assert marketable_limit_price("buy", last) > last
    assert marketable_limit_price("sell", last) < last


# ──────────────────────────────────────────────────────────
# 실체결 확인
# ──────────────────────────────────────────────────────────

class _HoldingsClient:
    """get_us_holdings가 호출될 때마다 미리 정한 스냅샷을 순서대로 돌려준다."""

    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        idx = min(self.calls - 1, len(self._snapshots) - 1)
        return self._snapshots[idx]


@pytest.fixture
def fast_fill(monkeypatch):
    """폴링 대기를 없애 테스트를 즉시 끝낸다."""
    import src.bot.us_session as us

    monkeypatch.setattr(us, "load_us_execution_config",
                        lambda: {"fill_wait_sec": 0.2, "fill_poll_sec": 0.01})

    def _install(client):
        monkeypatch.setattr(us, "get_us_holdings", lambda _c: client.snapshot())

    return _install


def test_confirm_fill_full_buy(fast_fill):
    client = _HoldingsClient([{"QQQM": {"qty": 10, "avg_price": 50.07}}])
    fast_fill(client)
    filled, avg = confirm_us_fill(client, "QQQM", "buy", qty_before=0, expected_qty=10)
    assert filled == 10
    assert avg == pytest.approx(50.07)


def test_confirm_fill_waits_then_fills(fast_fill):
    """접수 직후엔 잔고에 안 잡히고 잠시 뒤 체결되는 정상 케이스."""
    client = _HoldingsClient([
        {},                                            # 아직 미체결
        {},
        {"QQQM": {"qty": 5, "avg_price": 49.98}},      # 체결
    ])
    fast_fill(client)
    filled, avg = confirm_us_fill(client, "QQQM", "buy", qty_before=0, expected_qty=5)
    assert filled == 5
    assert avg == pytest.approx(49.98)


def test_confirm_fill_reports_unfilled(fast_fill):
    """끝까지 안 잡히면 0 — 호출부가 포지션을 기록하지 않아야 한다."""
    client = _HoldingsClient([{}])
    fast_fill(client)
    filled, _ = confirm_us_fill(client, "QQQM", "buy", qty_before=0, expected_qty=10)
    assert filled == 0


def test_confirm_fill_partial(fast_fill):
    client = _HoldingsClient([{"QQQM": {"qty": 4, "avg_price": 50.0}}])
    fast_fill(client)
    filled, _ = confirm_us_fill(client, "QQQM", "buy", qty_before=0, expected_qty=10)
    assert filled == 4


def test_confirm_fill_sell_direction(fast_fill):
    """매도는 보유수량이 줄어든 만큼이 체결분."""
    client = _HoldingsClient([{"QQQM": {"qty": 0, "avg_price": 0}}])
    fast_fill(client)
    filled, _ = confirm_us_fill(client, "QQQM", "sell", qty_before=10, expected_qty=10)
    assert filled == 10


def test_confirm_fill_sell_partial_keeps_remainder(fast_fill):
    client = _HoldingsClient([{"QQQM": {"qty": 3, "avg_price": 50.0}}])
    fast_fill(client)
    filled, _ = confirm_us_fill(client, "QQQM", "sell", qty_before=10, expected_qty=10)
    assert filled == 7


def test_confirm_fill_lookup_failure_returns_sentinel(monkeypatch):
    """잔고 조회가 계속 실패하면 -1 → 호출부가 요청값으로 폴백한다."""
    import src.bot.us_session as us

    monkeypatch.setattr(us, "load_us_execution_config",
                        lambda: {"fill_wait_sec": 0.05, "fill_poll_sec": 0.01})

    def _boom(_c):
        raise RuntimeError("KIS 조회 실패")

    monkeypatch.setattr(us, "get_us_holdings", _boom)
    filled, avg = confirm_us_fill(object(), "QQQM", "buy", qty_before=0, expected_qty=10)
    assert filled == -1
    assert avg == 0.0


def test_buy_ignores_preexisting_holding(fast_fill):
    """이미 보유분이 있으면 증가분만 체결로 센다."""
    client = _HoldingsClient([{"QQQM": {"qty": 15, "avg_price": 50.0}}])
    fast_fill(client)
    filled, _ = confirm_us_fill(client, "QQQM", "buy", qty_before=10, expected_qty=5)
    assert filled == 5
