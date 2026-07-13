"""미장 방향성 모멘텀 — config 정합성 + 조간 엔진의 US 파라미터 동작 + 자정넘김 청산시각.

핵심 리스크(US 왕복수수료 0.5%)를 파라미터가 실제로 커버하는지, 자정을 넘는 세션에서
시간청산/윈도 로직이 깨지지 않는지 회귀 검증. 룰 기반이라 테스트 가능(CLAUDE.md).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

from src.strategies.morning_momentum import morning_momentum_signal

KST = ZoneInfo("Asia/Seoul")
US_ROUNDTRIP_FEE = 0.005  # 진입+청산 ~0.5%


def _cfg() -> dict:
    with open("configs/user_overrides.yaml", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("us_momentum", {})


def test_instruments_are_affordable_nasdaq_pair():
    c = _cfg()
    assert c.get("enabled") is True
    assert c.get("long_symbol") == "QQQM"    # QQQ($712)는 가용예산 초과 → 저가판
    assert c.get("inverse_symbol") == "PSQ"  # 나스닥 1x 인버스(곱버스 아님)


def test_breakeven_buffer_covers_roundtrip_fee():
    # 본전버퍼 < 수수료면 "본전청산"이 실제론 순손실 → US는 버퍼가 수수료 이상이어야
    assert _cfg().get("breakeven_buffer_pct", 0) >= US_ROUNDTRIP_FEE


def test_thresholds_and_tp_clear_cost():
    c = _cfg()
    assert c.get("up_threshold_pct", 0) / 100 > US_ROUNDTRIP_FEE   # 진입문턱 > 수수료
    assert c.get("take_profit_pct", 0) > US_ROUNDTRIP_FEE * 2       # 익절 > 왕복수수료 2배


def test_string_time_exit_disabled_for_overnight():
    # 자정 넘는 세션에서 now>=exit_by 문자열 비교가 오작동하지 않게 exit_by=99:99
    c = _cfg()
    assert c.get("exit_by_kst") == "99:99"
    assert "23:59" < "99:99" and "00:30" < "99:99"  # 어떤 실제 HHMM도 트리거 안 됨


def test_up_move_goes_long():
    # 전일대비 +1.0%(>0.8), 시가대비 +  → QQQM 롱
    s = morning_momentum_signal(prev_close=100, today_open=100.3, cur_price=101.0,
                                now_hhmm="22:40", cfg=_cfg())
    assert s.direction == "long"


def test_down_move_goes_inverse():
    # 전일대비 -1.0%, 시가대비 -  → PSQ 숏(인버스)
    s = morning_momentum_signal(prev_close=100, today_open=99.7, cur_price=99.0,
                                now_hhmm="22:40", cfg=_cfg())
    assert s.direction == "inverse"


def test_extended_move_antichase_follows_config():
    # 안티체이스(max_move_pct)는 config 값에 따름. 2026-07-09 추세추종 전환으로 0(삭제).
    #   max_move_pct==0: 큰 추세(-3.5%)도 진입(추세 라이드)
    #   max_move_pct>0 : 그 이상 움직임은 추격 회피로 차단
    c = _cfg()
    s = morning_momentum_signal(prev_close=100, today_open=98, cur_price=96.5,
                                now_hhmm="22:40", cfg=c)
    if float(c.get("max_move_pct", 0) or 0) == 0:
        assert s.direction == "inverse"        # 안티체이스 꺼짐 → 진입
    else:
        assert s.direction == "none" and "추격" in s.reason


def test_no_entry_after_midnight_window_closed():
    # 00:30 KST는 진입윈도(22:30~23:59) 밖 → 진입 안 함(청산 전용 구간)
    s = morning_momentum_signal(prev_close=100, today_open=100.3, cur_price=101.5,
                                now_hhmm="00:30", cfg=_cfg())
    assert s.direction == "none" and not s.in_window


def test_minutes_until_us_close_crosses_midnight():
    from src.bot.us_session import _minutes_until_us_close
    # 23:30 KST → 다음날 05:00 폐장까지 330분
    now = datetime(2026, 7, 3, 23, 30, tzinfo=KST)
    assert abs(_minutes_until_us_close(now, "05:00") - 330) < 1
    # 04:45 KST → 같은날 05:00 폐장까지 15분
    now2 = datetime(2026, 7, 4, 4, 45, tzinfo=KST)
    assert abs(_minutes_until_us_close(now2, "05:00") - 15) < 1


def test_minutes_since_us_open_crosses_midnight():
    # 진입창 버그 수정 핵심: 개장(22:30) 경과분을 자정 넘어서도 올바로 계산
    from src.bot.us_session import _minutes_since_us_open
    assert abs(_minutes_since_us_open(datetime(2026, 7, 6, 22, 35, tzinfo=KST), "22:30") - 5) < 1
    assert abs(_minutes_since_us_open(datetime(2026, 7, 6, 23, 0, tzinfo=KST), "22:30") - 30) < 1
    # 01:00 KST(자정 이후) → 개장 전날 22:30부터 150분
    assert abs(_minutes_since_us_open(datetime(2026, 7, 7, 1, 0, tzinfo=KST), "22:30") - 150) < 1


def test_entry_window_reachable_after_midnight():
    # 버그 재현 방지: 01:00 KST가 진입창 안(개장 후 150분 <= 180). 예전엔 '01:00 ∉ 22:30~23:59'로 영영 불가.
    from src.bot.us_session import _minutes_since_us_open
    mso = _minutes_since_us_open(datetime(2026, 7, 7, 1, 0, tzinfo=KST), "22:30")
    assert 0 <= mso <= 180        # 진입 가능
    # 03:30 KST(개장 후 300분)는 창 밖 — 세션 후반 진입 안 함(force-exit 구간 근접)
    late = _minutes_since_us_open(datetime(2026, 7, 7, 3, 30, tzinfo=KST), "22:30")
    assert late > 180
