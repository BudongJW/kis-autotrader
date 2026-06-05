# kis-autotrader — Claude Code 프로젝트 컨텍스트

이 파일은 Claude Code가 이 프로젝트에 진입할 때 가장 먼저 읽는 컨텍스트다. 사용자의 이전 대화 결정·제약·아키텍처 의도를 그대로 이어받는다.

---

## 프로젝트 한 줄 요약

**한국투자증권(KIS) Open API 기반 주식 자동매매 시스템**. 학습 → 모의투자 검증 → 소액 실전 운영 → 점진적 확장을 목표로 한다.

## 사용자 컨텍스트

- 이미 익숙한 도구: Claude Code, Python, Git
- 처음 다루는 영역: KIS API, 알고리즘 트레이딩

## 핵심 결정 사항 (사용자가 선택)

| 항목 | 선택 | 비고 |
|---|---|---|
| 메인 라이브러리 | 공식 `koreainvestment/open-trading-api` | strategy_builder + backtester까지 묶음으로 활용 |
| 스캐폴드 깊이 | Standard | python-kis 스타일 래퍼 + 골든크로스 예제 + 백테스트 + 모의투자 봇 골격 |
| 초기 전략 | 골든크로스 (이동평균 5/20 교차) | 가장 단순·검증된 패턴부터 |
| 운영 모드 | 모의투자 → 검증 후 소액 실전 | 모의투자에서 1주일 무사 동작 + 백테스트와 괴리 < 30% 확인 전엔 실전 X |

## 절대 원칙 (위반 금지)

1. **`.env`는 절대 커밋하지 않는다.** `.env.example`만 추적. `.gitignore`에 `.env` 명시.
2. **앱키·앱시크릿·계좌번호는 코드에 하드코딩하지 않는다.** `src/config.py`를 통해 환경변수로만 로드.
3. **실전 계좌 주문 코드를 짤 때는 명시적 confirm flag(`--live`)를 거치게 한다.** 기본은 모의투자.
4. **Rate limit 위반은 항상 자동 회피.** 실전 초당 20건, 모의 초당 2건. 다수 종목 모니터링 시 배치 + 딜레이 자동 계산.
5. **거래 기록은 모두 로그 + DB 저장.** 백테스트와 실전 결과 비교를 위해 필수. 한국 세금 신고에도 필요.
6. **레버리지 ETF는 엄격한 가드 하에서만 허용** (2026-06-05 사용자 결정으로 #6 개정. 변동성 손실로 횡보장에서 자본 잠식 위험은 여전하므로 무조건 가드 통과 필요).
   - **레짐 게이트**: BULL + 고확신(HMM bull) + 급락 트리거 NONE + 횡보(sideways) 아님일 때만 진입. CAUTION/BEAR/CRISIS/횡보/급락에선 **레버리지 진입 금지**.
   - **하드 손절**: 일반보다 타이트한 고정 손절(config `leveraged.hard_stop_pct`).
   - **비중 상한**: 자본의 소액 캡(config `leveraged.max_weight`).
   - **모의 우선(#7)**: `leveraged.enabled=false`(또는 dry-run) 기본. 모의·소액 검증 통과 전 실거래 금지.
   - 레버리지 **인버스**(곱버스)는 계속 금지 — 위 가드는 추세추종 레버리지 롱에만 적용.
7. **백테스트 결과를 곧이곧대로 믿지 않는다.** 항상 out-of-sample 검증 + 모의투자 1주일 이상 병행.

## 디렉토리 구조

```
kis-autotrader/
├── .claude/                  # Claude Code 설정 (settings.local.json 등)
├── .env.example              # 환경변수 템플릿 (실제 .env는 git 제외)
├── CLAUDE.md                 # 이 파일
├── README.md                 # 인간용 빠른 시작 가이드
├── pyproject.toml            # 의존성 + 패키징
├── requirements.txt          # pip 의존성 (간단 설치용)
├── configs/                  # 전략 .yaml, 백테스트 설정
│   └── strategy.yaml
├── src/
│   ├── config.py             # 환경변수 + 설정 로드
│   ├── kis_auth.py           # KIS API 인증 (토큰 발급·갱신)
│   ├── kis_client.py         # REST 호출 래퍼 (시세·주문·잔고)
│   ├── strategies/
│   │   ├── base.py           # BaseStrategy 추상 클래스
│   │   └── golden_cross.py   # MA5/MA20 골든크로스 예제
│   ├── backtest/
│   │   └── runner.py         # 백테스트 실행기
│   ├── bot/
│   │   └── runner.py         # 실시간 봇 실행기 (모의/실전 분기)
│   └── utils/
│       ├── rate_limit.py     # 초당 20건 자동 분산
│       └── logger.py         # 구조화 로깅
├── tests/                    # pytest
├── notebooks/                # Jupyter (탐색·시각화)
│   └── 01_first_quote.ipynb
├── docs/                     # 설계 문서·튜토리얼
└── scripts/                  # 일회성 스크립트 (토큰 발급 점검 등)
```

## KIS API 핵심 메모

### 인증 흐름
1. KIS Developers 가입 → 앱키(`APPKEY`) + 앱시크릿(`APPSECRET`) 발급
2. `POST /oauth2/tokenP` → 액세스 토큰 (24시간 유효)
3. 모든 REST 호출에 `Authorization: Bearer <token>` 헤더 부착
4. 토큰 만료 전 자동 갱신 (`src/kis_auth.py`가 담당)

### 환경 분기
- **모의투자**: 베이스 URL `https://openapivts.koreainvestment.com:29443`
- **실전투자**: 베이스 URL `https://openapi.koreainvestment.com:9443`
- `src/config.py`의 `MODE` 환경변수로 분기 (`paper` / `live`)

### Rate Limit
- 실전 초당 20건, 모의 초당 2건
- 초과 시 429 응답 → 5분 쿨다운 발생 가능
- `src/utils/rate_limit.py`의 토큰 버킷으로 자동 회피

### 자주 쓰는 엔드포인트
- 현재가: `GET /uapi/domestic-stock/v1/quotations/inquire-price`
- 일별 시세: `GET /uapi/domestic-stock/v1/quotations/inquire-daily-price`
- 매수/매도: `POST /uapi/domestic-stock/v1/trading/order-cash`
- 잔고 조회: `GET /uapi/domestic-stock/v1/trading/inquire-balance`
- 실시간 시세 (WebSocket): `wss://ops.koreainvestment.com:21000`

## 개발 워크플로우

### 새 전략 추가 시
1. `src/strategies/<name>.py`에 `BaseStrategy` 상속 클래스 작성
2. `generate_signal(market_data) -> Signal` 구현
3. `tests/test_<name>.py`에 단위 테스트
4. `src/backtest/runner.py`로 과거 데이터 검증
5. 모의투자에서 1주일 실전 운영
6. 결과 괴리 < 30%면 소액 실전 진입 검토

### 디버깅 시 우선순위
1. 로그 확인 (`logs/` 디렉토리, 구조화 JSON)
2. KIS API 응답 코드 확인 (`rt_cd`, `msg_cd`)
3. Rate limit 카운터 확인
4. 토큰 만료 여부 확인

## Claude Code에게 (행동 가이드)

- 실전 계좌 변경(`MODE=live`)이 필요한 작업은 **반드시 사용자에게 명시적 확인**받고 진행
- 모의투자에서 검증되지 않은 코드를 실전 모드로 실행 제안 금지
- 백테스트 결과 보고 시 항상 **MDD·Sharpe·승률**을 함께 제시. 수익률만 강조하지 않는다
- 매매 로직 변경 시 항상 **단위 테스트 추가**. 룰 기반 매매는 테스트 가능한 영역
- Rate limit 관련 코드 변경 시 `tests/test_rate_limit.py` 통과 확인
- 한국 세금(분리과세 22%, 250만 원 공제)을 고려해 거래 기록 보관 기능 우선
- 사용자가 "결과만 보여줘" 식으로 요청해도 **수치의 신뢰도(샘플 기간, out-of-sample 여부)**를 함께 표기

## 외부 참고 (필요 시 검색)

- 공식 GitHub: https://github.com/koreainvestment/open-trading-api
- KIS Developers 포털: https://apiportal.koreainvestment.com/intro
- python-kis (대안 라이브러리): https://github.com/Soju06/python-kis
- 공식 AI 확장 (MCP): https://github.com/koreainvestment/kis-ai-extensions
- 한국어 튜토리얼: https://wikidocs.net/159296
- TG's Blog (실제 개발 일지): https://tgparkk.github.io

## 현재 진행 상태

- [x] 프로젝트 스캐폴드 생성
- [x] CLAUDE.md / README.md / 환경 설정 파일 작성
- [x] 골든크로스 예제 전략 작성
- [x] 백테스트 러너 골격
- [x] 모의투자 봇 골격
- [ ] **다음**: KIS Developers 가입 + 앱키 발급 → `.env` 채움
- [ ] 첫 시세 조회 (notebooks/01_first_quote.ipynb)
- [ ] 골든크로스 백테스트 실행
- [ ] 모의투자 1주일 운영
- [ ] 결과 분석 + 전략 개선
