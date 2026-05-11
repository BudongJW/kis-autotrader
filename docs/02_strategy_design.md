# 02 · 전략 설계 원칙

새 전략을 추가하기 전에 읽을 것.

## 좋은 전략의 5가지 속성

1. **단순함** — 룰이 3~5개 안에 설명 가능
2. **검증 가능성** — 과거 데이터로 백테스트 가능
3. **로버스트성** — 파라미터를 살짝 바꿔도 결과가 크게 안 변함 (과적합 안 됨)
4. **거래비용 견딤** — 수수료 + 세금 + 슬리피지 차감 후에도 양의 기댓값
5. **심리적 견딤** — MDD가 본인이 견딜 수 있는 범위 (보통 -15% 이내)

## 새 전략 추가 절차

### 1) 가설 명시
"이런 시장 비효율성이 존재한다고 본다. 이걸 룰화하면 잡을 수 있을 것이다."
- 예: "장기 횡보 후 거래량 폭증 종가 양봉은 다음날도 오를 확률이 높다"

### 2) 룰 정의
- 진입 조건 (BUY)
- 청산 조건 (SELL)
- 손절/익절
- 포지션 크기

### 3) 코드화

```python
# src/strategies/my_strategy.py
from src.strategies.base import BaseStrategy, Signal, SignalType

class MyStrategy(BaseStrategy):
    name = "my_strategy"
    required_lookback = 60

    def __init__(self, **params):
        self.params = params

    def generate_signal(self, symbol, history):
        # 룰 구현
        ...
```

### 4) 단위 테스트

```python
# tests/test_my_strategy.py
def test_buy_signal_on_breakout():
    strat = MyStrategy()
    history = _make_breakout_pattern()
    signal = strat.generate_signal("TEST", history)
    assert signal.type == SignalType.BUY
```

### 5) 백테스트 (in-sample)

```bash
python -m src.backtest.runner --strategy my_strategy \
    --symbol 005930 --from 2020-01-01 --to 2022-12-31
```

평가:
- 수익률 ≥ 같은 기간 KOSPI 수익률 + 5%
- MDD ≤ -20%
- Sharpe ≥ 0.8
- 거래 횟수가 너무 적으면 (10건 미만) 통계적 유의성 부족

### 6) 백테스트 (out-of-sample)

위 in-sample 기간과 겹치지 않는 별도 기간:
```bash
python -m src.backtest.runner --strategy my_strategy \
    --symbol 005930 --from 2023-01-01 --to 2024-12-31
```

**in-sample 결과의 50% 이상이면 통과**. 그 미만이면 과적합 의심 → 파라미터 단순화.

### 7) 모의투자 (paper) 운영

```bash
python -m src.bot.runner --strategy my_strategy --symbol 005930
```

1주일 ~ 1개월 운영. 백테스트와 결과 괴리 측정:
- 신호 발생 시점 일치 여부
- 체결가와 신호가의 차이 (슬리피지)
- 누적 수익률 괴리

**괴리가 30% 이내**면 실전 진입 검토.

### 8) 실전 소액 ($200~$500)

```bash
# .env에 MODE=live로 변경 후
python -m src.bot.runner --strategy my_strategy --symbol 005930 --live
```

`confirm_live_mode()`에서 "yes" 입력 필요.

1개월 운영 후 결과 분석. 양호하면 자본 증액 검토.

## 흔한 실수

1. **과적합** — 백테스트 파라미터 100개 중 가장 좋은 것 선택. 실전에선 그 종목 그 기간에만 잘 됨
   - 해결: 파라미터 sweep 그래프 그리기. 최고점이 너무 뾰족하면 위험
2. **survivorship bias** — 지금 상장된 종목만 백테스트. 상장폐지된 종목 제외돼서 결과 과장
   - 해결: `pykrx`로 과거 시점의 상장 종목 리스트 사용
3. **look-ahead bias** — 미래 데이터를 신호 생성에 사용
   - 해결: `history.iloc[:i+1]` 형태로 시점 명확히
4. **slippage 무시** — 시장가 주문 시 호가차이로 0.05~0.3% 손실
   - 해결: 백테스트에 slippage 모델 추가
5. **수수료 + 세금 무시** — 일평균 거래 많으면 수익이 다 빠짐
   - 해결: 단타 전략은 수수료 영향 큼. 1회당 0.3% 이상 마진 필요

## 추천 전략 진화 경로

```
1. golden_cross (현재) — 가장 단순, 추세추종
       ↓
2. golden_cross + trend filter — 장기 추세 위에서만 진입 (휩쏘 감소)
       ↓
3. momentum — N일 수익률 상위 종목 다종목 보유
       ↓
4. mean reversion — 과매도 종목 자동 매수 (단기, 횡보장 강함)
       ↓
5. multi-strategy ensemble — 추세 + 평균회귀 동시 운영, 시장 국면 따라 비중
       ↓
6. ML 기반 — 위 전략들을 feature로 한 분류·회귀 모델
```

## Out-of-the-Box 전략 (공식 backtester 활용)

공식 `koreainvestment/open-trading-api`의 strategy_builder가 제공하는 10개 프리셋:

1. 골든크로스 (이미 구현됨)
2. 모멘텀 (recent N-day return)
3. 52주 신고가 돌파
4. 연속 상승/하락
5. 이격도 (price / MA)
6. 돌파 실패 (false breakout 손절)
7. 강한 종가 (종가 ≈ 고가)
8. 변동성 확장
9. 평균회귀
10. 추세 필터

각각을 `.kis.yaml`로 export 가능. 우리 프로젝트의 `BaseStrategy`로 포팅하는 작업을 Claude Code에게 부탁할 수 있다.

## 자본 배분 원칙 (configs/strategy.yaml의 risk)

- **종목 1개 최대 비중**: 자본의 20% (`max_position_weight: 0.20`)
- **동시 포지션 최대 수**: 5종목
- **종목별 손절**: -3%
- **종목별 익절**: +5%
- **일일 손실 한도**: -5% 도달 시 봇 자동 정지

이 룰은 골든크로스 같은 단순 전략용. 다른 전략은 백테스트 결과에 맞춰 조정.
