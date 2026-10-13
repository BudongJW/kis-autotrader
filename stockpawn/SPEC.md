# 주식 전당포 (StockPawn) — Claude Code 구현 스펙

> 이 파일을 프로젝트 루트에 `CLAUDE.md` 또는 `SPEC.md`로 두고 Claude Code에서 작업을 시작하세요.
> 목표: 주식을 담보로 받고 → 가치 평가 → 대출금 산정 → 이자(연 20%) 계산 → 담보비율 모니터링 → 처분(반대매매)까지 **전 과정을 자동화하는 백오피스 솔루션의 MVP**.

---

## 0. 전제 및 범위

- **MVP는 "시뮬레이션 + 백오피스 관리 시스템"** 이다. 실제 증권 계좌에 매도 주문을 보내는 기능은 Phase 3에서 인터페이스만 정의하고 실제 연동은 하지 않는다 (실거래는 대부업 등록·증권사 제휴 후).
- 이자율은 **연 20% 고정** (대한민국 법정 최고금리). 연체이자·수수료를 포함한 총 이자율이 연 20%를 넘지 않도록 코드 레벨에서 강제한다.
- 담보 대상: 국내 상장주식 (KOSPI/KOSDAQ). 해외주식은 Phase 3.
- 시세 소스: MVP는 `pykrx` (KRX 일봉, 무료) → 운영 시 한국투자증권 OpenAPI(KIS) 실시간 시세로 교체 가능하도록 어댑터 패턴.

## 1. 기술 스택

| 영역 | 선택 | 이유 |
|---|---|---|
| 언어 | Python 3.12 | 금융 계산·데이터 라이브러리 풍부 |
| API | FastAPI | 자동 문서화, 타입 안전 |
| DB | SQLite (MVP) → PostgreSQL | 단일 파일로 빠른 시작 |
| ORM | SQLModel | FastAPI와 궁합 |
| 스케줄러 | APScheduler | 장중 주기 평가, 일일 이자 적립 |
| 시세 | pykrx (MVP), KIS OpenAPI 어댑터 | |
| 수치 | `decimal.Decimal` 사용 (float 금지) | 금융 계산 정밀도 |
| 테스트 | pytest | 계산 로직은 100% 테스트 |
| UI | 단일 HTML 대시보드 (Jinja2 or 정적 HTML + fetch) | 운영자 화면 |

## 2. 핵심 도메인 규칙 (파라미터는 `config.py`에서 관리)

```python
ANNUAL_RATE = Decimal("0.20")          # 연 20% (법정 상한, 초과 설정 시 ValueError)
DAY_COUNT = 365
MAX_LTV_BY_TIER = {                    # 종목 등급별 최대 대출비율 (담보가치 대비)
    "A": Decimal("0.70"),              # 대형 우량주 (KOSPI200 상위, 시총 5조↑)
    "B": Decimal("0.50"),              # 중형주
    "C": Decimal("0.30"),              # 소형/변동성 높음
    "X": Decimal("0.00"),              # 담보 불가 (관리종목, 거래정지, 정리매매, 스팩 등)
}
MAINTENANCE_RATIO = Decimal("1.40")    # 담보유지비율 140% 미만 → 마진콜
LIQUIDATION_RATIO = Decimal("1.20")    # 120% 미만 → 즉시 처분 사유
MARGIN_CALL_GRACE_DAYS = 2             # 마진콜 후 추가담보/상환 유예 영업일
LOAN_TERM_DAYS = 90                    # 기본 대출기간
GRACE_AFTER_MATURITY_DAYS = 7          # 만기 후 유예 → 경과 시 처분
HAIRCUT_VOLATILITY_WEIGHT = Decimal("0.5")  # 변동성 기반 추가 할인
```

### 2.1 담보가치 평가 (Valuation Engine)
입력: 종목코드, 수량
1. 최근 종가 `P` (pykrx)
2. 20일 평균 거래대금 → **유동성 점수**: 담보 평가액이 20일 평균 거래대금의 10%를 넘으면 초과분은 담보가치 0 (처분 가능성 반영)
3. 60일 역사적 변동성 `σ` (연율화) → **헤어컷** `h = min(0.5, σ × HAIRCUT_VOLATILITY_WEIGHT)`
4. 종목 등급 `tier` 결정 (시총, 관리종목 여부, 거래정지 여부, 변동성)
5. `담보평가액 = P × 수량 × (1 − h)`
6. `최대대출금 = 담보평가액 × MAX_LTV_BY_TIER[tier]`

출력: `Valuation(price, qty, raw_value, haircut, tier, collateral_value, max_loan, reasons: list[str])`
→ `reasons`에 "관리종목", "변동성 과다", "유동성 부족" 등 사유 로그.

### 2.2 이자 계산 (Interest Engine)
- 일할 계산: `daily_interest = principal × ANNUAL_RATE / 365`
- 매일 00:05 스케줄러가 `accrued_interest`에 적립 (단리). 복리 금지 — 복리 시 실효 이자율이 20%를 넘을 수 있음.
- 연체이자: 약정금리 + 3%p 이내가 원칙이나, **총합이 20%를 초과할 수 없으므로 이 시스템에서는 연체 가산이자 = 0**. 대신 만기 유예 후 처분으로 대응.
- 중도상환: 수수료 없음 (수수료도 이자로 간주됨). 상환일까지의 이자만 정산.
- 선이자 공제 금지.

### 2.3 담보비율 모니터링 (Risk Monitor)
- 장중 매 10분 (09:00–15:30) + 장 마감 후 1회 평가
- `collateral_ratio = 현재 담보평가액(헤어컷 미적용 시가) / (원금 + 미수이자)`
- 상태 전이:
  - `NORMAL` : ratio ≥ 140%
  - `MARGIN_CALL` : 120% ≤ ratio < 140% → 알림 발송, 유예 2영업일
  - `LIQUIDATION_PENDING` : ratio < 120% **또는** 마진콜 유예 만료 **또는** 만기+7일 경과 **또는** 담보종목이 tier X로 변경
  - `LIQUIDATING` : 처분 주문 생성 (다음 거래일 09:00 동시호가)
  - `CLOSED` : 원리금 회수 완료 (잉여금은 차주에게 반환, 부족분은 미수채권으로 기록)

### 2.4 처분 (Liquidation Engine)
- 처분 수량 = `ceil((원금 + 미수이자 + 예상수수료) / 예상체결가(전일종가 × 0.97))`, 보유수량 상한
- 부분 처분 우선: 담보비율이 150%로 회복되는 수량만 처분 (전량 처분은 ratio < 100%일 때만)
- `BrokerAdapter` 인터페이스: `place_sell_order(account, code, qty, order_type)` → MVP는 `MockBroker`(전일 종가 기준 체결 시뮬레이션), 운영은 `KISBroker`
- 체결 후: 원리금 상계 → 잉여금 정산 → 상태 `CLOSED`

## 3. 데이터 모델 (SQLModel)

```
Borrower      id, name, phone, account_no, kyc_status, created_at
Stock         code, name, market, tier, is_blocked, updated_at
Loan          id, borrower_id, principal, annual_rate, start_date, maturity_date,
              status, accrued_interest, repaid_principal, repaid_interest, closed_at
Collateral    id, loan_id, stock_code, qty, pledge_price, pledge_date, released_qty
Valuation     id, loan_id, ts, market_value, collateral_value, ratio, status_at_ts
InterestLog   id, loan_id, date, daily_interest, principal_base
Event         id, loan_id, ts, type(MARGIN_CALL|LIQ_ORDER|FILL|REPAY|...), payload(json)
LiquidationOrder id, loan_id, stock_code, qty, order_ts, fill_price, fill_qty, status
```

## 4. 작동 흐름 (End-to-End)

```
[차주 신청] ──> [KYC/계좌 확인] ──> [종목·수량 입력]
                                        │
                                        ▼
                              [Valuation Engine]
                       시세 + 변동성 + 유동성 + 등급
                                        │
                      tier X ───────────┼──────────> 거절 + 사유
                                        ▼
                        [최대대출금 제시] → 차주가 금액 선택 (≤ max)
                                        │
                                        ▼
                     [계약 생성: Loan + Collateral 질권 설정]
                     연 20% 고정 / 90일 / 담보유지 140%
                                        │
                                        ▼
                             [대출 실행 → 송금(Mock)]
                                        │
          ┌─────────────────────────────┼──────────────────────────┐
          ▼                             ▼                          ▼
 [매일 00:05 이자 적립]      [장중 10분마다 담보비율 평가]       [만기 D-7 알림]
                                        │
                             ratio<140% → MARGIN_CALL (2영업일)
                             ratio<120% / 유예만료 / 만기+7일 / tier X
                                        │
                                        ▼
                            [Liquidation Engine → BrokerAdapter]
                                        │
                                        ▼
                      [체결 → 원리금 상계 → 잉여금 반환 → CLOSED]

 (언제든) 차주 상환 요청 → 일할 이자 정산 → 담보 해제 → CLOSED
 (언제든) 추가 담보 제공 → ratio 재계산 → NORMAL 복귀
```

## 5. API (FastAPI)

```
POST /borrowers                      차주 등록
POST /valuations                     {code, qty} → 담보평가 + 최대대출금 (계약 전 조회용)
POST /loans                          {borrower_id, collaterals:[{code,qty}], amount} → 계약 생성
GET  /loans/{id}                     상태, 원금, 미수이자, 담보비율, 이벤트 로그
POST /loans/{id}/repay               {amount} → 일할이자 정산 후 원금 차감
POST /loans/{id}/collateral          추가 담보 제공
POST /loans/{id}/liquidate           수동 처분 트리거 (운영자)
GET  /dashboard                      전체 대출 현황, 위험 등급별 집계
POST /admin/run-valuation            스케줄러 수동 실행 (테스트용)
POST /admin/run-interest             이자 적립 수동 실행 (테스트용)
```

## 6. 디렉터리 구조

```
stockpawn/
├── CLAUDE.md                 ← 이 파일
├── pyproject.toml
├── app/
│   ├── main.py               FastAPI 앱, 라우터 등록, 스케줄러 시작
│   ├── config.py             파라미터 (2장)
│   ├── models.py             SQLModel 테이블 (3장)
│   ├── db.py
│   ├── engines/
│   │   ├── valuation.py      담보평가
│   │   ├── interest.py       이자 계산
│   │   ├── risk.py           담보비율 모니터링·상태 전이
│   │   └── liquidation.py    처분 로직
│   ├── adapters/
│   │   ├── price/            PriceSource 인터페이스, PykrxSource, KISSource(stub)
│   │   └── broker/           BrokerAdapter, MockBroker, KISBroker(stub)
│   ├── routers/
│   ├── scheduler.py
│   └── static/dashboard.html
├── tests/
│   ├── test_interest.py      일할계산, 20% 상한 강제, 복리 금지
│   ├── test_valuation.py     헤어컷·tier·유동성
│   ├── test_risk.py          상태 전이 전 케이스
│   └── test_liquidation.py   부분/전량 처분 수량 계산, 잉여금 정산
└── scripts/
    └── simulate.py           과거 시세로 N일 리플레이 → 마진콜/처분 발생 시나리오 출력
```

## 7. Claude Code 작업 순서 (각 단계를 한 세션 프롬프트로)

1. **스캐폴딩**: "이 CLAUDE.md의 6장 구조대로 프로젝트를 만들고, pyproject.toml에 fastapi, sqlmodel, pykrx, apscheduler, pytest를 추가해. `config.py`에 2장 파라미터를 Decimal로 정의하고 ANNUAL_RATE > 0.20이면 import 시점에 ValueError를 내도록 해."
2. **이자 엔진 + 테스트**: "`engines/interest.py`를 2.2 규칙대로 구현하고 `tests/test_interest.py`에 (a) 원금 1,000만원 30일 이자 = 164,383원 (b) 365일 적립 합계가 정확히 원금×0.20 (c) 복리 적용 시 테스트 실패 를 포함해."
3. **시세 어댑터 + 평가 엔진**: "`adapters/price`에 PriceSource 추상 클래스와 PykrxSource를 만들고, `engines/valuation.py`를 2.1대로 구현해. pykrx 호출은 테스트에서 fixture로 모킹해."
4. **모델 + DB + 계약 API**: "3장 모델과 `POST /valuations`, `POST /loans`, `GET /loans/{id}`를 구현해."
5. **리스크 모니터 + 상태머신**: "`engines/risk.py`에 2.3 상태 전이를 구현하고 모든 전이 경로를 테스트해. 마진콜 유예는 영업일 기준(주말·공휴일 제외, pykrx 거래일 캘린더 사용)."
6. **처분 엔진 + MockBroker**: "2.4대로 구현. 부분처분 수량 계산과 잉여금 정산 테스트 포함."
7. **스케줄러 + 상환/추가담보 API + 대시보드**
8. **리플레이 시뮬레이터**: "`scripts/simulate.py`: 종목코드·수량·대출금·시작일을 받아 과거 90일을 하루씩 리플레이하면서 담보비율, 마진콜, 처분 발생일과 최종 손익을 표로 출력."

## 8. 하지 말 것 (Claude Code 가드레일)

- float로 금액 계산 금지 → Decimal
- 연 20%를 초과하는 어떤 수수료/가산이자도 추가 금지
- 실제 증권사 API에 주문을 보내는 코드를 기본 활성화하지 말 것 (`BROKER=mock` 기본값)
- 개인정보(주민번호 등)는 모델에 넣지 말 것 — KYC는 외부 인증 결과 플래그만 저장

## 9. 운영 전환 시 필요한 것 (코드 밖)

- 대부업 등록 (지자체 또는 금융위) — 미등록 영업은 형사처벌 대상
- 증권사와 질권 설정·반대매매 연계 제휴 (스탁론 구조와 동일하게 저축은행/캐피탈이 증권사와 제휴하는 형태)
- 금융소비자보호법상 핵심설명서, 계약서 양식
- 실시간 시세 라이선스 (KIS OpenAPI 등)
