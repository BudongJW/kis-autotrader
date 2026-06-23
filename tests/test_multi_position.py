"""etf_multi_position_skip 단위 테스트.

핵심: 한 종목 보유했다고 다른 종목 진입을 막지 않는다(단일포지션 가정 제거).
보유 종목은 스킵(피라미딩 가능분만 추가), 신규 슬롯 없으면 미보유도 스킵.
"""
from src.bot.single_run import etf_multi_position_skip


def test_new_symbol_with_slot_not_skipped():
    # 069500 보유 중이어도 미보유 005930은 신규 슬롯 있으면 평가(스킵 안 함)
    held = {"069500": 1}
    assert etf_multi_position_skip("005930", held, set(), open_slots=4) is False


def test_held_non_pyramidable_skipped():
    # 보유 중이고 피라미딩 불가면 스킵(중복 진입 방지)
    held = {"069500": 1}
    assert etf_multi_position_skip("069500", held, set(), open_slots=4) is True


def test_held_pyramidable_not_skipped():
    # 보유 중이라도 피라미딩 가능(+2%)이면 추가 매수 허용
    held = {"069500": 1}
    assert etf_multi_position_skip("069500", held, {"069500"}, open_slots=4) is False


def test_new_symbol_no_slot_skipped():
    # 신규 슬롯(max 도달) 없으면 미보유 종목도 스킵
    held = {"069500": 1, "005930": 1, "000660": 1, "034220": 1, "010140": 1}
    assert etf_multi_position_skip("011200", held, set(), open_slots=0) is True


def test_old_single_position_assumption_removed():
    # 회귀 방지: 예전엔 보유 1개면 모든 신규 진입 차단됐음. 이제 미보유는 허용.
    held = {"069500": 1}
    blocked = [s for s in ("005930", "000660", "034220", "010140", "011200")
               if etf_multi_position_skip(s, held, set(), open_slots=4)]
    assert blocked == []   # 단타종목 5종 전부 평가 가능
