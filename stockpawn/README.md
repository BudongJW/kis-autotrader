# StockPawn — 주식 담보대출(주식 전당포) 백오피스 MVP

국내 상장주식을 담보로 받아 **담보평가 → 대출 → 연 20% 단리 이자 → 담보비율 모니터링 → 처분(반대매매) → 정산**까지
자동화하는 시뮬레이션 + 백오피스 시스템이다. 실제 증권사 주문은 나가지 않는다(MockBroker).

> ⚠️ 실제 영업에는 대부업 등록, 증권사 질권·반대매매 제휴, 금융소비자보호법상 설명서·계약서, 시세 라이선스가 필요하다
> ([SPEC.md](SPEC.md) 9장). 미등록 대부 영업은 형사처벌 대상이다.

## 빠른 시작

```bash
cd stockpawn
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,calendar]"
cp .env.example .env          # KRX_ID / KRX_PW 입력 권장 (pykrx 시가총액·거래대금 조회용)

pytest -q                                   # 테스트 (네트워크 불필요)
python -m scripts.simulate --demo           # 합성 급락 시나리오 리플레이
uvicorn app.main:app --env-file .env        # http://localhost:8000/ 대시보드, /docs API 문서
```

## 동작 요약

| 단계 | 규칙 | 코드 |
|---|---|---|
| 담보평가 | 종가 × 수량을 20일 평균 거래대금의 10%로 제한 → 변동성 헤어컷 min(50%, σ×0.5) → 등급별 LTV (A 70% / B 50% / C 30% / X 불가) | `engines/valuation.py` |
| 이자 | 연 20% 단리, 적수 방식 일할 계산, 대출일 포함·상환일 제외, 연체가산·수수료·선이자 없음 | `engines/interest.py` |
| 모니터링 | 담보비율 = 시가 / (원금 + 미수이자). 140% 미만 마진콜(2영업일 유예), 120% 미만·유예 만료·만기+7일·tier X → 처분 | `engines/risk.py` |
| 처분 | 100% 미만 전량 / 만기·부적격·수동은 원리금 회수분 / 그 외 150% 회복분만. 이자 → 원금 → 잉여금 반환, 부족분은 미수채권 | `engines/liquidation.py` |

스케줄(KST): 00:05 이자 적립 · 08:45 처분 주문(동시호가) · 09:00~15:30 10분마다 평가 · 15:40 장후 평가 + 등급 재산정.

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/borrowers` | 차주 등록 (KYC 결과 플래그만) |
| PATCH | `/borrowers/{id}/kyc` | KYC 상태 갱신 |
| POST | `/valuations` | `{code, qty}` → 담보평가·최대대출금 |
| POST | `/loans` | `{borrower_id, collaterals:[{code,qty}], amount}` → 계약 + Mock 송금 |
| GET | `/loans`, `/loans/{id}` | 상태·원금·미수이자·담보비율·이벤트·처분주문 |
| POST | `/loans/{id}/repay` | `{amount}` → 일할 이자 정산 후 이자 → 원금 |
| POST | `/loans/{id}/collateral` | 추가 담보 (마진콜 해소) |
| POST | `/loans/{id}/liquidate` | 운영자 수동 처분 |
| GET | `/dashboard` | 상태별·위험구간별 집계 |
| POST | `/admin/run-interest`, `/admin/run-valuation`, `/admin/run-liquidation` | 스케줄러 잡 수동 실행 |
| GET/PUT | `/admin/stocks`, `/admin/stocks/{code}` | 종목 캐시 조회 / 담보 차단·등급 상한 |

`API_KEY` 환경변수를 설정하면 데이터 API에 `X-API-Key` 헤더가 필요하다.

## 리플레이 시뮬레이터

```bash
python -m scripts.simulate --demo [--days 105] [--amount 20000000] [--slippage 0.03]
python -m scripts.simulate --code 005930 --qty 1000 --start 2025-01-02          # pykrx (KRX 로그인 필요)
python -m scripts.simulate --csv bars.csv --code 005930 --qty 1000 --start 2025-01-02 \
    --market KOSPI --market-cap 400000000000000                                # CSV: date,close,volume[,trading_value]
```

일봉 종가만 쓰므로 장중 급락을 놓치고, 체결도 전일 종가 기준이라 갭하락 손실을 과소평가한다.
`--slippage`로 보수적으로 돌리고, 여러 구간·종목으로 반복 검증할 것.

## 알려진 제약

- pykrx ≥ 1.2는 KRX 로그인(`KRX_ID`/`KRX_PW`)이 있어야 시가총액·종목목록·거래일 조회가 된다. 없으면 시가총액 미상 → 최고 C등급.
- pykrx는 관리종목·정리매매 여부를 주지 않는다 → 운영자가 `PUT /admin/stocks/{code}`로 차단. (KIS 소스 구현 시 자동화)
- KISSource / KISBroker는 stub. SQLite 단일 프로세스 전제(프로세스 락).

구현 세부 결정과 진행 상태는 [CLAUDE.md](CLAUDE.md) 참고.
