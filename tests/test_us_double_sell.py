"""같은 US 포지션에 매도 주문이 두 번 나가던 문제.

US 포지션의 청산 경로는 셋이다:

  1. check_us_risk()        — us_positions.json 순회, 60초 주기
  2. run_us_momentum_...()  — 모멘텀 자체 state 순회, 300초 주기
  3. close_us_positions()   — us_positions.json 순회, 마감 직전 1회

그런데 US-MOM 매수는 `record_us_buy()`(us_positions.json)와 모멘텀 state에
**동시에** 등록된다. 즉 한 포지션에 청산 담당이 둘이고, 서로를 모른다.

2026-07-29 실전 로그 (run 30462059362):

    [US-MOM BUY] PSQ 10주 @ 한도 $27.75 (현재가 $27.70) ≤ $277.50 (inverse)
          체결: 10주 @ $27.44 (슬리피지 -0.96%)
    [US 리스크] PSQ 10주 @ 한도 $27.57 (현재가 $27.61) — US 추적손절 (고점 $27.84에서 -0.8%)
          응답: rt_cd=0, msg=주문 전송 완료 되었습니다.
    [US-MOM] PSQ 청산: 본전이익 보존 (고점 +1.46%였다가 반전 → +0.55%에서 청산)
          응답: rt_cd=0, msg=주문 전송 완료 되었습니다.

10주를 사고 10주 매도가 **두 번** 접수됐다. 첫 주문이 체결된 뒤 두 번째가 나가면
없는 주식을 파는 것 = 공매도가 열린다. 그리고 `get_us_holdings()`는 `qty > 0`인
종목만 반환하므로 **그 음수 포지션은 봇 눈에 아예 안 보인다** — 손절도 마감청산도
안 걸린다.

방어는 두 겹이다:
  A. 소유권 분리 — US-MOM이 들고 있는 동안엔 범용 경로가 손대지 않는다.
  B. 주문 직전 브로커 잔고 재확인 — 경로와 무관하게 보유량 초과 매도를 막는다.

B가 없으면 A만으로는 부족하다. 세 경로 말고 다른 조합(수동 개입, 부분체결 뒤
재시도)에서도 같은 사고가 날 수 있기 때문이다.
"""

import pytest

import src.bot.us_session as us


# ──────────────────────────────────────────────────────────
# 공통 스텁
# ──────────────────────────────────────────────────────────

class FakeClient:
    """order_overseas 호출을 전부 기록하는 스텁."""

    def __init__(self, rt_cd="0"):
        self.orders = []
        self.rt_cd = rt_cd

    def order_overseas(self, symbol, qty, price=None, side=None,
                       exchange=None, order_type=None):
        self.orders.append({"symbol": symbol, "qty": qty, "price": price,
                            "side": side, "exchange": exchange})
        return {"rt_cd": self.rt_cd, "msg1": "주문 전송 완료 되었습니다."}


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """포지션 파일·체결확인·거래기록을 전부 임시본으로 격리."""
    monkeypatch.setattr(us, "US_POSITIONS_PATH", tmp_path / "us_positions.json")
    monkeypatch.setattr(us, "US_MOM_POSITIONS_PATH", tmp_path / "us_mom.json")
    monkeypatch.setattr(us, "log_trade", lambda *a, **k: None)
    return tmp_path


def _held(monkeypatch, qty, avg=27.44):
    """브로커 보유수량 스텁. qty=-1이면 조회 실패."""
    monkeypatch.setattr(us, "_held_qty", lambda _c, _s: (qty, avg))


# ══════════════════════════════════════════════════════════
# B) 주문 직전 브로커 잔고 가드 — _sell_and_record
# ══════════════════════════════════════════════════════════

def test_no_order_when_broker_holds_nothing(isolate, monkeypatch):
    """이미 팔린 포지션에 매도 주문을 내면 공매도다 — 주문 자체가 나가면 안 된다."""
    _held(monkeypatch, 0)
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44}})
    client = FakeClient()

    ok = us._sell_and_record(client, "PSQ", "AMEX", 10, 27.61, 27.57, "추적손절")

    assert client.orders == [], "브로커 보유 0주인데 매도 주문이 나갔다 (공매도 위험)"
    assert ok is True, "이미 청산된 포지션은 '청산 완료'로 처리돼야 호출부가 정리한다"
    assert "PSQ" not in us.load_us_positions(), "장부가 정리되지 않았다"


def test_no_phantom_trade_record_when_skipped(isolate, monkeypatch):
    """주문을 안 냈으면 거래기록도 남기면 안 된다 (저널 손익 오염)."""
    _held(monkeypatch, 0)
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44}})
    recorded = []
    monkeypatch.setattr(us, "log_trade", lambda *a, **k: recorded.append(a))

    us._sell_and_record(FakeClient(), "PSQ", "AMEX", 10, 27.61, 27.57, "추적손절")

    assert recorded == [], "체결되지도 않은 매도가 거래기록에 남았다"


def test_qty_clamped_to_actual_holding(isolate, monkeypatch):
    """부분 청산 뒤 잔여분보다 많이 팔라고 하면 실보유까지만 판다."""
    _held(monkeypatch, 4)
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44}})
    client = FakeClient()
    monkeypatch.setattr(us, "confirm_us_fill", lambda *a, **k: (4, 27.5))

    us._sell_and_record(client, "PSQ", "AMEX", 10, 27.61, 27.57, "마감청산", eod=True)

    assert len(client.orders) == 1
    assert client.orders[0]["qty"] == 4, "보유량을 초과해 주문했다"


def test_normal_full_sell_unchanged(isolate, monkeypatch):
    """보유량과 요청량이 같으면 기존 동작 그대로."""
    _held(monkeypatch, 10)
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44}})
    client = FakeClient()
    monkeypatch.setattr(us, "confirm_us_fill", lambda *a, **k: (10, 27.5))

    ok = us._sell_and_record(client, "PSQ", "AMEX", 10, 27.61, 27.57, "청산")

    assert ok is True
    assert len(client.orders) == 1 and client.orders[0]["qty"] == 10
    assert "PSQ" not in us.load_us_positions()


def test_lookup_failure_falls_back_to_old_behavior(isolate, monkeypatch):
    """잔고 조회 실패(-1)면 판단 근거가 없다 — 막지 말고 기존 동작 유지.

    조회 실패로 청산을 건너뛰면 손절이 통째로 누락된다. 그쪽이 더 위험하다.
    """
    _held(monkeypatch, -1)
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44}})
    client = FakeClient()
    monkeypatch.setattr(us, "confirm_us_fill", lambda *a, **k: (-1, 0.0))

    ok = us._sell_and_record(client, "PSQ", "AMEX", 10, 27.61, 27.57, "손절")

    assert len(client.orders) == 1 and client.orders[0]["qty"] == 10
    assert ok is True


def test_rejected_order_keeps_position(isolate, monkeypatch):
    """거부(rt_cd != 0)면 여전히 보유 중 — 장부에서 지우면 유령이 된다."""
    _held(monkeypatch, 10)
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44}})

    ok = us._sell_and_record(FakeClient(rt_cd="1"), "PSQ", "AMEX", 10,
                             27.61, 27.57, "손절")

    assert ok is False
    assert "PSQ" in us.load_us_positions()


# ══════════════════════════════════════════════════════════
# A) 소유권 분리 — is_momentum_owned
# ══════════════════════════════════════════════════════════

def test_momentum_position_is_owned_by_momentum(isolate):
    us._save_us_mom({"PSQ": {"entry": 27.44, "qty": 10}})
    assert us.is_momentum_owned("PSQ", {"asset_type": "us_mom_inverse"}) is True
    assert us.is_momentum_owned("QQQM", {"asset_type": "us_mom_long"}) is False


def test_plain_us_position_is_not_momentum_owned(isolate):
    """일반 US 전략 포지션은 범용 리스크 경로가 계속 관리한다."""
    us._save_us_mom({"SPLG": {"entry": 70.0, "qty": 3}})   # 심볼이 같아도
    assert us.is_momentum_owned("SPLG", {"asset_type": "us_long"}) is False
    assert us.is_momentum_owned("SPLG", {}) is False


def test_orphaned_momentum_position_falls_back_to_risk_manager(isolate):
    """모멘텀 state가 유실되면 소유권을 놓아야 한다 — 아니면 아무도 관리 안 한다.

    세션 사망·아티팩트 누락으로 us_momentum_positions.json이 비면, 태그만 보고
    계속 제외했다간 손절도 마감청산도 안 걸리는 고아 포지션이 된다.
    """
    us._save_us_mom({})     # 모멘텀 state 유실
    assert us.is_momentum_owned("PSQ", {"asset_type": "us_mom_inverse"}) is False


def test_risk_manager_skips_momentum_position(isolate, monkeypatch):
    """실전 재현: 모멘텀이 들고 있는 PSQ에 리스크 매니저가 손대면 안 된다."""
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44,
                                  "asset_type": "us_mom_inverse",
                                  "exchange": "AMEX"}})
    us._save_us_mom({"PSQ": {"entry": 27.44, "qty": 10, "peak": 27.84}})
    monkeypatch.setattr(us, "get_us_price", lambda *a, **k: 27.61)
    monkeypatch.setattr(us, "check_us_stop_loss",
                        lambda *a, **k: (True, "US 추적손절"))
    client = FakeClient()

    us.check_us_risk(client, dry_run=False)

    assert client.orders == [], "모멘텀 보유분에 리스크 매니저가 중복 매도를 냈다"
    assert "PSQ" in us.load_us_positions()


def test_risk_manager_still_sells_orphaned_momentum_position(isolate, monkeypatch):
    """모멘텀 state가 없으면 리스크 매니저가 떠맡아야 한다."""
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44,
                                  "asset_type": "us_mom_inverse",
                                  "exchange": "AMEX"}})
    us._save_us_mom({})
    monkeypatch.setattr(us, "get_us_price", lambda *a, **k: 27.61)
    monkeypatch.setattr(us, "check_us_stop_loss", lambda *a, **k: (True, "손절"))
    _held(monkeypatch, 10)
    monkeypatch.setattr(us, "confirm_us_fill", lambda *a, **k: (10, 27.5))
    client = FakeClient()

    us.check_us_risk(client, dry_run=False)

    assert len(client.orders) == 1, "고아 포지션이 방치됐다"


def test_risk_manager_still_sells_plain_position(isolate, monkeypatch):
    """일반 포지션 손절은 그대로 동작해야 한다 (기능 회귀 방지)."""
    us.save_us_positions({"SPLG": {"qty": 3, "buy_price": 70.0,
                                   "asset_type": "us_long", "exchange": "AMEX"}})
    us._save_us_mom({"PSQ": {"entry": 27.44, "qty": 10}})
    monkeypatch.setattr(us, "get_us_price", lambda *a, **k: 68.0)
    monkeypatch.setattr(us, "check_us_stop_loss", lambda *a, **k: (True, "손절"))
    _held(monkeypatch, 3)
    monkeypatch.setattr(us, "confirm_us_fill", lambda *a, **k: (3, 68.0))
    client = FakeClient()

    us.check_us_risk(client, dry_run=False)

    assert len(client.orders) == 1 and client.orders[0]["symbol"] == "SPLG"


def test_eod_close_skips_momentum_position(isolate, monkeypatch):
    """마감청산도 모멘텀 보유분엔 손대면 안 된다 (모멘텀이 세션말 청산을 직접 한다)."""
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44,
                                  "asset_type": "us_mom_inverse",
                                  "exchange": "AMEX"}})
    us._save_us_mom({"PSQ": {"entry": 27.44, "qty": 10}})
    monkeypatch.setattr(us, "get_us_price", lambda *a, **k: 27.61)
    monkeypatch.setattr(us, "eod_us_hold_decision", lambda *a, **k: (False, "청산"))
    client = FakeClient()

    us.close_us_positions(client, dry_run=False)

    assert client.orders == [], "모멘텀 보유분에 마감청산이 중복 매도를 냈다"


def test_eod_close_still_liquidates_plain_position(isolate, monkeypatch):
    """일반 포지션 마감청산은 그대로 (기능 회귀 방지)."""
    us.save_us_positions({"SPLG": {"qty": 3, "buy_price": 70.0,
                                   "asset_type": "us_long", "exchange": "AMEX"}})
    us._save_us_mom({})
    monkeypatch.setattr(us, "get_us_price", lambda *a, **k: 69.0)
    monkeypatch.setattr(us, "eod_us_hold_decision", lambda *a, **k: (False, "청산"))
    _held(monkeypatch, 3)
    monkeypatch.setattr(us, "confirm_us_fill", lambda *a, **k: (3, 69.0))
    client = FakeClient()

    us.close_us_positions(client, dry_run=False)

    assert len(client.orders) == 1 and client.orders[0]["symbol"] == "SPLG"


# ══════════════════════════════════════════════════════════
# 두 방어가 겹쳤을 때 — 실전 시나리오 재현
# ══════════════════════════════════════════════════════════

def test_second_seller_cannot_open_a_short(isolate, monkeypatch):
    """리스크가 먼저 팔고 모멘텀이 뒤이어 파는 실전 순서를 그대로 재현.

    소유권 분리를 우회하는 경우(고아 판정 등)에도 두 번째 매도는 브로커 잔고
    가드에 막혀야 한다. 막히지 않으면 공매도가 열리고, get_us_holdings()가
    qty>0만 보므로 그 포지션은 영원히 보이지 않는다.
    """
    us.save_us_positions({"PSQ": {"qty": 10, "buy_price": 27.44,
                                  "asset_type": "us_mom_inverse",
                                  "exchange": "AMEX"}})
    client = FakeClient()

    # 1차: 리스크 매니저 — 10주 보유 → 정상 매도
    _held(monkeypatch, 10)
    monkeypatch.setattr(us, "confirm_us_fill", lambda *a, **k: (10, 27.57))
    assert us._sell_and_record(client, "PSQ", "AMEX", 10, 27.61, 27.57,
                               "추적손절") is True

    # 2차: 모멘텀 — 이미 0주 → 주문이 나가면 안 된다
    _held(monkeypatch, 0)
    assert us._sell_and_record(client, "PSQ", "AMEX", 10, 27.59, 27.55,
                               "본전이익 보존") is True

    assert len(client.orders) == 1, (
        f"매도가 {len(client.orders)}번 나갔다 — 공매도가 열린다"
    )
