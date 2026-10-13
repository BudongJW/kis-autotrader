# StockPawn (주식 전당포) — Claude Code 프로젝트 컨텍스트

주식을 담보로 받아 → 가치 평가 → 대출금 산정 → 이자(연 20% 단리) → 담보비율 모니터링 → 처분(반대매매)까지
자동화하는 **백오피스 MVP**. 원 스펙은 [`SPEC.md`](SPEC.md) — 규칙·파라미터의 1차 출처는 그 파일이고,
이 파일은 **구현하면서 내린 해석·결정과 진행 상태**를 기록한다. 상위 저장소(kis-autotrader)의 KIS 클라이언트는
운영 전환 시 시세·주문 어댑터로 재사용할 예정이다.

## 절대 원칙 (SPEC 8장 + 상위 프로젝트 원칙)

1. **금액은 Decimal만.** float 금지 (`app.money.D`·`DecimalType`이 float를 거부한다).
2. **총 이자율(약정 + 연체가산 + 수수료) ≤ 연 20%.** `config.validate_rates`가 import 시점에 강제.
   연체 가산이자·중도상환수수료·선이자 공제는 전부 0/금지. 대주가 받는 새 수수료 항목 추가 금지.
3. **복리 금지.** 이자 기준은 원금 잔액뿐. `services.accrue_loan`이 "적수 ≤ 최초원금 × 경과일수"와 법정 상한을 매번 검사.
4. **실주문 기본 비활성.** `BROKER=mock` 기본, `BROKER=kis`는 `ENABLE_LIVE_ORDERS=true`까지 있어야 생성된다(그래도 stub).
   대부업 등록·증권사 제휴 전에는 실주문 코드를 활성화하지 않는다.
5. **개인정보 최소 저장.** 주민번호 등 저장 금지, KYC는 외부 인증 결과 플래그(`kyc_status`)만.
6. **계산 로직 변경 시 테스트 필수.** `tests/`의 이자·평가·리스크·처분 테스트는 스펙 수치를 그대로 박아 둔 회귀 기준이다.

## 디렉터리

```
stockpawn/
├── SPEC.md / CLAUDE.md / README.md / pyproject.toml / .env.example
├── app/
│   ├── config.py          2장 파라미터 (Decimal) + 런타임 env + import 시점 검증
│   ├── money.py clock.py business_days.py notifier.py
│   ├── models.py          3장 테이블 (+ DecimalType, KSTDateTime)
│   ├── db.py deps.py schemas.py main.py scheduler.py
│   ├── services.py        DB 상태 + 엔진 + 어댑터 오케스트레이션 (상태 변경은 여기서만)
│   ├── engines/           순수 함수: interest / valuation / risk / liquidation
│   ├── adapters/price/    PriceSource, PykrxSource, KISSource(stub), InMemoryPriceSource
│   ├── adapters/broker/   BrokerAdapter, MockBroker, KISBroker(stub)
│   ├── routers/           borrowers / valuations / loans / dashboard / admin
│   └── static/dashboard.html
├── tests/                 pytest (네트워크 없음 — pykrx는 가짜 모듈 주입)
└── scripts/simulate.py    과거 시세 리플레이 → 마진콜/처분/손익 표
```

## 구현 결정 (스펙이 열어 둔 부분의 해석)

**이자 (2.2)**
- 적수(積數) 방식: 누적이자 = floor(Σ일별 원금잔액 × 연이율 / 365), 일별 적립액 = 누적이자의 차분.
  → 1,000만 원 30일 = 164,383원, 365일 = 정확히 2,000,000원 (매일 반올림하면 둘 다 틀린다).
- 한편넣기: 대출일 포함·상환일 제외. 00:05 잡이 **전일분**까지 적립, 상환·처분 직전엔 어제까지 catch-up.
- 상환 충당: 이자 → 원금 (민법 479조). 잔액 초과 상환은 422로 거절.

**평가 (2.1)**
- 유동성 한도(20일 평균 거래대금 × 10%)는 **헤어컷 전** 시가에 먼저 적용 → 반대 순서보다 항상 같거나 보수적.
- 헤어컷 h = min(0.5, σ × 0.5)를 소수 4자리 **올림**. σ = 최근 60개 일간 로그수익률 표본표준편차 × √252.
- 등급: A = KOSPI & 시총 ≥ 5조 & σ ≤ 40% (KOSPI200 편입은 미확인 — 시총으로 근사) /
  B = 시총 ≥ 5,000억 & σ ≤ 60% / C = 그 외 & σ ≤ 100% /
  X = 관리종목·거래정지·정리매매·스팩·운영자 차단·KONEX 등·이력 < 20일·σ > 100%·거래대금 0.
  시가총액이나 시장 구분을 모르면(KRX 로그인 없음) 최고 C. 운영자 지정 등급(`tier_floor`)은 하향만 반영.
- pykrx로 관리종목·정리매매를 알 수 없다 → `PUT /admin/stocks/{code}` `is_blocked`로 운영자가 차단.

**리스크 (2.3)**
- 담보비율 = 시가 합계(헤어컷 미적용) / (원금 잔액 + 미수이자). 임계값 비교는 정밀값, 표시는 소수 4자리 절사.
- 마진콜 유예: 발생일 T + 2영업일의 **장 마감(15:30)** 까지. 만료는 그날 15:40 장후 평가에서 잡힌다.
- 만기 + 7일 "경과" = 만기 M이면 M+8일부터 처분 사유.
- `LIQUIDATION_PENDING`은 sticky: 일시 반등으로 되돌리지 않는다. 벗어나는 길은 완제(CLOSED) 또는 처분 진행.
- 장중 평가는 종목 캐시(등급·헤어컷)를 쓰고, 등급 재산정은 장후 1회(`refresh_tiers`). 시세 조회 실패 시 상태 불변.
- 영업일: pykrx 실제 거래일(과거) + exchange_calendars XKRX(미래, 선택 설치) + `EXTRA_HOLIDAYS`, 실패 시 주말 폴백.

**처분 (2.4)**
- 모드: ratio < 100% → FULL(전량) / 만기 경과·tier X·수동 → PAYOFF(원리금 회수분) / < 120%·유예 만료 → PARTIAL(150% 회복분).
- 수량: PAYOFF q = ceil(D / (F·(1−c))) — 스펙의 ceil((D + 예상수수료)/F)를 닫힌 형태로 푼 것. F = 전일 종가 × 0.97.
  PARTIAL q = ceil((1.5·D − V) / (1.5·F·(1−c) − P)), PAYOFF 수량 상한. 위험 담보(tier X→C→B→A, 헤어컷 큰 순)부터.
- 매도비용률 c = 0.25%(증권거래세 + 위탁수수료 추정). 제3자 실비라 이자로 보지 않았다 — **운영 전 법률 검토 항목**.
- 정산: 순매도대금 → 이자 → 원금 → 잉여금(차주 반환). 담보를 다 팔고도 남은 채무는 `shortfall`(미수채권) 기록 후 CLOSED.
- 처분 주문은 거래일 08:45(장 시작 동시호가 접수). 체결 실패(거래정지)면 PENDING 유지 → 다음 거래일 재시도.
- 운영자 수동 처분(`POST /loans/{id}/liquidate`)은 즉시 PAYOFF 실행 (MVP — 운영에선 다음 동시호가).
- 주식 결제 T+2는 MVP에서 무시(체결 즉시 상계).

**인프라**
- SQLite에선 Decimal을 TEXT로, datetime을 KST 벽시계로 저장(SQLAlchemy 버전별 차이 제거). 집계는 파이썬에서.
- 금액 변경 진입점은 프로세스 락(`services.serialized`)으로 직렬화 — 다중 프로세스 운영 시엔 DB 행 잠금 필요.
- `API_KEY`를 설정하면 모든 데이터 API에 `X-API-Key` 요구 (대시보드 HTML만 예외).

## 진행 상태

- [x] 1. 스캐폴딩 + config (20% 초과 시 import ValueError)
- [x] 2. 이자 엔진 + 테스트 (164,383원 / 365일 = 20% / 복리 시 실패)
- [x] 3. 시세 어댑터(PykrxSource, 테스트는 모킹) + 평가 엔진
- [x] 4. 모델 + DB + 계약 API
- [x] 5. 리스크 모니터 + 상태머신 (영업일 기준 유예)
- [x] 6. 처분 엔진 + MockBroker (부분/전량, 잉여금 정산)
- [x] 7. 스케줄러 + 상환/추가담보 API + 대시보드
- [x] 8. 리플레이 시뮬레이터 (`--demo`, `--csv`, pykrx)
- [ ] 실제 pykrx 시세로 리플레이 검증 (KRX_ID/KRX_PW 필요 — 이 환경에선 KRX 접속 불가로 미검증)
- [ ] KISSource 구현 (상위 저장소 `src/kis_client.py` 재사용, 관리종목 플래그 `iscd_stat_cls_code`)
- [ ] 알림 채널(SMS/알림톡), 운영자 인증·권한, PostgreSQL + 마이그레이션(Alembic)
- [ ] 운영 전 법률 검토: 매도비용 처리, 윤년 일수 기준, 마진콜·처분 통지 의무 (SPEC 9장)

## 작업 방법

```bash
cd stockpawn
pip install -e ".[dev]"            # 선택: pip install -e ".[calendar]" (미래 휴장일 정확도)
pytest -q                          # 전체 테스트 (네트워크 불필요)
ruff check app tests scripts
python -m scripts.simulate --demo  # 오프라인 리플레이
uvicorn app.main:app --env-file .env   # 대시보드 http://localhost:8000/  ·  API 문서 /docs
```
