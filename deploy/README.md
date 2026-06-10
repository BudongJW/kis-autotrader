# 한국 VPS 상시가동 배포 (인프라 1-B)

GitHub Actions(해외 IP·스케줄 드랍·6h 한도)의 한계를 없애고, **한국 IP에서 24시간
상시가동**으로 봇을 돌리기 위한 패키지. 6-08 폭락 때 KIS API 전면 타임아웃으로
봇이 거래 불능이었던 근본 원인을 해소한다.

## 무엇이 들어있나
| 파일 | 역할 |
|---|---|
| `run_session.sh` | 세션 래퍼: 상태 복원 → 봇 `--loop` 실행 → journal로 영속화 (워크플로 미러) |
| `kis-kr.service` / `kis-kr.timer` | 한국장: 평일 08:55 KST 시작 |
| `kis-us.service` / `kis-us.timer` | 미국장 야간: 평일 22:25(+23:25 윈터) KST 시작 |
| `setup.sh` | 1회 부트스트랩(의존성·타임존·journal 클론·타이머 설치) |

GitHub Actions 워크플로는 **그대로 둬도 됩니다**(백업 경로). VPS가 주 경로가 되면
Actions 크론을 꺼서 중복 매매를 피하세요(아래 ⚠️ 참고).

## 빠른 시작

```bash
# 1) 서울 리전 VPS 생성 (예: Vultr Seoul / AWS ap-northeast-2 / Naver Cloud)
#    Ubuntu 22.04+, 최소 1vCPU/1GB. SSH 접속.

# 2) repo 클론
git clone https://github.com/BudongJW/kis-autotrader.git
cd kis-autotrader

# 3) .env 작성 (절대 커밋 금지 — CLAUDE.md #1)
cp .env.example .env
nano .env       # MODE=live, APPKEY, APPSECRET, 계좌번호, JOURNAL_PAT 채우기

# 4) 셋업 (의존성·타임존·journal 클론 + preflight 검증 후 타이머 설치)
bash deploy/setup.sh
#    → setup.sh가 scripts/preflight.py를 돌려 ALL PASS일 때만 타이머를 켭니다.

# 5) (선택) 검증만 따로: live 켜기 전 read-only go/no-go 점검
python3 scripts/preflight.py          # 6항목 PASS/FAIL — 주문 없음

# 6) 즉시 1회 테스트 (장외여도 복원·영속화는 동작)
bash deploy/run_session.sh kr

# 7) 동작 확인
journalctl -u kis-kr.service -f       # 실시간 로그
systemctl list-timers 'kis-*'         # 다음 실행 시각
```

## 안전 점검 (preflight)

`python3 scripts/preflight.py` 는 **라이브 켜기 전** 다음을 읽기 전용으로 확인하고
go/no-go를 줍니다 (주문 안 함):
1. `.env`/인증정보 로드 · 2. KIS 토큰 발급 · 3. 잔고 조회(연결) ·
4. 시세 조회 · 5. journal 영속화 경로 · 6. 결정 파이프라인.
**ALL PASS여야 systemd 타이머를 켜세요.** (setup.sh가 자동으로 이 게이트를 적용)

## 운영

- **로그:** `journalctl -u kis-kr.service` / `-u kis-us.service`
- **수동 실행:** `bash deploy/run_session.sh kr|us`
- **타이머 중지:** `sudo systemctl disable --now kis-kr.timer kis-us.timer`
- **코드 업데이트:** `git pull` (run_session.sh가 매 세션 자동 `git pull`도 함)
- **대시보드:** 그대로 https://budongjw.github.io/kis-trading-journal (journal 푸시로 갱신)

## ⚠️ 중복 매매 방지 (중요)

VPS와 GitHub Actions가 **동시에 같은 계좌로 매매하면 중복 주문** 위험이 있습니다.
VPS를 주 경로로 쓰면 **Actions 크론을 비활성화**하세요:
- `kis-autotrader/.github/workflows/autotrader.yml`의 `schedule:` 블록 주석 처리
  (또는 GitHub Actions UI에서 워크플로 Disable). `us-night-trader.yml`도 동일.
- 또는 한쪽만 `MODE=live`, 다른 쪽은 `--dry-run`으로 운영.

## DST(서머타임) 메모

- 미국 서머타임(3~11월): 미국장 22:30 KST → `kis-us.timer`의 22:25가 맞음.
- 윈터타임(11~3월): 23:30 KST → 23:25가 맞음. 두 시각을 모두 등록해 뒀고,
  이른 쪽은 `night_run`의 개장대기 한도 초과로 자체 종료되어 안전합니다.
- `configs/strategy.yaml`의 `us_session.summer_time` 플래그도 시즌에 맞게 유지.

## 보안 (CLAUDE.md 절대원칙)

- `.env`·KIS 키·`JOURNAL_PAT`는 **절대 커밋 금지**. VPS에만 둡니다.
- VPS는 SSH 키 인증 + 방화벽(22번만) 권장. KIS 키가 있는 서버이므로 접근 최소화.
