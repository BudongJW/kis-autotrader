"""US 세션 상태를 KST 날짜가 아니라 US 거래일(ET)로 키잉하는지 검증.

US 세션은 KST로 '월밤~화새벽'에 걸쳐 있고, **마감청산은 항상 KST 자정 이후**
(04:45/05:45)에 일어난다. 상태를 KST 날짜로 키잉하면:
  - 같은 세션의 청산 기록이 세션 날짜보다 하루 뒤로 찍혀 재진입 쿨다운이
    설정보다 한 세션 더 길어진다 (2일 설정 → 실제 3세션 차단)
  - 세션 도중 00:00에 당일 손익 집계가 리셋된다
"""

from datetime import date, datetime

import pytest

from src.utils.clock import KST


def _kst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=KST)


# ──────────────────────────────────────────────────────────
# 거래 기록 → US 거래일 매핑
# ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("ts, expected", [
    ("2026-07-27T22:35:00", "2026-07-27"),   # 개장 직후(KST 월 밤)
    ("2026-07-27T23:50:00", "2026-07-27"),   # 자정 직전
    ("2026-07-28T00:10:00", "2026-07-27"),   # 자정 직후 — 같은 세션
    ("2026-07-28T04:45:00", "2026-07-27"),   # 마감청산 — 같은 세션
    ("2026-07-28T22:35:00", "2026-07-28"),   # 다음 세션
])
def test_trade_row_maps_to_us_session_date(ts, expected):
    from src.bot.us_session import _to_us_session_row

    row = _to_us_session_row({"timestamp": ts, "side": "sell", "symbol": "SCHG"})
    assert row["date"] == expected
    assert row["timestamp"] == expected


def test_force_close_and_next_evening_share_no_session():
    """04:45 KST 청산과 그날 밤 22:35 진입은 서로 다른 US 거래일이어야 한다."""
    from src.bot.us_session import _to_us_session_row

    close_row = _to_us_session_row({"timestamp": "2026-07-28T04:45:00"})
    next_open = _to_us_session_row({"timestamp": "2026-07-28T22:35:00"})
    assert close_row["date"] != next_open["date"]


# ──────────────────────────────────────────────────────────
# 재진입 쿨다운 off-by-one
# ──────────────────────────────────────────────────────────

def test_cooldown_counts_sessions_not_kst_days():
    """cooldown_days=2가 정확히 2일치만 막아야 한다 (기존엔 3세션 차단).

    월요일밤 세션의 마감청산은 KST로 화요일 04:45에 찍힌다. KST 날짜로 비교하면
    기준이 하루 밀려 목요일 밤까지 막혔다. US 거래일로 정규화하면 월요일 세션의
    청산은 '월요일'이 되어 수요일 밤엔 풀린다.
    """
    from src.bot.us_session import _to_us_session_row
    from src.strategies.cost_gate import recently_force_closed

    # 월요일(07-27) 세션의 마감청산 → KST로는 화요일 새벽에 기록됨
    sells = [_to_us_session_row({
        "symbol": "SCHG", "side": "sell",
        "timestamp": "2026-07-28T04:45:00",
        "reason": "마감청산",
    })]
    assert sells[0]["date"] == "2026-07-27"

    def session_of(kst_str):
        from src.utils.clock import us_session_date_et
        return us_session_date_et(_kst(kst_str)).isoformat()

    # 화요일 밤(07-28 세션) → 차단
    assert recently_force_closed("SCHG", sells, session_of("2026-07-28T22:35"), 2)
    # 수요일 밤(07-29 세션) → 차단 (2일차)
    assert recently_force_closed("SCHG", sells, session_of("2026-07-29T22:35"), 2)
    # 목요일 밤(07-30 세션) → 해제. KST 날짜 기준이던 예전엔 여기서도 막혔다.
    assert not recently_force_closed("SCHG", sells, session_of("2026-07-30T22:35"), 2)


def test_cooldown_stable_across_midnight_within_session():
    """세션 도중 자정을 넘어도 기준 세션 날짜가 바뀌지 않는다."""
    from src.utils.clock import us_session_date_et

    before = us_session_date_et(_kst("2026-07-27T23:55"))
    after = us_session_date_et(_kst("2026-07-28T00:05"))
    assert before == after == date(2026, 7, 27)


# ──────────────────────────────────────────────────────────
# US 일일 손실 한도
# ──────────────────────────────────────────────────────────

@pytest.fixture
def us_trades(tmp_path, monkeypatch):
    """trades.csv를 임시 파일로 바꿔치기하고 행을 써 넣는 헬퍼를 준다."""
    import csv
    import src.tracker as tracker

    path = tmp_path / "trades.csv"

    def write(rows):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(tracker.FIELDS)
            for r in rows:
                w.writerow(r)
        monkeypatch.setattr(tracker, "TRADE_LOG_PATH", path)
        return path

    return write


def _row(ts, symbol, side, qty, price_cents):
    return [ts, symbol, symbol, side, qty, price_cents,
            qty * price_cents, 0, "마감청산" if side == "sell" else ""]


def test_us_loss_limit_aggregates_whole_session_across_midnight(us_trades, monkeypatch):
    """자정을 넘긴 매도도 같은 세션 손익으로 집계된다."""
    import src.bot.us_session as us

    us_trades([
        _row("2026-07-27T22:40:00", "QQQM", "buy", 10, 5000),    # $50.00 × 10
        _row("2026-07-28T04:40:00", "QQQM", "sell", 10, 4000),   # $40.00 × 10 → -$100
    ])
    monkeypatch.setattr(us, "now_kst", lambda: _kst("2026-07-28T04:50:00"))

    blocked, reason = us.check_us_daily_loss_limit()
    assert blocked is True, reason
    assert "-100.00 USD" in reason
    assert "2026-07-27" in reason      # 세션 날짜로 보고


def test_us_loss_limit_ignores_kr_trades(us_trades, monkeypatch):
    """국내 체결(원)은 US 한도 계산에 섞이면 안 된다."""
    import src.bot.us_session as us

    us_trades([
        _row("2026-07-27T10:00:00", "069500", "buy", 10, 30000),
        _row("2026-07-27T14:00:00", "069500", "sell", 10, 10000),   # 원화 대손실
    ])
    monkeypatch.setattr(us, "now_kst", lambda: _kst("2026-07-27T23:00:00"))

    blocked, _ = us.check_us_daily_loss_limit()
    assert blocked is False


def test_us_loss_limit_not_triggered_when_profitable(us_trades, monkeypatch):
    import src.bot.us_session as us

    us_trades([
        _row("2026-07-27T22:40:00", "QQQM", "buy", 10, 5000),
        _row("2026-07-28T04:40:00", "QQQM", "sell", 10, 5200),
    ])
    monkeypatch.setattr(us, "now_kst", lambda: _kst("2026-07-28T04:50:00"))

    blocked, reason = us.check_us_daily_loss_limit()
    assert blocked is False
    assert "+20.00 USD" in reason


def test_us_loss_limit_excludes_previous_session(us_trades, monkeypatch):
    """전날 세션의 손실은 오늘 세션 한도에 포함되지 않는다."""
    import src.bot.us_session as us

    us_trades([
        _row("2026-07-27T22:40:00", "QQQM", "buy", 10, 5000),
        _row("2026-07-28T04:40:00", "QQQM", "sell", 10, 4000),   # 07-27 세션 손실
    ])
    monkeypatch.setattr(us, "now_kst", lambda: _kst("2026-07-28T22:40:00"))  # 07-28 세션

    blocked, _ = us.check_us_daily_loss_limit()
    assert blocked is False


# ──────────────────────────────────────────────────────────
# KR 한도가 US 체결에 오염되지 않는지
# ──────────────────────────────────────────────────────────

def test_kr_daily_limit_ignores_us_cent_trades(us_trades, monkeypatch):
    """US 체결은 센트 단위라 원화 집계에 섞이면 손익이 왜곡된다.

    log_trade의 market 인자는 CSV에 기록되지 않으므로(FIELDS에 컬럼 없음)
    심볼 형태로 갈라야 한다.
    """
    import src.risk_manager as rm

    path = us_trades([
        _row("2026-07-27T22:40:00", "QQQM", "buy", 100, 5000),
        _row("2026-07-27T23:40:00", "QQQM", "sell", 100, 1000),   # 센트 기준 대손실
    ])
    monkeypatch.setattr(rm, "today_kst", lambda: date(2026, 7, 27))

    class _Client:
        def get_price(self, *_a, **_k): return {"rt_cd": "1"}
        def get_balance(self): return {"rt_cd": "1"}

    exceeded, reason = rm.check_daily_loss_limit(_Client())
    # US 행만 있으므로 국내 당일 손익은 0 — 한도에 걸리면 안 된다
    assert exceeded is False, reason


def test_is_kr_symbol_discriminates():
    from src.tracker import is_kr_symbol

    assert is_kr_symbol("069500") is True
    assert is_kr_symbol("005930") is True
    assert is_kr_symbol("QQQM") is False
    assert is_kr_symbol("SPLG") is False
    assert is_kr_symbol("") is False
