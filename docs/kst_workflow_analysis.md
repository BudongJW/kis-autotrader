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
