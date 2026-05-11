# 01 · KIS API 기본기

이 프로젝트에서 자주 쓰는 KIS Open API 핵심 부분만.

## 인증 흐름

```
[KIS Developers 가입] → 앱키 + 앱시크릿
       ↓
[POST /oauth2/tokenP] → 액세스 토큰 (24h 유효)
       ↓
[모든 REST 호출] Authorization: Bearer <token>
```

토큰은 `src/kis_auth.py`가 `.kis_token_cache.json`에 캐싱. 24시간 유효, 만료 10분 전 자동 갱신.

## 환경 분기

| 모드 | Base URL | Rate Limit | TR_ID prefix |
|---|---|---|---|
| 모의 (`MODE=paper`) | `openapivts.koreainvestment.com:29443` | 초당 2건 | V로 시작 (`VTTC...`) |
| 실전 (`MODE=live`) | `openapi.koreainvestment.com:9443` | 초당 20건 | T로 시작 (`TTTC...`) |

`src/config.py`가 자동 분기.

## 자주 쓰는 엔드포인트

### 시세 (공통 TR_ID)

| 기능 | 경로 | TR_ID | 메모 |
|---|---|---|---|
| 현재가 | `/uapi/domestic-stock/v1/quotations/inquire-price` | `FHKST01010100` | 가장 자주 씀 |
| 일/주/월별 시세 | `/uapi/domestic-stock/v1/quotations/inquire-daily-price` | `FHKST01010400` | 30영업일 한계 |
| 기간별 차트 | `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` | `FHKST03010100` | 더 긴 과거 데이터용 |
| 분봉 | `/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice` | `FHKST03010200` | 1분~ 단위 |

### 주문 (모드별 TR_ID 다름)

| 기능 | 경로 | 실전 TR_ID | 모의 TR_ID |
|---|---|---|---|
| 현금 매수 | `/uapi/domestic-stock/v1/trading/order-cash` | `TTTC0802U` | `VTTC0802U` |
| 현금 매도 | (같음) | `TTTC0801U` | `VTTC0801U` |
| 주문 정정 | `/uapi/domestic-stock/v1/trading/order-rvsecncl` | `TTTC0803U` | `VTTC0803U` |
| 주문 취소 | (같음) | `TTTC0803U` | `VTTC0803U` |

### 잔고

| 기능 | 경로 | 실전 TR_ID | 모의 TR_ID |
|---|---|---|---|
| 주식 잔고 | `/uapi/domestic-stock/v1/trading/inquire-balance` | `TTTC8434R` | `VTTC8434R` |
| 일별 주문 체결 | `/uapi/domestic-stock/v1/trading/inquire-daily-ccld` | `TTTC8001R` | `VTTC8001R` |

## 주문 파라미터

`POST /uapi/domestic-stock/v1/trading/order-cash` body:

| 필드 | 의미 | 예 |
|---|---|---|
| `CANO` | 계좌번호 앞 8자리 | "12345678" |
| `ACNT_PRDT_CD` | 상품코드 2자리 | "01" |
| `PDNO` | 종목코드 6자리 | "005930" |
| `ORD_DVSN` | 주문구분 | "00"=지정가, "01"=시장가 |
| `ORD_QTY` | 주문수량 | "10" |
| `ORD_UNPR` | 주문단가 (시장가는 "0") | "71500" |

## 응답 코드

| 필드 | 의미 |
|---|---|
| `rt_cd` | "0"=성공, 그 외 실패 |
| `msg_cd` | 메시지 코드 (실패 시 의미 파악용) |
| `msg1` | 사람이 읽는 메시지 |
| `output` | 단건 데이터 (dict) |
| `output1`, `output2` | 복합 데이터 (header + list) |

## Rate Limit 회피 패턴

`src/utils/rate_limit.py`의 토큰 버킷이 자동으로 처리. 직접 짤 때는:

```python
# 70종목을 1초 안에 처리하려 하면 안 됨 (실전 20건/초)
# → 배치 10 + 딜레이 1.0초 = 7초에 70건 (rate 안 넘김)

for batch in chunks(symbols, size=10):
    for sym in batch:
        client.get_price(sym)
    time.sleep(1.0)
```

## 실시간 시세 (WebSocket)

이 프로젝트의 v0.1은 REST polling만 사용. WebSocket 통합은 다음 단계:

- 접속: `wss://ops.koreainvestment.com:21000`
- 승인키 별도 발급 필요 (`/oauth2/Approval`)
- 메시지 포맷: `|`로 구분된 필드열
- 사용 시: `python-kis` 또는 공식 샘플 참고

## 한국 거주자 세금 메모

- 국내주식 양도세: 대주주 외 미과세 (2025년 현재). 2027년 금융투자소득세 25% 도입 예정 (재논의 중)
- 해외주식: 양도소득세 22%, 연 250만 원 공제
- **모든 거래 기록 보관 의무** — `logs/` 디렉토리에 JSON으로 자동 적재됨
- 자동매매 수익은 사업소득(반복적) 또는 양도소득(일회성)으로 분류 가능. 세무사 상담 권장

## 자주 만나는 에러

| `msg_cd` | 의미 | 대처 |
|---|---|---|
| `OPSP9999` | 시스템 오류 | 재시도 (60초 후) |
| `EGW00121` | 거래 시간 외 | 장 시간에만 시도 |
| `40310000` | 모의투자 미신청 | 한투 모의투자 신청 |
| `7` | rate limit 초과 | 호출 빈도 줄이기, 5분 쿨다운 대기 |
| `40240000` | 토큰 만료 | `kis_auth.py`가 자동 갱신해야 함, 코드 점검 |
