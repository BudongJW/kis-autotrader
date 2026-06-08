#!/usr/bin/env bash
# 한국 VPS 1회 셋업 — 의존성·타임존·journal 클론·systemd 타이머 설치.
# 사용: repo 루트에서  bash deploy/setup.sh
# 전제: Ubuntu/Debian 계열, sudo 권한, .env는 직접 채워둘 것(아래 8단계).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(whoami)"
cd "$REPO"
echo "REPO=$REPO  USER=$RUN_USER"

echo "== 1. 시스템 패키지 =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl tzdata

echo "== 2. 타임존 Asia/Seoul =="
sudo timedatectl set-timezone Asia/Seoul || true
timedatectl | grep "Time zone" || true

echo "== 3. 파이썬 의존성 =="
python3 -m pip install --user -r requirements.txt

echo "== 4. journal repo 클론 (영속화 대상) =="
if [ ! -d "$REPO/journal/.git" ]; then
  if [ -z "${JOURNAL_PAT:-}" ]; then
    echo "JOURNAL_PAT 환경변수가 없습니다. .env의 토큰으로 클론합니다."
    # shellcheck disable=SC1091
    [ -f .env ] && set -a && . ./.env && set +a || true
  fi
  git clone "https://${JOURNAL_PAT:-}@github.com/BudongJW/kis-trading-journal.git" journal \
    || git clone "https://github.com/BudongJW/kis-trading-journal.git" journal
fi
grep -qxF "journal/" .gitignore 2>/dev/null || echo "journal/" >> .gitignore

echo "== 5. .env 확인 =="
if [ ! -f .env ]; then
  echo "⚠️  .env 없음. .env.example 복사 후 KIS 키·JOURNAL_PAT를 채우세요:"
  echo "    cp .env.example .env && nano .env"
  echo "   (MODE=live, APPKEY, APPSECRET, 계좌번호, JOURNAL_PAT 필수)"
fi

echo "== 6. systemd 유닛 설치(__USER__/__REPO__ 치환) =="
TMP=$(mktemp -d)
for f in kis-kr.service kis-us.service; do
  sed -e "s|__USER__|$RUN_USER|g" -e "s|__REPO__|$REPO|g" "deploy/$f" > "$TMP/$f"
  sudo cp "$TMP/$f" "/etc/systemd/system/$f"
done
sudo cp deploy/kis-kr.timer deploy/kis-us.timer /etc/systemd/system/
sudo systemctl daemon-reload

echo "== 7. 타이머 활성화 =="
sudo systemctl enable --now kis-kr.timer kis-us.timer
systemctl list-timers 'kis-*' --no-pager || true

cat <<'DONE'

== 8. 남은 수동 단계 ==
  1) .env에 KIS 키·계좌번호·JOURNAL_PAT 채우기 (아직 안 했다면)
  2) 즉시 테스트(장중 아니어도 복원/영속화는 동작):
       bash deploy/run_session.sh kr   # 한국장 (또는 us)
  3) 로그 확인:        journalctl -u kis-kr.service -f
  4) 다음 타이머 확인: systemctl list-timers 'kis-*'

  타이머는 평일 KR 08:55 / US 22:25(+23:25 윈터) KST 자동 실행됩니다.
  봇은 마감 후 자체 종료하고, 다음 개장에 다시 시작됩니다.
DONE
