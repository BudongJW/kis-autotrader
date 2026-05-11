# 00 · 상세 셋업 가이드

집에서 처음 진입할 때 따라가는 체크리스트.

## 사전 준비

- [ ] Python 3.11 이상 설치
- [ ] Git 설치
- [ ] 한국투자증권 일반 계좌 (계좌가 이미 있어야 모의투자 신청 가능)
- [ ] (선택) VS Code + Python 확장
- [ ] (선택) Claude Code CLI 설치 — `npm install -g @anthropic-ai/claude-code`

## 1. 저장소 클론

```bash
git clone https://github.com/BudongJW/kis-autotrader.git
cd kis-autotrader
```

## 2. Python 가상환경 + 의존성

```bash
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Windows cmd
.venv\Scripts\activate.bat
# macOS/Linux
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

검증:
```bash
python -c "import requests, pandas, structlog; print('OK')"
```

## 3. KIS Developers 가입

1. https://apiportal.koreainvestment.com/intro 접속
2. 회원가입 (한국투자증권 계좌 보유자 한정)
3. **앱 등록** 페이지에서 새 앱 추가
   - 앱 이름: 자유 (예: `kis-autotrader-paper`)
   - 콜백 URL: 사용 안 함, 임의값 가능
4. 앱키(APPKEY)와 앱시크릿(APPSECRET) 복사

## 4. 모의투자 계좌 신청

1. 한투 HTS 또는 MTS 로그인
2. 메뉴: **모의투자 → 모의투자 신청**
3. 종목 선택 (보통 주식·선물 신청)
4. 다음 영업일에 모의 계좌번호 발급
5. **모의투자용 앱키도 별도 발급** (위 3단계 반복, 앱 이름만 다르게)

## 5. 환경변수 설정

```bash
cp .env.example .env
```

`.env` 열어 다음 값 채우기:

| 변수 | 값 |
|---|---|
| `MODE` | `paper` (처음에는 항상 모의) |
| `KIS_VIRTUAL_APPKEY` | 모의용 앱키 |
| `KIS_VIRTUAL_APPSECRET` | 모의용 앱시크릿 |
| `KIS_APPKEY` | 실전용 앱키 (당분간 안 씀, 채워둬도 OK) |
| `KIS_APPSECRET` | 실전용 앱시크릿 |
| `KIS_HTSID` | 본인 한투 HTS ID |
| `KIS_ACCOUNT_NO` | 모의계좌 번호 앞 8자리 |
| `KIS_ACCOUNT_PROD_CODE` | `01` (기본) |

> ⚠️ `.env`는 절대 커밋되지 않도록 `.gitignore`에 등록되어 있습니다. 그래도 한번 확인하세요: `git check-ignore .env`

## 6. 인증 점검

```bash
python scripts/check_auth.py
```

기대 출력:
```
📡 KIS Developers 인증 점검 (mode=paper)
   Base URL: https://openapivts.koreainvestment.com:29443
   계좌:     12345678-01

✅ 토큰 발급 성공 (만료: 2026-05-12 11:30)
✅ 삼성전자 현재가: 71,500원

🎉 모든 점검 통과. 이제 백테스트·봇을 실행해도 됩니다.
```

실패 시:
- `❌ 환경변수 누락`: `.env` 다시 확인
- `❌ 토큰 발급 실패`: 앱키/시크릿이 모드에 맞는지 (paper 모드에 실전 키 넣지 않았는지)
- `❌ 시세 조회 실패`: 모의투자 계좌가 승인 완료되었는지, 장 시간 외인지 (장외엔 일부 응답 비어있음)

## 7. 첫 노트북

```bash
jupyter notebook notebooks/01_first_quote.ipynb
```

위에서 아래로 셀 하나씩 실행. 마지막 셀까지 통과하면 다음 단계로.

## 8. Claude Code 실행

```bash
# 프로젝트 루트에서
claude
```

Claude Code가 `CLAUDE.md`를 자동으로 읽고, 이전 대화에서의 결정사항·진행 상태를 이어받습니다. 첫 메시지로:

> "CLAUDE.md 읽고 현재 상태 요약해줘. 다음으로 뭘 하면 좋아?"

라고 물어보면 좋습니다.

## 9. 첫 백테스트

```bash
python -m src.backtest.runner --strategy golden_cross --symbol 005930 --from 2023-01-01 --to 2024-12-31
```

> 현재 `load_history()`는 더미 데이터. 실제 백테스트 전 KIS API 또는 `pykrx`로 교체해야 의미 있는 결과 나옴. Claude Code에게 "load_history를 KIS API 또는 pykrx로 채워줘"라고 부탁하면 됩니다.

## 10. 모의투자 봇 실행 (dry-run)

주문 안 보내고 신호만 출력:
```bash
python -m src.bot.runner --strategy golden_cross --symbol 005930 --dry-run
```

실제 모의주문까지 보내려면 `--dry-run` 빼고:
```bash
python -m src.bot.runner --strategy golden_cross --symbol 005930
```

## 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `Rate Limit Exceeded` | rate_limit.py가 작동 안 함. `src/utils/rate_limit.py` 확인 |
| 토큰 발급 시 401 | 앱키 잘못 입력 / 모의 ↔ 실전 키 혼동 |
| 일봉 응답 비어있음 | 장외 시간 / 종목코드 오타 / 모의투자 계좌 미승인 |
| `ModuleNotFoundError: src` | 프로젝트 루트가 아닌 곳에서 실행. `cd kis-autotrader` 후 재실행 |
| Jupyter에서 src import 실패 | 노트북 1번 셀(`sys.path`) 다시 실행 |
