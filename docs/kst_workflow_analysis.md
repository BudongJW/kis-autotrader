# 한국시간(KST) 기준 작동 흐름 분석 및 개선 지점

작성일: 2026-07-26
대상 커밋: `claude/korea-timezone-workflow-analysis-rna1a1` 기준 main 스냅샷

> **처리 현황 (2026-07-26)**: 아래 P0~P3 전 항목 수정 완료.
> 문서 본문은 **수정 전 상태의 분석 기록**으로 그대로 둔다 (왜 이렇게 고쳤는지의
> 근거). 각 항목에 적용된 수정은 [§4 적용된 수정](#4-적용된-수정)에 정리했다.

---

## 1. KST 24시간 작동 흐름 맵

GitHub Actions cron은 **UTC**로 해석되고, 모든 워크플로가 `env: TZ=Asia/Seoul`을
설정한다. 따라서 **cron은 UTC, 실행 중인 파이썬 프로세스의 벽시계는 KST**다.
이 이중 구조가 이 프로젝트 시간 로직의 핵심이자 대부분 리스크의 출처다.

### 1.1 평일 하루 (KST)

| KST | 워크플로 | cron (UTC) | 하는 일 |
|---|---|---|---|
| 07:00 | `tests.yml` | `0 22 * * *` | 회귀 pytest |
| 08:00 | `autotrader.yml` | `0 23 * * 0-4` | 토큰 갱신 + 봇 기동(09:00까지 대기) |
| 08:30 | `autotrader.yml` / `market-learn.yml` | `30 23 * * 0-4` | 봇 예비 기동 / 장 전 학습 |
| 08:50 | `dry-run-report.yml` | `50 23 * * 0-4` | KR 개장 전 프리뷰 |
| 08:55 | `autotrader.yml` | `55 23 * * 0-4` | 봇 최종 예비 기동 |
| 09:00~15:55 | `autotrader.yml` | `*/5 0-6 * * 1-5` | 5분 watchdog (죽었으면 재기동) |
| 09:00~09:10 | (봇 내부) | — | 시가 평가 → 조건부 매도 |
| 15:20 | (봇 내부) | — | EOD 청산 시작 (`MARKET_CLOSE`) |
| 15:30 | (봇 내부) | — | 루프 종료 (`MARKET_END`) |
| 16:00 | `daily-report.yml`, `market-learn.yml --phase post` | `0 7 * * 1-5` | 일일 리포트 + 장 후 학습 |
| 16:10 | `journal.yml` | `10 7 * * 1-5` | 저널 repo 푸시 |
| 21:30 / 21:50 | `us-night-trader.yml` | `30,50 12 * * 1-5` | US pre-open 기동 |
| 22:10 | `us-night-trader.yml` | `10 13 * * 1-5` | US pre-open 기동 |
| 22:20 | `dry-run-report.yml` | `20 13 * * 1-5` | US 개장 전 프리뷰 |
| 22:30(서머) / 23:30(동절) | (봇 내부) | — | 미국 정규장 개장 |
| 22:00~익일 05:55 | `us-night-trader.yml` | `*/5 13-20 * * 1-5` | 5분 watchdog |
| 익일 04:45(서머) / 05:45(동절) | (봇 내부) | — | 폐장 15분 전 강제 청산 |
| 익일 05:00(서머) / 06:00(동절) | (봇 내부) | — | 폐장, 루프 종료 |
| 익일 06:00~06:45 | `us-night-trader.yml` | `*/15 21 * * 1-5` | 동절기 마감 직전 커버 |
| 익일 06:30 | `market-learn.yml --phase post_us` | `30 21 * * 1-5` | 미국장 후 학습 |

### 1.2 주말 (KST)

| KST | 워크플로 | cron (UTC) | 비고 |
|---|---|---|---|
| 토 05:55까지 | `us-night-trader.yml` | `*/5 13-20 * * 5` | 금요일 US장의 토요일 새벽분 |
| 토 06:30 | `market-learn.yml` | `30 21 * * 5` | 금요일 US장 학습 |
| 일 10:00 | `optimize.yml` | `0 1 * * 0` | 주간 파라미터 최적화 |
| 일 10:30 | `drift-check.yml` | `30 1 * * 0` | 백테스트 vs 실전 drift |

### 1.3 검증 결과 — cron ↔ KST 변환은 전부 정확

12개 워크플로의 모든 cron을 UTC→KST로 재계산한 결과 **의도한 KST 시각과 어긋난
cron은 없다.** 특히 까다로운 두 케이스가 올바르게 처리돼 있다:

- **장 전 cron의 요일 시프트**: `0-4`(일~목 UTC)로 지정해 KST 월~금 08:00을
  정확히 맞췄다. `1-5`로 썼다면 KST 화~토가 되는 흔한 실수를 피했다.
- **미국장 후 학습**: `30 21 * * 1-5`(UTC 월~금) → KST 화~토 06:30. 금요일
  US장(토 새벽 마감)까지 정확히 커버.

또한 `_us_weekend_closed()`는 "금요일 미국장의 토요일 새벽분(KST)"을 정상 거래로
허용하도록 이미 수정돼 있고 `tests/test_us_session_history.py`가 이를 고정한다.
`get_overseas_daily_price()`의 `BYMD`도 KST 대신 `America/New_York` 기준 날짜를
쓰도록 고쳐져 있다. 시간대 관련 기존 버그 수정 이력이 잘 남아 있는 편이다.

---

## 2. 개선 필요 지점

심각도 순.

### [P0] 미국 서머타임이 수동 플래그 — 2026-11-02에 확정적으로 깨진다

`configs/strategy.yaml:256`
```yaml
summer_time: true
```

이 값 하나가 `src/bot/us_session.py:69,87`과 `src/bot/night_run.py:146`에서
개장/폐장 시각 전체를 결정한다. **DST 전환 시 사람이 직접 바꿔야 한다.**

검증한 실제 전환 시점 (KST):

| 날짜 | 개장 KST | 폐장 KST |
|---|---|---|
| 2026-10-30 (금) | 22:30 | 토 05:00 |
| **2026-11-02 (월)** | **23:30** | **화 06:00** |

플래그를 안 바꾸면 (`summer_time: true` 유지, 실제는 동절기):

1. **22:30~23:30 KST**: 장이 안 열렸는데 봇이 개장으로 인식 → 정규장 시세가
   아닌 값으로 진입 판단. 주문은 거부되거나 예약 처리.
2. **04:45 KST**: 폐장 15분 전으로 착각하고 `close_us_positions()` 강제 청산.
   실제 폐장은 06:00 → **1시간 15분 일찍 전량 청산.**
3. **05:00~06:00 KST**: 루프가 `break`로 종료. **장이 열려 있는 마지막 1시간
   동안 손절·추적손절이 전혀 동작하지 않는다.**

반대 방향(3월 전환에 플래그가 `false`로 남는 경우)이 더 위험하다: 폐장(05:00)이
이미 지난 05:45에 청산을 시도 → 주문 거부 → **의도치 않은 오버나이트 캐리.**
`us_session.py:41`에 `EST = ZoneInfo("America/New_York")`가 선언돼 있지만
**어디서도 쓰이지 않는다** — 자동화 의도는 있었으나 미완인 상태.

**개선안**: `get_us_market_times()`를 `America/New_York`의 09:30/16:00을
`Asia/Seoul`로 변환해 계산하도록 바꾸고, `summer_time`은 수동 오버라이드용으로만
남기거나 제거. 전환 주 경계(10/30, 11/2)를 고정한 단위 테스트 추가.

### [P0] 장 휴장일 캘린더가 없다 (한국·미국 모두)

`grep`으로 확인한 결과 프로젝트 전체에 휴장일 처리가 **주말 판정
(`_us_weekend_closed`) 하나뿐**이다. 공휴일 캘린더는 존재하지 않는다.

- **한국**: cron이 UTC 월~금이므로 설날·추석·광복절·대체공휴일에도 봇이 정상
  기동해 09:00~15:30 동안 루프를 돌린다. 시세는 전일 종가로 고정되고 주문은
  거부된다. 금전 손실보다는 로그·리포트 오염과 API 낭비, 그리고 "시가 매도"
  로직이 잘못된 가격으로 매도 판단을 내릴 여지가 문제다.
- **미국(더 위험)**: **조기 폐장일**이 연 3회 있다 (추수감사절 다음날, 크리스마스
  이브, 독립기념일 전날). 이 날은 13:00 ET에 마감 = **KST 03:00(서머)**. 봇은
  폐장을 05:00으로 믿고 04:45에 청산을 시도하지만 이미 2시간 전에 장이 닫혔다
  → **주문 실패 → 포지션 오버나이트 캐리.** DST 오류와 정확히 같은 실패 모드다.

**개선안**: `pandas_market_calendars` 또는 `exchange_calendars`(둘 다 KRX·NYSE
지원)로 거래일/조기폐장 판정. 최소한 조기 폐장일만이라도 yaml에 하드코딩하고
`get_us_market_times()`가 참조하게 할 것. 봇 루프 진입 시 "오늘 거래일 아님 →
즉시 종료" 가드를 추가하면 한국 공휴일 노이즈도 같이 해결된다.

### [P1] US 재진입 쿨다운이 KST 자정을 넘으며 어긋난다

`src/bot/us_session.py:551`
```python
_today = _dt.now().strftime("%Y-%m-%d")
```

두 가지 문제가 겹친다.

1. **US 세션의 강제 청산은 항상 KST 자정 이후에 일어난다** (04:45 또는 05:45).
   즉 월요일 밤 시작한 세션의 마감청산 기록은 **화요일 날짜**로 찍힌다.
   `recently_force_closed()`는 이 KST 날짜를 그대로 비교하므로
   `reentry_cooldown_days: 2`가 실제로는 **3개 세션**을 막는다 (검증:
   화 dawn 청산 → 화/수/목 밤 진입 차단, 금 밤에 해제).
2. `_today`가 `run_us_strategy()` 진입 시점에 **한 번만** 계산된다. 세션이
   자정을 넘으면 이후 호출에서 기준 날짜가 하루 밀려 쿨다운 창이 조용히 이동한다.

**개선안**: US 세션을 KST 날짜가 아니라 **"US 거래일"(= `America/New_York`
기준 날짜)로 키잉**한다. 세션 시작 시 US 거래일을 한 번 확정해 넘기면 두 문제가
동시에 해결된다.

### [P1] naive `datetime.now()`가 56곳 — TZ 환경변수에만 의존

`src/` 전역에서 `datetime.now()` / `strftime("%Y-%m-%d")`가 **timezone-naive**로
56회 쓰인다 (`risk_manager.py`, `market_learner.py`, `pre_briefing.py`,
`experience.py`, `tracker.py`, 전략 모듈 다수). 이들이 KST를 반환하는 유일한
근거는 워크플로의 `env: TZ=Asia/Seoul`이다.

- 코드 어디에도 TZ 부트스트랩(`os.environ.setdefault("TZ", ...)`)이 없다.
- **새 워크플로에서 `TZ` 한 줄을 빠뜨리면 전 시스템이 조용히 9시간 밀린다.**
  일일 손실 한도·쿨다운·리포트 날짜가 전부 어긋나는데 예외는 안 난다.
- 이를 검증하는 테스트도 없다.

또한 **aware/naive 혼재**가 이미 존재한다:
`risk_manager.py:83`은 `buy_time`을 naive ISO로, `us_session.py:345,393`은
`datetime.now(KST)` aware ISO로 기록한다. 현재는 `check_stop_loss()`가 KR
포지션만 읽어서 충돌하지 않지만, **US 포지션에 같은 보유시간 계산을 붙이는 순간
`TypeError: can't subtract offset-naive and offset-aware datetimes`가 난다.**
`tests/test_regression_bugs.py`가 바로 이 버그 클래스를 이미 한 번 잡은 이력이 있다.

**개선안**: `src/utils/clock.py`에 `now_kst()` / `today_kst()`를 두고 전역
치환. 최소 조치로는 (a) `src/config.py` import 시 TZ 강제, (b) 모든 워크플로에
`TZ` 존재를 검사하는 pytest 1개 추가.

### [P2] US 세션에 일일 손실 한도가 없다

`check_daily_loss_limit()` / `check_daily_profit_target()`은 `single_run.py`
(한국장)에서만 호출된다. `night_run.py`·`us_session.py`에는 호출부가 없다.

이건 순수 시간대 이슈는 아니지만 **원인은 시간대에 있다**: 두 함수 모두
`today_str = datetime.now().strftime("%Y-%m-%d")`로 KST 날짜를 잡고
`trades.csv`를 필터링하는데, US 세션은 KST 자정을 가로지르므로 **세션 도중
00:00 KST에 당일 손익 집계가 리셋된다.** KST-day 모델이 야간 세션에 맞지 않아
아예 적용하지 못한 것으로 보인다.

**개선안**: 위 P1의 "US 거래일" 키를 도입하면 이 함수들을 US 세션에도 안전하게
붙일 수 있다.

### [P3] 사소한 것들

- **`optimize.yml:5` 주석 오류**: `# 매주 일요일 10:00 KST (= 토요일 01:00 UTC)`
  → cron `0 1 * * 0`은 **일요일** 01:00 UTC다. 동작은 정확(KST 일 10:00),
  주석만 틀렸다. 나중에 주석 보고 cron을 "고치는" 사고를 부를 수 있다.
- **낭비성 기동**: `*/15 21 * * 1-5`(KST 06:00~06:45)는 서머타임엔 폐장
  1시간 후라 전부 즉시 종료된다. `*/5 0-6`의 KST 15:35~15:55분도 `MARKET_END`
  초과로 즉시 종료. public repo라 분 한도는 무제한이지만 run 목록 노이즈가 크다.
- **동절기 pre-open 대기**: `30 12 * * 1-5`(KST 21:30)에 기동하면 동절기 개장
  (23:30)까지 120분. `PREOPEN_WAIT_LIMIT_MIN = 120`에 정확히 걸려 대기에
  들어가고, 60초 sleep 루프로 2시간을 태운다. DST 자동화(P0) 시 함께 조정 필요.
- **KRX 임시 장시간 변경 미대응**: 수능일 개장 1시간 지연(10:00~16:30),
  연말 폐장일 등. `MARKET_OPEN`/`MARKET_CLOSE`가 상수라 대응 불가.

---

## 3. 권장 조치 순서

1. **`get_us_market_times()`를 `America/New_York` 기준으로 자동 계산** —
   2026-11-02 전에 필수. 전환 경계 테스트 동봉. (P0)
2. **거래일/조기폐장 캘린더 도입** + 봇 루프 진입 가드. (P0)
3. **`src/utils/clock.py` 도입 및 naive `datetime.now()` 치환** +
   워크플로 `TZ` 검사 테스트. (P1)
4. **US 세션 상태 키를 "US 거래일"로 전환** → 쿨다운 정합성 + 일일 손실 한도
   적용 가능. (P1, P2)
5. 주석·불필요 cron 정리. (P3)

1번과 2번은 같은 실패 모드(폐장 시각 오인 → 강제 청산 실패 → 오버나이트 캐리)를
공유하므로 함께 처리하는 것이 효율적이다.

---

## 4. 적용된 수정

전 항목 수정 완료. 전체 테스트 **363 passed, 4 skipped**
(`test_rate_limit.py`는 실시간 sleep이 있어 별도 실행 — 통과 확인).

| # | 항목 | 커밋 | 핵심 변경 |
|---|---|---|---|
| P0 | US 서머타임 수동 플래그 | `fix(us): ... ET 기준 자동 계산` | `src/utils/clock.py` 신설. ET 09:30/16:00을 KST로 변환해 DST 자동 추종. `summer_time`은 값 무시 + 불일치 시 경고만 |
| P0 | 휴장일 캘린더 부재 | `feat(calendar): ...` | `src/utils/market_calendar.py` 신설. `exchange_calendars`(XKRX/XNYS)로 KRX 음력 공휴일 + NYSE 조기폐장 판정. 봇 루프에 휴장일 가드 |
| P1 | naive `datetime.now()` 56곳 | `refactor(time): ...` | 전량 `now_kst()/today_kst()/kst_stamp()`로 치환. `config.py` import 시 TZ 고정(2차 방어). 워크플로 TZ 누락 검사 테스트 |
| P1 | US 쿨다운 KST 자정 어긋남 | `fix(us): ... US 거래일(ET) 키잉` | 거래 기록·기준 날짜를 모두 US 거래일로 정규화. `cooldown_days=2`가 정확히 2일치만 차단 |
| P2 | US 일일 손실 한도 부재 | 〃 | `check_us_daily_loss_limit()` 추가. US 거래일 키잉으로 자정 리셋 해소 |
| P3 | 주석 오류·낭비 cron | `chore(workflows): ...` | `optimize.yml` 주석 정정, 낭비 기동 6회/일 제거, 실행시간 예산을 개장 시점부터 계산 |

### 작업 중 추가로 발견한 것들

분석 시점에는 안 보였다가 수정하면서 드러난 문제들.

1. **토큰 캐시 aware/naive 충돌 (잠재 → 실제)**
   `kis_auth.TokenBundle.expires_at`을 aware로 바꾸는 순간, 구버전이 남긴 naive
   캐시와 비교에서 `TypeError`가 나 **인증이 통째로 죽는다**. 토큰 캐시는
   artifact로 run 간에 넘어오므로 배포 직후 첫 run에서 바로 터졌을 것이다.
   → `parse_kst()`로 정규화.

2. **KR 일일 손실 한도가 US 체결에 오염 (기존 버그)**
   `trades.csv`에 KR(원)과 US(**센트**, `int(price*100)`)가 한 파일에 섞여 있는데
   `log_trade`의 `market` 인자는 CSV에 기록되지 않는다(`FIELDS`에 컬럼 없음).
   그래서 KST 저녁의 US 체결이 원화 손익으로 둔갑해 국내 한도 계산에 들어갔다.
   → `tracker.is_kr_symbol()`(KRX=6자리 숫자 / 美=알파벳)로 분리. 스키마 변경이
   없어 레거시 행에도 그대로 적용된다.

3. **pre-open 대기가 실행시간 예산을 잠식**
   340분 예산이 프로세스 시작 시점부터 계산돼, 동절기 US는 21:30 기동 → 120분
   대기 → 03:10 KST에 자체 종료(폐장 06:00까지 한참 남음)로 세션 한복판에서
   핸드오프가 났다. → 개장 확인 시점에 예산 리셋.

### 남은 것 (이번 범위 밖)

- **KRX 임시 장시간 변경**: 수능일 개장 1시간 지연(10:00~16:30), 연말 폐장일 등.
  `MARKET_OPEN`/`MARKET_CLOSE`가 여전히 상수다. `exchange_calendars`의
  `session_open/session_close`를 KR에도 쓰면 해결 가능하나, KR 로직 전반이
  상수 기반이라 변경 범위가 크다.
- **임시공휴일**: `exchange_calendars` 릴리스 후 지정된 날짜는 라이브러리가
  모른다. `configs/strategy.yaml`의 `calendar.extra_holidays_kr` /
  `extra_holidays_us`로 수동 보완할 수 있게만 열어뒀다.
- **Kelly·drawdown 스케일의 KR/US 혼재**: 비율 계산이라 통화 단위 오류는 없지만,
  국내 포지션 사이징이 US 성과에 영향받는 게 의도인지는 설계 판단이 필요해
  건드리지 않았다.

---

## 5. 실전 로그로 확인된 추가 결함 (2026-07-30)

7/28·7/29 이틀치 미국장 세션 로그(run 30371050662 / 30462059362)를 대조해 확인.

### [P0] 같은 포지션에 매도 주문이 두 번 나갔다 — 공매도 위험

US 포지션의 청산 경로는 셋인데, US-MOM 매수는 `record_us_buy()`(us_positions.json)와
모멘텀 자체 state에 **동시** 등록된다. 즉 한 포지션에 청산 담당이 둘이고 서로를 모른다.

| 경로 | 읽는 장부 | 주기 |
|---|---|---|
| `check_us_risk()` | us_positions.json | 60초 |
| `run_us_momentum_strategy()` | us_momentum_positions.json | 300초 |
| `close_us_positions()` | us_positions.json | 마감 1회 |

2026-07-29 실전 로그:

```
[US-MOM BUY] PSQ 10주 @ 한도 $27.75 (현재가 $27.70) ≤ $277.50 (inverse)
      체결: 10주 @ $27.44 (슬리피지 -0.96%)
[US 리스크] PSQ 10주 @ 한도 $27.57 (현재가 $27.61) — US 추적손절 (고점 $27.84에서 -0.8%)
      응답: rt_cd=0, msg=주문 전송 완료 되었습니다.
[US-MOM] PSQ 청산: 본전이익 보존 (고점 +1.46%였다가 반전 → +0.55%에서 청산)
      응답: rt_cd=0, msg=주문 전송 완료 되었습니다.
```

10주를 사고 10주 매도가 두 번 접수됐다. 첫 주문 체결 뒤 두 번째가 나가면 없는 주식을
파는 것 = 공매도가 열린다. 그리고 `get_us_holdings()`는 `qty > 0`만 반환하므로
(`us_session.py`의 잔고 파싱) **그 음수 포지션은 봇 눈에 아예 안 보인다** — 손절도
마감청산도 안 걸린다. 6-08 국장 -14% 방치 사건과 같은 구조의 최악 버전.

7/28 밤엔 안 났다 — 간헐적이다. 두 번째 주문이 실제 체결됐는지는 로그만으로 확인 불가.

**수정 (2겹 방어)**

1. **소유권 분리** — `is_momentum_owned()`. `asset_type`이 `us_mom_*`이고
   **모멘텀 state에 실제로 있을 때만** 소유권을 인정한다. state가 유실되면
   (세션 사망·아티팩트 누락) 범용 경로가 다시 떠맡는다 → 담당자는 항상 정확히 하나.
   태그만 보고 제외했다간 고아 포지션이 생긴다.
2. **주문 직전 브로커 잔고 재확인** — `_sell_and_record()`가 보유 0주면 주문을
   내지 않고 장부만 정리, 보유량 미만이면 실보유까지로 수량을 깎는다. 조회
   실패(-1)면 기존 동작 유지(막았다가 손절이 통째로 누락되는 쪽이 더 위험).

1만으로는 부족하다 — 수동 개입·부분체결 재시도 등 다른 조합에서도 같은 사고가
날 수 있어 2가 경로와 무관한 최종 방어선이다. `tests/test_us_double_sell.py` 15건.

### [P1] 시세 지연 실측이 2연속 실패 — 반영할 측정치가 0건

```
시세 지연 실측: 판정 불가 (kis_quote_empty)
{"symbol": "SPLG", "kis_last": 0.0, "error": "kis_quote_empty", ...}
```

7/28·7/29 동일. `report_quote_lag()`는 루프 시작 시 **1회만** 돌고, `_us_quote()`는
예외를 통째로 삼켜(`except Exception: pass`) 실패 사유가 안 남는다. 같은 세션에서
PSQ(동일 AMEX) 시세는 정상이었으니 일시적 실패로 보이는데, 재시도가 없어 그 밤
전체가 측정 없이 끝난다. → `execution.quote_lag_min`을 실측으로 채우는 계획이
현재 근거 없음.

**수정**

- `_us_quote_err()` 신설 — `rt_cd`/예외/`last=0`을 구분해 사유를 돌려준다.
  `_us_quote()`는 여기에 위임하므로 기존 호출부는 그대로다.
- 후보 폴백 — `quote_lag_probe_candidates()`가 설정된 us_session 유니버스와
  모멘텀 롱/인버스 종목을 후보로 깔고 앞에서부터 시도한다. 한 종목이 비어도
  측정이 죽지 않는다.
- 재시도 — `report_quote_lag()`가 결론 여부를 bool로 돌려주고, `night_run`이
  결론이 날 때까지 매 주기 다시 부른다(상한 5회). 무한 재시도는 안 한다.

### [P1] 매수 슬리피지가 이틀 연속 정확히 -0.96%

| 날짜 | 기준가(KIS last) | 체결가(pchs_avg_pric) | 슬리피지 |
|---|---|---|---|
| 7/28 | $27.45 | $27.19 | -0.96% |
| 7/29 | $27.70 | $27.44 | -0.96% |

표면상 유리하지만, 부호와 크기가 이틀 연속 같은 건 시장 슬리피지의 모습이 아니다.
시세 지연으로도 설명이 안 된다 — 두 번 다 QQQ 하락(=PSQ 상승) 구간 진입이라
지연이면 오히려 비싸게 사야 한다. KIS `last`와 `pchs_avg_pric` 사이의 계통 오차일
가능성이 높고, 그렇다면 `record_us_buy`가 저장하는 진입가가 실제보다 낮게 잡혀
저널 손익이 매 왕복 ~1%씩 부풀려진다. 원인 규명 전까지 수익으로 해석하지 말 것.

**수정 — 추정 대신 계측**

원인을 지금 확정할 근거가 없다. 대신 **갈라낼 데이터**를 남긴다:
`record_fill_slippage()`가 매수 체결마다 기준가·한도·체결평단에 더해
**체결 직후 재호가**를 `logs/us_slippage.json`에 누적한다(표본 500개 상한,
artifact로 run 간 보존). 0.5% 초과 괴리는 경고로 승격.

판정 기준:

| 재호가 위치 | 해석 |
|---|---|
| 체결가에 붙음 | 주문 시점 호가가 낡았던 것 = **지연** |
| 여전히 기준가 쪽 | `last`와 `pchs_avg_pric`의 기준이 다름 = **계통 오차** |

한 밤만 더 모으면 갈린다. 매매 동작은 바꾸지 않는다.

### [P2] US 매도는 실체결가가 기록되지 않는다

`_sell_and_record()`가 `confirm_us_fill()`의 평단을 버리고(`filled, _ =`) 매도를
**주문 전 기준가**로 기록한다. 매수만 실체결가가 남는 비대칭. 다만 매도 후엔 잔고에
평단이 안 남으므로 해외 체결조회 API를 붙이지 않으면 근본 해결이 안 된다.

### 확인된 것 — PR #3 검증기 수정이 실전에서 동작

```
[체결 검증] 국내 0건 정상 | 해외 2건 검증제외(국내 ccld 조회로는 확인 불가)
```

예전이라면 PSQ 2건이 `ccld_not_found` 오탐으로 잡혀 경고가 나갔을 자리다.

### 운영 메모 — GitHub Actions 스케줄러 지연

7/29 09:46Z 이후 레포의 **모든** 워크플로가 약 5시간 발화하지 않았다. 미국장 세션은
개장(22:30 KST) 71분 뒤인 23:41 KST에야 기동했다. 워크플로 설정 문제가 아니라
(state=active, cron 정상) 플랫폼 측 배달 지연이다. cron 다중화로도 못 막는 구간이
있다는 뜻 — 개장 후 일정 시간 내 기동 실패를 감지할 별도 수단이 필요하다.

---

## 6. 토큰 캐시 artifact — 자격증명 노출 (2026-07-31)

### 발견 경위

PR #2에서 `include-hidden-files: true`를 넣어 토큰 캐시 업로드를 고쳤고, 7/29
로그에서 실제로 성공을 확인했다:

```
Artifact kis-token-cache has been successfully uploaded! Final size is 510 bytes.
```

그런데 같은 로그의 다운로드 쪽은 여전히 실패였다 — 토큰 캐시만이 아니라 **7개
전부**:

```
##[error]Unable to download artifact(s): Artifact not found for name: trade-log
##[error]Unable to download artifact(s): Artifact not found for name: kis-token-cache
##[error]Unable to download artifact(s): Artifact not found for name: us-positions-state
... (learning-data, bear-state, killswitch-state, ledger-db)
```

`actions/download-artifact@v4`는 artifact를 **run 단위로 스코프**한다. `run-id` +
`github-token` 없이 이름만 주면 현재 run이 만든 것만 찾는다. 레포도 이미 알고
있었다 — 워크플로 주석에 "artifact는 이전 run을 못 봐 매 run 리셋"이라 적혀 있고
`trades.csv`·`positions.json`은 journal repo에서 curl로 복원하는 우회로가 있다.
토큰 캐시엔 그 우회로가 없어서, 봇은 계속 run마다 토큰을 재발급받고 있었다.

### 더 중요한 문제

**이 레포는 public이다.** artifact는 레포 읽기 권한자면 누구나 내려받는다.
`.kis_token_cache.json`에는 `access_token`이 평문으로 들어 있고, 그 토큰은
**실계좌 주문 권한이 붙은 24시간짜리 자격증명**이다.

즉 "run 간 재사용이 안 된다"를 artifact 쪽으로 고치는 방향은 이 노출을 더 길게
유지할 뿐이다. 방향을 반대로 잡았다.

### 조치

1. **토큰 캐시 artifact 업로드·다운로드 전부 제거** — autotrader / us-night-trader
   / market-learn / status-check 4개 워크플로, 총 7개 스텝. run 안에서의 디스크
   캐시(원자적 쓰기·파일락·lock 안 재확인)는 그대로 둔다. 그건 ephemeral runner
   안에서만 살고 밖으로 안 나간다.
2. **"1분당 1회" 제한을 재시도로 흡수** — `_is_reissue_throttled()`가 제한 응답을
   식별하고 62초 대기 후 재시도한다(최대 3회).

2번이 필요한 이유: `get_token()`의 5번 단계("KIS API 실패 → 만료 안 된 캐시
폴백")는 run 간 캐시가 있다는 전제 위에 있었다. 그 전제가 애초에 거짓이었으니
**실환경에서 도달 불가능한 죽은 코드**였고, 제한에 걸리면 폴백할 캐시가 없어
`RuntimeError`가 그대로 올라와 인증이 통째로 죽었다. 캐시를 공유할 수 없다면
기다리는 수밖에 없다.

제한을 오인하지 않는 것도 중요하다 — 앱키 오류처럼 기다려도 안 풀리는 실패는
즉시 올려야 한다. 그래서 `_is_reissue_throttled()`는 성공·비제한 오류에
대해서는 False를 돌려주고, 테스트로 고정했다.

### 남은 권고 (코드 밖)

이미 업로드된 artifact가 retention 기간(1일) 동안 남아 있다. 노출 창을 닫으려면
**KIS 앱키·앱시크릿 재발급**을 검토할 것. 액세스 토큰 자체는 24시간이면 만료되지만,
그 사이 발급된 토큰으로 주문이 가능했다.
