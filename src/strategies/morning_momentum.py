"""조간 모멘텀 스캘프 — 개장 직후 지수의 강한 방향성을 빠르게 잡는다.

배경(사용자 관찰 2026-06-30): 아침마다 확 오르거나 확 떨어지는 추세가 크다. 변동성
돌파/TA 게이트는 느려서 이 초반 추세를 놓친다. 그래서 개장 윈도(기본 09:00~10:00)
한정으로, 벤치마크 지수(KODEX 200)의 전일종가 대비 아침 변동을 보고:
  - 강하게 상승 + 추세 유지(시가 위) → 인덱스 롱 진입
  - 강하게 하락 + 추세 유지(시가 아래) → 인버스 진입
빠르게 들어가고, 타이트한 익절/손절 + 시간청산으로 빠르게 빠진다(오버나이트 캐리 아님).

순수 함수만 둔다(테스트 가능). 실제 발주/청산은 single_run.run_morning_momentum_strategy.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MorningMomentumSignal:
    direction: str        # "long" | "inverse" | "none"
    reason: str
    move_pct: float       # (현재가 - 전일종가) / 전일종가 * 100  — 아침 변동 폭
    intraday_pct: float   # (현재가 - 시가) / 시가 * 100         — 시가 대비(추세 유지 확인)
    in_window: bool

    @property
    def is_entry(self) -> bool:
        return self.direction in ("long", "inverse")


def morning_momentum_signal(*, prev_close: float, today_open: float, cur_price: float,
                            now_hhmm: str, cfg: dict,
                            blind: bool = False) -> MorningMomentumSignal:
    """개장 윈도 내 지수 아침 변동으로 롱/인버스 방향을 판단.

    Args:
        prev_close/today_open/cur_price: 벤치마크 지수(KODEX 200 등) 시세.
        now_hhmm: 현재 KST "HH:MM".
        cfg: morning_momentum 설정 블록.
        blind: 시장데이터 신뢰 불가 시 True → 진입 보류.

    판단:
        move = (현재-전일종가)/전일종가. intra = (현재-시가)/시가.
        move >= up_threshold AND intra >= intra_confirm  → long (계속 상승)
        move <= -down_threshold AND intra <= -intra_confirm → inverse (계속 하락)
        그 외 none. (갭만 뜨고 시가 대비 반대로 꺾이면 진입 안 함 — 반전 회피.)
    """
    start = str(cfg.get("window_start_kst", "09:00"))
    end = str(cfg.get("entry_end_kst", "10:00"))
    up_th = float(cfg.get("up_threshold_pct", 1.0))
    down_th = float(cfg.get("down_threshold_pct", 1.0))
    intra_confirm = float(cfg.get("intraday_confirm_pct", 0.0))

    in_window = start <= now_hhmm <= end

    if prev_close <= 0 or today_open <= 0 or cur_price <= 0:
        return MorningMomentumSignal("none", "가격데이터 부족", 0.0, 0.0, in_window)

    move = (cur_price - prev_close) / prev_close * 100.0
    intra = (cur_price - today_open) / today_open * 100.0

    if blind:
        return MorningMomentumSignal("none", "블라인드(시장데이터 실패) — 진입 보류",
                                     move, intra, in_window)
    if not in_window:
        return MorningMomentumSignal("none", f"윈도 밖({now_hhmm}∉{start}~{end})",
                                     move, intra, in_window)

    if move >= up_th and intra >= intra_confirm:
        return MorningMomentumSignal(
            "long", f"강한 상승 아침추세 (전일대비 {move:+.2f}%, 시가대비 {intra:+.2f}%)",
            move, intra, in_window)
    if move <= -down_th and intra <= -intra_confirm:
        return MorningMomentumSignal(
            "inverse", f"강한 하락 아침추세 (전일대비 {move:+.2f}%, 시가대비 {intra:+.2f}%)",
            move, intra, in_window)

    return MorningMomentumSignal(
        "none", f"임계 미달 (전일대비 {move:+.2f}%, 시가대비 {intra:+.2f}%)",
        move, intra, in_window)


def _hhmm_to_min(s: str) -> int:
    return (int(s[:2]) * 60 + int(s[3:5])) if s and len(s) >= 5 else -10000


def can_reenter(*, meta: dict, now_hhmm: str, cfg: dict) -> tuple[bool, str]:
    """인트라데이 재진입 가능 여부 — 일일 사이클 상한 + 청산 후 쿨다운(순수 함수).

    프로세스(진입→청산→재판단→재진입)가 꼭지 추격/과매매로 폭주하지 않게 막는다.

    Args:
        meta: {"cycles": 오늘 완료 사이클수, "last_exit_hhmm": "HH:MM" or None}
        now_hhmm: 현재 "HH:MM".
        cfg: max_cycles_per_day, reentry_cooldown_min.
    Returns: (재진입 가능?, 사유)
    """
    max_cycles = int(cfg.get("max_cycles_per_day", 3))
    cooldown = int(cfg.get("reentry_cooldown_min", 30))
    cycles = int(meta.get("cycles", 0))
    if cycles >= max_cycles:
        return False, f"일일 사이클 상한 도달({cycles}/{max_cycles})"
    last = meta.get("last_exit_hhmm")
    if last:
        gap = _hhmm_to_min(now_hhmm) - _hhmm_to_min(last)
        if gap < cooldown:
            return False, f"청산 후 쿨다운 중({gap}<{cooldown}분)"
    return True, "재진입 가능"


def should_exit_morning(*, entry_price: float, cur_price: float, direction: str,
                        now_hhmm: str, cfg: dict) -> tuple[bool, str]:
    """조간 포지션 청산 판단 — 타이트한 익절/손절 + 시간청산(오버나이트 캐리 금지).

    direction이 inverse면 인버스 ETF 자체의 가격으로 손익을 본다(인버스 ETF는 지수가
    빠지면 가격이 오르므로, 보유 ETF 가격 기준 손익이 곧 우리 손익이다).

    Returns: (청산여부, 사유).
    """
    tp = float(cfg.get("take_profit_pct", 0.012))
    sl = float(cfg.get("stop_loss_pct", 0.007))
    exit_by = str(cfg.get("exit_by_kst", "11:00"))

    if entry_price <= 0 or cur_price <= 0:
        return False, "가격데이터 부족"

    pnl = (cur_price - entry_price) / entry_price  # 보유 ETF 기준 손익률

    if pnl >= tp:
        return True, f"익절 +{pnl*100:.2f}% (>= +{tp*100:.1f}%)"
    if pnl <= -sl:
        return True, f"손절 {pnl*100:.2f}% (<= -{sl*100:.1f}%)"
    if now_hhmm >= exit_by:
        return True, f"시간청산 {now_hhmm} (>= {exit_by}, 손익 {pnl*100:+.2f}%)"
    return False, f"보유 (손익 {pnl*100:+.2f}%)"
