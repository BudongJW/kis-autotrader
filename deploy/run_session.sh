#!/usr/bin/env bash
# 한국 VPS 상시가동용 세션 래퍼 — GitHub Actions autotrader.yml의
# 복원→실행→영속화 흐름을 그대로 옮긴 것. systemd 타이머가 개장 시각에 호출.
#
# 사용: run_session.sh kr   # 한국장 (--loop)
#       run_session.sh us   # 미국장 야간 (--loop)
#
# 전제 레이아웃 (setup.sh가 구성):
#   $REPO            = ~/kis-autotrader        (이 repo)
#   $REPO/journal    = kis-trading-journal 클론 (영속화 대상, .gitignore됨)
#   $REPO/.env       = KIS 키 + JOURNAL_PAT
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
MODE="${1:-kr}"
export PYTHONPATH="$REPO"
export PYTHONIOENCODING=utf-8
JRAW="https://raw.githubusercontent.com/BudongJW/kis-trading-journal/main/state"
mkdir -p logs

echo "[$(date '+%F %T %Z')] === 세션 시작 (mode=$MODE) ==="

# 1) 최신 코드·학습 상태 동기화 (market-learn이 state/learning에 커밋)
git pull --rebase --autostash origin main || echo "git pull 스킵"

# 2) 상태 복원 (positions/trades canonical from journal, learning from repo)
curl -fsSL "$JRAW/trades.csv"        -o logs/trades.csv        && echo "trades.csv 복원"        || echo "trades.csv 없음"
curl -fsSL "$JRAW/positions.json"    -o logs/positions.json    && echo "positions.json 복원"    || echo "positions.json 없음"
curl -fsSL "$JRAW/us_positions.json" -o logs/us_positions.json && echo "us_positions.json 복원" || echo "us_positions 없음"
[ -d state/learning ] && cp -f state/learning/* logs/ 2>/dev/null && echo "학습 상태 복원" || true

# 3) 봇 실행 (장 시작~마감까지 --loop, 자체 종료)
if [ "$MODE" = "us" ]; then
    python -m src.bot.night_run --loop || echo "야간봇 종료(코드 $?)"
else
    python -m src.bot.single_run --loop || echo "한국봇 종료(코드 $?)"
fi

# 4) 상태 영속화 → journal repo (trades·positions·portfolio)
JDIR="$REPO/journal"
if [ -d "$JDIR/.git" ]; then
  git -C "$JDIR" config user.name  "kis-autotrader-bot"
  git -C "$JDIR" config user.email "bot@kis-autotrader"
  for i in 1 2 3; do
    git -C "$JDIR" fetch origin main && git -C "$JDIR" reset --hard origin/main
    python -m src.merge_trades "$JDIR/state/trades.csv" logs/trades.csv || true
    cp "$JDIR/state/trades.csv" logs/trades.csv 2>/dev/null || true
    [ -s logs/positions.json ] && cp logs/positions.json "$JDIR/state/positions.json" || true
    [ -s logs/us_positions.json ] && cp logs/us_positions.json "$JDIR/state/us_positions.json" || true
    python -m src.journal_quick || true   # journal/_data/portfolio.json 재생성
    git -C "$JDIR" add -A
    if git -C "$JDIR" diff --cached --quiet; then echo "영속화: 변경 없음"; break; fi
    git -C "$JDIR" commit -m "auto: $MODE session $(date +'%F %H:%M')" || true
    if git -C "$JDIR" push; then echo "영속화 푸시 완료"; break; else echo "푸시 거부 — 재시도 $i"; fi
  done
else
  echo "⚠️ journal 클론($JDIR) 없음 — 영속화 스킵 (setup.sh 재실행 필요)"
fi

echo "[$(date '+%F %T %Z')] === 세션 종료 ==="
