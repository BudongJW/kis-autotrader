"""조간 모멘텀 스캘프 시그널/청산 단위 테스트.

방향 판단(상승→롱, 하락→인버스, 갭반전 회피, 윈도/블라인드)과 청산(익절/손절/시간)을
검증. 룰 기반 매매는 테스트 가능 영역(CLAUDE.md).
"""
from src.strategies.morning_momentum import (
    morning_momentum_signal, should_exit_morning, can_reenter,
)

CFG = {
    "window_start_kst": "09:00", "entry_end_kst": "10:00",
    "up_threshold_pct": 1.0, "down_threshold_pct": 1.0,
    "intraday_confirm_pct": 0.0,
    "take_profit_pct": 0.012, "stop_loss_pct": 0.007, "exit_by_kst": "11:00",
}


def _sig(prev, op, cur, hhmm="09:10", blind=False):
    return morning_momentum_signal(prev_close=prev, today_open=op, cur_price=cur,
                                   now_hhmm=hhmm, cfg=CFG, blind=blind)


def test_strong_up_goes_long():
    # 전일 100 → 시가 101 → 현재 102 (전일대비 +2%, 시가대비 상승) → 롱
    s = _sig(100, 101, 102)
    assert s.direction == "long" and s.is_entry


def test_strong_down_goes_inverse():
    # 전일 100 → 시가 99 → 현재 98 (전일대비 -2%, 시가대비 하락) → 인버스
    s = _sig(100, 99, 98)
    assert s.direction == "inverse" and s.is_entry


def test_gap_up_but_fading_no_entry():
    # 갭업(시가 102)인데 현재 100.5로 시가 대비 꺾임 → 전일대비 +0.5%<1% 임계미달 + 반전 → none
    s = _sig(100, 102, 100.5)
    assert s.direction == "none"


def test_gap_up_strong_but_reversing_below_open_no_long():
    # 전일대비 +1.2%지만 시가(101.5) 대비 하락(현재 101.2) — intra +면 롱 유지.
    # 시가 아래로 꺾이면 롱 안 함: 시가 101.5, 현재 101.0(전일대비 +1.0, 시가대비 -0.49)
    s = morning_momentum_signal(prev_close=100, today_open=101.5, cur_price=101.0,
                                now_hhmm="09:10", cfg=CFG)
    assert s.direction == "none"   # 전일대비 +1.0 충족이나 시가대비 음수라 추세 미확인


def test_small_move_no_entry():
    s = _sig(100, 100.2, 100.3)   # +0.3% — 임계 미달
    assert s.direction == "none"


def test_out_of_window_no_entry():
    s = _sig(100, 101, 102, hhmm="11:30")   # 윈도 밖
    assert s.direction == "none" and not s.in_window


def test_blind_no_entry():
    s = _sig(100, 99, 97, blind=True)   # 강한 하락이지만 블라인드
    assert s.direction == "none"


def test_bad_data_no_entry():
    s = _sig(0, 0, 0)
    assert s.direction == "none"


def test_exit_take_profit():
    out, _ = should_exit_morning(entry_price=100, cur_price=101.5, direction="long",
                                 now_hhmm="09:30", cfg=CFG)
    assert out   # +1.5% >= +1.2% 익절


def test_exit_stop_loss():
    out, _ = should_exit_morning(entry_price=100, cur_price=99.0, direction="inverse",
                                 now_hhmm="09:30", cfg=CFG)
    assert out   # -1.0% <= -0.7% 손절


def test_exit_time_force():
    out, why = should_exit_morning(entry_price=100, cur_price=100.3, direction="long",
                                   now_hhmm="11:05", cfg=CFG)
    assert out and "시간청산" in why   # 손익 무관 시간청산


def test_no_exit_within_band_before_time():
    out, _ = should_exit_morning(entry_price=100, cur_price=100.3, direction="long",
                                 now_hhmm="09:40", cfg=CFG)
    assert not out   # +0.3%, 시간 전 → 보유


# ── 인트라데이 재진입(사이클 상한·쿨다운) ──
RC = {"max_cycles_per_day": 3, "reentry_cooldown_min": 30}


def test_reenter_first_time_ok():
    ok, _ = can_reenter(meta={"cycles": 0, "last_exit_hhmm": None},
                        now_hhmm="09:15", cfg=RC)
    assert ok   # 첫 진입 — 청산 이력 없음


def test_reenter_blocked_by_cooldown():
    # 10:00 청산 후 10:20(20분<30) 재진입 시도 → 차단
    ok, why = can_reenter(meta={"cycles": 1, "last_exit_hhmm": "10:00"},
                          now_hhmm="10:20", cfg=RC)
    assert not ok and "쿨다운" in why


def test_reenter_ok_after_cooldown():
    # 10:00 청산 후 10:35(35분>=30) → 재진입 가능
    ok, _ = can_reenter(meta={"cycles": 1, "last_exit_hhmm": "10:00"},
                        now_hhmm="10:35", cfg=RC)
    assert ok


def test_reenter_blocked_by_daily_cap():
    # 이미 3사이클 완료 → 상한 도달, 쿨다운 지나도 차단
    ok, why = can_reenter(meta={"cycles": 3, "last_exit_hhmm": "13:00"},
                          now_hhmm="13:59", cfg=RC)
    assert not ok and "상한" in why
