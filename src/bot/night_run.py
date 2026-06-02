"""미국 ETF 야간 매매 봇 — 한국 밤/새벽 시간대 미국장 운영.

실행 모드:
  --loop     : 미국장 개장~폐장까지 연속 감시 (22:30~06:00 또는 23:30~06:00 KST)
  (기본)     : 1회 체크 후 종료.

전략: 변동성 돌파 + TA (us_session.py 전략 모듈)
  - QQQ/SPY/SH/SMH/TLT 유니버스
  - 한국장 레짐 연동 (bear → SH 우선)
  - USD 기준 손절/추적손절

리스크 관리 (1분마다 체크):
  - 손절: -2.5% 하락 시 매도
  - 추적 손절: +2% 도달 후 고점 -1% 이탈 시 매도
  - 장 마감 30분 전 미청산 포지션 강제 청산
"""

from __future__ import annotations

import argparse
import time as time_mod
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from src.config import settings
from src.kis_client import KISClient
from src.safety import killswitch
from src.bot.us_session import (
    load_us_config,
    is_us_market_hours,
    get_us_market_times,
    run_us_strategy,
    check_us_risk,
    close_us_positions,
    load_us_positions,
)
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

# 루프 간격 (초)
RISK_CHECK_INTERVAL = 60        # 리스크 체크: 1분
STRATEGY_CHECK_INTERVAL = 300   # 전략 체크: 5분
# 개장 직후(첫 30분) 전략 체크를 좀 더 자주
STRATEGY_CHECK_EARLY = 120      # 개장 초반: 2분
EARLY_SESSION_MINUTES = 30      # 개장 후 30분까지 '초반'
# 폐장 전 청산 시점
CLOSE_BEFORE_MINUTES = 15       # 폐장 15분 전 청산 시작
# 개장 직전 대기 허용 한도. 다음 개장까지 이보다 더 남았으면(장외 dead zone)
# 대기 없이 즉시 종료한다. pre-open cron(개장 60분 전부터)은 모두 커버.
PREOPEN_WAIT_LIMIT_MIN = 120

# 루프 자체 최대 실행시간(초). 미국장 본세션(~390분)은 GitHub 하드 타임아웃(360분)을
# 넘으므로, 그 전에 스스로 정상 종료해 정리 스텝(거래기록·저널 푸시)을 보장한다.
# (KR single_run과 동일 사유 — 강제 종료 시 체결 기록 유실 방지)
MAX_LOOP_RUNTIME_SEC = 340 * 60


def _now() -> datetime:
    return datetime.now(KST)


def _time_in_range(t: dtime, start: dtime, end: dtime) -> bool:
    """자정을 넘는 시간 범위 지원."""
    if start > end:
        return t >= start or t <= end
    return start <= t <= end


def run_once(dry_run: bool) -> None:
    """미국장 1회 체크: 리스크 + 전략."""
    cfg = load_us_config()
    if not cfg.get("enabled", False):
        print("[US Night] 비활성화 상태. configs/strategy.yaml us_session.enabled=true 필요.")
        return

    if not is_us_market_hours():
        open_t, close_t = get_us_market_times()
        print(f"[US Night] 미국 정규장 시간 아님 (개장: {open_t.strftime('%H:%M')}~"
              f"{close_t.strftime('%H:%M')} KST)")
        return

    now = _now()
    print(f"\n{'=' * 50}")
    print(f"[US Night] 1회 체크 | {now:%Y-%m-%d %H:%M:%S} KST")
    print(f"  mode={settings.mode.value} | dry_run={dry_run}")
    print(f"{'=' * 50}")

    client = KISClient()

    # 리스크 체크
    check_us_risk(client, dry_run)

    # 전략 실행
    run_us_strategy(client, dry_run)

    # 포트폴리오 저널 업데이트
    _update_journal()

    print(f"\n[US Night] 1회 체크 완료.\n")


def run_loop(dry_run: bool) -> None:
    """미국장 연속 감시 루프.

    매 1분: 리스크 체크 (손절/추적손절)
    매 5분: 전략 실행 (변동성 돌파 매수 탐색)
    폐장 15분 전: 미청산 포지션 전량 청산
    """
    cfg = load_us_config()
    if not cfg.get("enabled", False):
        print("[US Night] 비활성화 상태.")
        return

    open_t, close_t = get_us_market_times()
    summer = cfg.get("summer_time", False)

    print(f"\n{'=' * 60}")
    print(f"[US Night Loop] 미국장 야간 매매 시작")
    print(f"  mode={settings.mode.value} | dry_run={dry_run}")
    print(f"  시간대: {'서머타임' if summer else '동절기'}")
    print(f"  개장: {open_t.strftime('%H:%M')} KST | 폐장: {close_t.strftime('%H:%M')} KST")
    print(f"  리스크 체크: {RISK_CHECK_INTERVAL}초 | 전략 체크: {STRATEGY_CHECK_INTERVAL}초")
    print(f"{'=' * 60}")

    client = KISClient()

    last_strategy_check = 0.0
    bought_today = False
    closing_done = False

    # ── Killswitch 초기 체크 ──
    ks_status = killswitch.get_status()
    if ks_status["active"]:
        print(f"\n⚠️  [Killswitch] mode={ks_status['mode']} | reason={ks_status['reason']}")
        if ks_status["mode"] == "full_stop":
            print("  full_stop → 미국장 봇 진입 안 함. 종료.")
            return

    wait_start = time_mod.time()
    loop_start_epoch = wait_start  # 하드 타임아웃 전 자체 종료 기준
    MAX_WAIT_SECONDS = 7200  # 개장 대기 최대 2시간

    while True:
        now = _now()
        t = now.time()
        epoch_now = time_mod.time()

        # ── Killswitch 매 루프 체크 ──
        if killswitch.is_full_stop():
            print(f"\n⚠️  [{now:%H:%M:%S}] Killswitch full_stop. 루프 종료.")
            break

        # ── 자체 최대 실행시간 → 정상 종료(핸드오프) ──
        # 하드 타임아웃 강제 종료 시 정리 스텝 스킵 → 거래기록 유실 방지.
        if (epoch_now - loop_start_epoch) >= MAX_LOOP_RUNTIME_SEC:
            print(f"\n[{now:%H:%M:%S}] 최대 실행시간({MAX_LOOP_RUNTIME_SEC // 60}분) 도달 "
                  f"— 정상 종료(핸드오프). 다음 run이 이어받음.")
            break

        # ── 주말 체크 (토·일 = 5, 6) ──
        if now.weekday() >= 5:
            print(f"[{now:%H:%M:%S}] 주말 — 미국장 휴장. 종료.")
            break

        # ── 개장 전: 대기 ──
        if not _time_in_range(t, open_t, close_t):
            # 이미 한 바퀴 돌았다면 (폐장 이후) → 종료
            if closing_done or last_strategy_check > 0:
                print(f"\n[{now:%H:%M:%S}] 미국장 폐장. 루프 종료.")
                break

            # 장외 dead zone: 폐장 후 깬 watchdog run은 다음 개장까지 한참 남음.
            # 의미 없이 2시간 idle하지 말고 즉시 종료. 정당한 pre-open 대기만 허용.
            mins_to_open = _minutes_until_open(now, open_t)
            if mins_to_open > PREOPEN_WAIT_LIMIT_MIN:
                print(f"\n[{now:%H:%M:%S}] 장외 시간 — 개장({open_t.strftime('%H:%M')} KST)까지 "
                      f"{mins_to_open / 60:.1f}h. 대기 없이 종료.")
                break

            # 개장 대기 — 최대 2시간
            waited = epoch_now - wait_start
            if waited > MAX_WAIT_SECONDS:
                print(f"[{now:%H:%M:%S}] 개장 대기 {waited/60:.0f}분 초과. 종료.")
                break

            print(f"[{now:%H:%M:%S}] 미국장 개장 대기 ({open_t.strftime('%H:%M')} KST)")
            time_mod.sleep(60)
            continue

        # ── 폐장 직전 청산 ──
        close_dt = _get_close_datetime(now, close_t)
        minutes_to_close = (close_dt - now).total_seconds() / 60

        if minutes_to_close <= CLOSE_BEFORE_MINUTES and not closing_done:
            print(f"\n[{now:%H:%M:%S}] === 폐장 {minutes_to_close:.0f}분 전 — 포지션 청산 ===")
            close_us_positions(client, dry_run)
            closing_done = True
            _update_journal()
            time_mod.sleep(RISK_CHECK_INTERVAL)
            continue

        if closing_done:
            # 청산 완료 후 폐장까지 대기
            time_mod.sleep(RISK_CHECK_INTERVAL)
            continue

        # ── 리스크 체크 (매 1분) ──
        positions = load_us_positions()
        if positions:
            check_us_risk(client, dry_run)

        # ── 전략 체크 (매 5분, 개장 초반 2분) ──
        early_end = _add_minutes(open_t, EARLY_SESSION_MINUTES)
        is_early = _time_in_range(t, open_t, early_end)
        interval = STRATEGY_CHECK_EARLY if is_early else STRATEGY_CHECK_INTERVAL

        if epoch_now - last_strategy_check >= interval and not bought_today:
            last_strategy_check = epoch_now

            positions = load_us_positions()
            max_pos = cfg.get("max_positions", 2)

            if len(positions) < max_pos:
                phase = "초반" if is_early else "정규"
                print(f"\n[{now:%H:%M:%S}] === US 전략 체크 ({phase}) ===")
                used = run_us_strategy(client, dry_run)
                if used > 0:
                    bought_today = True
                    _update_journal()
            else:
                print(f"  [US] 최대 포지션 유지 ({len(positions)}/{max_pos})")

        # ── 1분 대기 ──
        elapsed = time_mod.time() - epoch_now
        sleep_time = max(1, RISK_CHECK_INTERVAL - elapsed)
        time_mod.sleep(sleep_time)

    # 루프 종료 후 최종 저널 업데이트
    _update_journal()
    print(f"\n[US Night Loop] 종료. {'=' * 40}")


def _minutes_until_open(now: datetime, open_t: dtime) -> float:
    """다음 개장까지 남은 분. 오늘 개장 시각이 이미 지났으면 내일 개장 기준."""
    open_dt = now.replace(hour=open_t.hour, minute=open_t.minute,
                          second=0, microsecond=0)
    if open_dt <= now:
        open_dt += timedelta(days=1)
    return (open_dt - now).total_seconds() / 60


def _get_close_datetime(now: datetime, close_t: dtime) -> datetime:
    """폐장 시각을 datetime으로 변환. 자정 넘는 경우 처리."""
    close_dt = now.replace(hour=close_t.hour, minute=close_t.minute,
                           second=0, microsecond=0)
    # 현재가 22~23시이고 close가 05~06시면 → 다음날
    if now.hour >= 20 and close_t.hour < 12:
        close_dt += timedelta(days=1)
    return close_dt


def _add_minutes(t: dtime, minutes: int) -> dtime:
    """dtime에 분을 더한 결과 반환 (자정 넘김 지원)."""
    total = t.hour * 60 + t.minute + minutes
    total %= 24 * 60
    return dtime(total // 60, total % 60)


def _update_journal() -> None:
    """포트폴리오 저널 빠른 업데이트 (에러 무시)."""
    try:
        from src.journal_quick import main as journal_main
        journal_main()
    except Exception as e:
        log.warning("us_journal_update_failed", error=str(e))


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 미국 ETF 야간 매매 봇")
    parser.add_argument("--dry-run", action="store_true",
                        help="주문 실행 없이 시뮬레이션")
    parser.add_argument("--loop", action="store_true",
                        help="미국장 개장~폐장 연속 감시")
    args = parser.parse_args()

    if args.loop:
        run_loop(args.dry_run)
    else:
        run_once(args.dry_run)


if __name__ == "__main__":
    main()
