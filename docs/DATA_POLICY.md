# 데이터 공개 정책

이 저장소는 **공개**다. 커밋은 영구 기록이고, 푸시하는 순간 누구나 클론·포크할 수 있다.
나중에 파일을 지워도 과거 커밋·포크·캐시에 남는다. 그래서 "올리기 전에 막는 것"이 유일하게 확실한 방어다.

## 분류

| 분류 | 예 | 둘 곳 |
|---|---|---|
| **공개** | 코드(`src/`, `scripts/`), 테스트(`tests/`), 설정 템플릿(`.env.example`, `configs/*.yaml`), 일반 문서(`docs/`, `README.md`) | 이 저장소 |
| **비공개** | 실거래 기록(`trades.csv`, `positions.json`, `ledger.db`), 로그(`logs/`), 학습 상태(`experience.json`, `lgbm_model.pkl` 등 `state/learning/`), 리서치 노트·리포트, 사업 문서(준법 검토·계약서·사업계획) | 비공개 저장소 또는 서버·DB |
| **금지** | 비밀값: KIS 앱키·앱시크릿, 계좌번호, GitHub 토큰, Discord 웹훅, Telegram 봇 토큰, 비밀번호, 인증서·키 파일 | `.env`(로컬) 또는 GitHub Secrets에만. **어떤 저장소에도 커밋 금지** |

> 계좌번호는 문서·예시에도 실번호를 쓰지 않는다. 형식 설명은 `12345678-01`처럼 자리표시로 쓴다.

## 가드

| 장치 | 시점 | 하는 일 |
|---|---|---|
| `.gitignore` | `git add` | 비공개·금지 경로를 기본으로 제외 |
| `.githooks/pre-commit` | 커밋 직전 (로컬) | `scripts/check_public_safety.py --staged` 실행. 위반이면 커밋 중단 |
| `.github/workflows/public-guard.yml` | 모든 push·PR | 같은 스크립트로 변경분 검사. 위반이면 실패 |
| GitHub Secret scanning + Push protection | push 수신 시 | 알려진 토큰 형식이면 push 자체를 거부 (Settings → Code security에서 켜기) |

pre-commit 훅은 클론마다 한 번 켜야 한다.

```bash
git config core.hooksPath .githooks
```

`scripts/check_public_safety.py` 판정 기준:

- **실패 (금지 경로)**: 추가·변경된 파일이 `.env`·`.env.*`(`.env.example` 제외), `*.db`·`*.sqlite*`, `*.log`, `*.pem`·`*.key`,
  `trades.csv`·`positions.json`·`positions.csv`, 또는 `logs/`·`data/`·`private/`·`internal/`·`research_notes/`·`reports/`·`secrets/`·`tokens/` 아래
- **실패 (비밀값 패턴)**: 추가된 줄에 KIS 앱키(`PS` + 34자), `ghp_`·`github_pat_` 토큰, Discord 웹훅 URL, Telegram 봇 토큰,
  실계좌번호 형태(8자리-2자리, `ACCOUNT_NO=`/`CANO` 뒤 8자리). 자리표시(`12345678`, 같은 숫자 반복, 날짜)는 통과
- **경고만**: `state/learning/` 변경 (아래 TODO)
- 삭제는 검사하지 않는다. 지우는 것은 언제나 허용된다
- 오탐이면 그 줄에 `public-safety: allow` 주석을 달고, PR에서 사유를 적는다
- 보고서에는 비밀값을 앞 4자만 남기고 마스킹해서 출력한다 (Actions 로그도 공개다)

전체 감사: `python scripts/check_public_safety.py --all`

## 사고 대응 (비밀값을 푸시했을 때)

1. **먼저 폐기·재발급한다.** KIS 앱키는 KIS Developers에서 재발급, GitHub 토큰은 revoke, 웹훅은 삭제 후 재생성.
   이력에서 지우는 것보다 이것이 먼저다. 공개되는 순간 이미 수집됐다고 가정한다.
2. 파일을 지우고 `.gitignore`에 추가한 커밋을 올린다.
3. 이미 공개된 과거 이력은 **재작성하지 않는다.** 노출된 값은 이미 노출된 것이라 효과가 없고, 기존 클론·포크를 깨뜨린다.
   폐기된 값만 남게 만드는 것이 목표다.

## TODO

- [ ] **봇 재가동 전에 학습 상태(`state/learning/`)를 비공개 저장소로 옮긴다.**
  지금은 봇이 운영 중지 상태(자동 스케줄 제거)라 새 데이터가 들어오지 않으므로 가드에서 경고만 낸다.
  재가동 시 `market-learn.yml`이 이 경로에 커밋하고 `autotrader.yml`·`daily-report.yml`이 읽어 가므로, 비공개 저장소
  체크아웃(토큰은 Secrets) 또는 아티팩트·외부 스토리지로 바꾼 다음 가드를 **경고 → 실패**로 올린다.
- [ ] 투자일지 저장소(kis-trading-journal)의 실거래 내역 공개 여부 결정. 비공개로 돌리면 `autotrader.yml`의
  `raw.githubusercontent.com` 복원 경로도 인증 방식으로 바꿔야 한다.
