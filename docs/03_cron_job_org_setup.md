# cron-job.org로 정시 트리거 설정

## 왜 필요한가

GitHub Actions의 `schedule:` cron은 **공식적으로 정시 발화를 보장하지 않습니다.**
공식 docs 인용: *"Schedule events can be delayed during periods of high load.
High load times include the start of every hour. If the load is sufficiently
high enough, some queued jobs may be dropped."*

실측 데이터:
- 1/10이 5분+ 지연
- 1/50이 15분+ 지연
- worst case 30~60분 지연 (특히 UTC 정각)

봇이 09:00 KST 정각에 깨어나야 하는데 09:30이 돼서야 발화하면 **변동성 돌파
신호를 통째로 놓칩니다.** 이 문제를 cron-job.org가 우리 워크플로의
`workflow_dispatch` API를 정시에 호출해서 해결합니다.

cron-job.org는:
- 무료 (카드 등록 X)
- 무제한 cronjob
- 분 단위 정시 ±몇 초 정확도
- POST + Bearer token 헤더 지원
- 30초 타임아웃

## 셋업 단계

### 1. GitHub Personal Access Token 발급

cron-job.org가 `workflow_dispatch` API를 부르려면 GitHub PAT가 필요합니다.

1. https://github.com/settings/personal-access-tokens/new 접속
2. **Fine-grained personal access token** 선택
3. 설정:
   - **Token name**: `cron-job.org-autotrader-dispatch`
   - **Resource owner**: 본인 (BudongJW)
   - **Expiration**: 1년 (만료되면 갱신)
   - **Repository access**: `Only select repositories` → `BudongJW/kis-autotrader` 만 선택
   - **Repository permissions**:
     - `Actions`: **Read and write** (workflow_dispatch에 필요)
     - `Metadata`: Read-only (자동)
   - 나머지는 No access
4. **Generate token** 클릭
5. `github_pat_xxxxxxxxxxxx...` 토큰을 복사 → 다음 단계에서 사용
6. **토큰은 한 번만 보입니다.** 잃어버리면 재발급.

**보안**: 이 토큰은 kis-autotrader 레포의 워크플로 dispatch 권한만 가집니다.
다른 레포나 secrets에는 접근 불가.

### 2. cron-job.org 가입

1. https://cron-job.org 접속
2. **Sign up** (이메일 + 비밀번호만, 카드 X)
3. 이메일 인증

### 3. Cronjob 등록

대시보드에서 **+ Create cronjob** 클릭.

#### 한국장 시작 — 09:00:00 KST 정각

| 필드 | 값 |
|---|---|
| **Title** | `KIS 한국장 시작 09:00 KST` |
| **URL** | `https://api.github.com/repos/BudongJW/kis-autotrader/actions/workflows/autotrader.yml/dispatches` |
| **Schedule** | `Every day` `Monday-Friday` at `00:00` UTC (= 09:00 KST) |
| **Timezone** | `UTC` (cron-job.org 인터페이스에서 KST 설정해도 OK) |
| **Request method** | `POST` |
| **Request headers** (Advanced 탭) | 아래 참조 |
| **Request body** | `{"ref":"main","inputs":{"mode":"loop","dry_run":"false"}}` |

**Request headers** (3개 추가):
```
Accept: application/vnd.github+json
Authorization: Bearer github_pat_여기에_토큰_붙여넣기
X-GitHub-Api-Version: 2022-11-28
```

**Save** → **Enable**

#### 추가로 등록할 정시 트리거 (권장)

| 시간 (KST) | 시간 (UTC) | 워크플로 | 이유 |
|---|---|---|---|
| 08:55:00 월~금 | 23:55 일~목 | autotrader.yml | 토큰 사전 갱신 |
| **09:00:00 월~금** | **00:00 월~금** | **autotrader.yml** | **장 시작 정각** |
| 09:00:30 월~금 | 00:00:30 월~금 | autotrader.yml | 09:00 누락 백업 |
| 15:25:00 월~금 | 06:25 월~금 | autotrader.yml | 마감 정리 |
| **22:30:00 월~금 (서머)** | **13:30 월~금** | **us-night-trader.yml** | **미국장 시작 (서머)** |
| **23:30:00 월~금 (동절)** | **14:30 월~금** | **us-night-trader.yml** | **미국장 시작 (동절)** |

서머/동절 둘 다 등록해두면 자동으로 적절한 시점에 발화. 봇이 내부에서
`is_us_market_hours()`로 비개장 시 즉시 종료하므로 양쪽 다 등록 안전.

#### us-night-trader.yml URL
```
https://api.github.com/repos/BudongJW/kis-autotrader/actions/workflows/us-night-trader.yml/dispatches
```

### 4. 테스트

cron-job.org 대시보드에서 등록한 잡 옆 **"Run now"** 버튼 클릭.

성공 시:
- cron-job.org에 `204 No Content` 응답 (GitHub API 정상)
- GitHub Actions에 새 run 즉시 등록 (workflow_dispatch 트리거)

실패 시:
- `401 Unauthorized` → PAT 잘못됨 (오타, 만료, 권한 부족)
- `404 Not Found` → URL 오타 또는 PAT가 해당 레포 권한 없음
- `422 Unprocessable Entity` → body의 `ref` 또는 `inputs` 잘못

### 5. 모니터링

- cron-job.org 대시보드의 **History** 탭에서 발화 기록 확인
- 실패 알림 이메일 자동 발송 (기본 활성)
- 한 달에 한 번 정도 PAT 만료 임박 여부 확인

## PAT 갱신 (1년 후)

1. https://github.com/settings/personal-access-tokens 접속
2. 만료 임박 토큰 옆 **Regenerate token**
3. 새 토큰 복사
4. cron-job.org에서 모든 잡의 Authorization 헤더 업데이트

## 보안 체크리스트

- [ ] PAT는 fine-grained (classic이 아님)
- [ ] PAT 권한은 `Actions:write` + `Metadata:read` 만
- [ ] PAT는 `BudongJW/kis-autotrader` 1개 레포만 접근
- [ ] PAT 만료일 1년 이내
- [ ] cron-job.org 계정 2FA 활성화 (Settings → Account → 2FA)
- [ ] PAT를 git에 커밋한 적 없음 (cron-job.org 웹 UI에만 입력)

## 트러블슈팅

### cron-job.org가 발화는 했는데 GitHub Actions에 run이 안 보임
- GitHub API 응답이 204인지 cron-job.org History에서 확인
- 204인데 안 보이면 GitHub Actions 자체 큐 지연 (드물지만 발생)

### 200 OK인데 workflow가 안 돌아감
- workflow YAML에 `on: workflow_dispatch:` 블록이 있는지 확인 (있음 — autotrader.yml line 14, us-night-trader.yml line 28)
- 브랜치(`ref: main`)가 실제로 존재하는지 확인

### "Workflow does not have 'workflow_dispatch' trigger"
- 워크플로 파일 push 직후 발생 가능. GitHub가 metadata 인덱싱하는 데 1~2분 걸림. 잠시 대기 후 재시도.
