"""갭업 회복 진입 신호 (gap recovery).

크래시 직후 TA 합성점수는 수 주간 마이너스에 갇혀, 펀더멘털 호재로 인한
V자 회복 갭상승마저 변동성돌파/TA 게이트가 전부 veto한다(2026-06-15 종전 호재
국장 랠리를 통째로 놓친 사례). 이 모듈은 그 사각지대를 메운다.

**오직 개장 윈도(기본 09:00~09:20)에서만** 동작한다 — 즉 시가 갭을 보고 들어가되,
오후 추격매수(고점 추격)는 구조적으로 불가능하게 막는다. 추가 가드:
  · 오버나이트 촉발(aggressive_buy) 필요 — 글로벌 리스크온/촉매의 프록시
  · 시가 갭 ≥ 임계(기본 +1.5%)
  · 현재가 ≥ 시가 (갭을 지키는 중 — 시가 아래로 무너지면 진입 안 함)
  · CRISIS 레짐·블라인드(데이터 실패) 제외
레버리지·인버스(곱버스)에는 적용하지 않는다(롱 ETF 전용). 진입 시 소액·타이트 손절.

순수 함수라 단위 테스트로 검증 가능. 라이브 발주는 호출부에서 별도 플래그
(gap_recovery.enabled)로 통제하며, 검증 전에는 dry-run 리포트로만 노출한다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GapRecoverySignal:
    is_buy: bool
    reason: str
    gap_open_pct: float       # (시가 - 전일종가) / 전일종가 * 100
    intraday_pct: float       # (현재가 - 시가) / 시가 * 100
    in_window: bool           # 개장 윈도 내 여부


_DEFAULTS = {
    "window_start_kst": "09:00",
    "window_end_kst": "09:20",
    "gap_min_pct": 1.5,
    "require_above_open": True,
    "catalyst_action": "aggressive_buy",
    "exclude_regimes": ["CRISIS"],
}


def gap_recovery_signal(*, prev_close: float, today_open: float, cur_price: float,
                        now_hhmm: str, overnight_action: str, regime: str,
                        blind: bool, cfg: dict | None = None) -> GapRecoverySignal:
    """개장 갭업 회복 진입 신호를 순수 평가한다.

    Args:
        prev_close: 전일 종가
        today_open: 당일 시가
        cur_price:  현재가
        now_hhmm:   현재 KST 시각 "HH:MM" (zero-padded 24h)
        overnight_action: overnight_signal.recommended_action
        regime:     현재 레짐 ("BULL"/"CAUTION"/"BEAR"/"CRISIS"...)
        blind:      시장데이터 조회 실패 여부
        cfg:        gap_recovery 설정 dict
    """
    c = {**_DEFAULTS, **(cfg or {})}
    start = str(c["window_start_kst"])
    end = str(c["window_end_kst"])
    gap_min = float(c["gap_min_pct"])
    require_above = bool(c["require_above_open"])
    catalyst = str(c["catalyst_action"])
    exclude = set(c.get("exclude_regimes") or [])

    # 가격데이터 유효성
    if not (prev_close and prev_close > 0) or not (today_open and today_open > 0) \
            or not (cur_price and cur_price > 0):
        return GapRecoverySignal(False, "가격데이터 부족(시가/전일종가 없음)", 0.0, 0.0, False)

    gap_open_pct = (today_open - prev_close) / prev_close * 100.0
    intraday_pct = (cur_price - today_open) / today_open * 100.0
    in_window = (start <= now_hhmm <= end)

    if blind:
        return GapRecoverySignal(False, "블라인드(시장데이터 실패) — 진입 보류",
                                 gap_open_pct, intraday_pct, in_window)
    if regime in exclude:
        return GapRecoverySignal(False, f"{regime} 레짐 제외", gap_open_pct, intraday_pct, in_window)
    if overnight_action != catalyst:
        return GapRecoverySignal(False, f"촉발 없음(오버나이트={overnight_action}≠{catalyst})",
                                 gap_open_pct, intraday_pct, in_window)
    if not in_window:
        return GapRecoverySignal(False, f"개장윈도({start}~{end}) 경과 — 추격 방지",
                                 gap_open_pct, intraday_pct, in_window)
    if gap_open_pct < gap_min:
        return GapRecoverySignal(False, f"갭 부족 {gap_open_pct:+.1f}% < {gap_min:.1f}%",
                                 gap_open_pct, intraday_pct, in_window)
    if require_above and cur_price < today_open:
        return GapRecoverySignal(False, f"시가 아래({intraday_pct:+.1f}%) — 갭 소진",
                                 gap_open_pct, intraday_pct, in_window)

    return GapRecoverySignal(
        True,
        f"갭회복 매수: 시가갭 {gap_open_pct:+.1f}%·현재 시가대비 {intraday_pct:+.1f}%·촉발 {catalyst}",
        gap_open_pct, intraday_pct, in_window)
