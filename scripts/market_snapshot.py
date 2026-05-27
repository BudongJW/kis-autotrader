"""실시간 시장 스냅샷 — 봇이 지금 보고 있을 풍경 그대로 재현.

진행 중 한국장 봇은 5분마다 전략 체크하는데 portfolio.json엔 마감 후에만
반영. 이 스크립트로 현재 시점 시세·시장 상태를 즉시 확인.

출력:
  - 현재 시각 (KST), 마감까지 남은 시간
  - 시장 환경 (학습된 regime, confidence, VIX, 갭)
  - 14개 ETF 각각: 현재가, 변동성 돌파 목표가, 돌파 여부
  - 강신호 후보 (돌파 + TA + 융합 충족) 추정
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import yaml

from src.kis_client import KISClient
from src.bot.runner import fetch_recent_history
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.strategies.ta_composite import compute_ta_score

KST = ZoneInfo("Asia/Seoul")
MARKET_CLOSE = dtime(15, 30)


def main() -> None:
    client = KISClient()
    now = datetime.now(KST)
    t = now.time()

    print("=" * 70)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] 시장 스냅샷")
    if t < MARKET_CLOSE:
        remain = (datetime.combine(now.date(), MARKET_CLOSE, tzinfo=KST) - now).total_seconds() / 60
        print(f"  한국장 마감까지: {remain:.0f}분 남음")
    else:
        print(f"  한국장 마감됨")
    print("=" * 70)

    # 시장 환경 (학습된 결과)
    with open("configs/strategy.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    regime = cfg.get("market_regime", {})
    print(f"\n[시장 환경 — 학습 결과]")
    print(f"  추세: {regime.get('trend')} (점수 {regime.get('trend_score')})")
    print(f"  변동성: {regime.get('volatility')} (백분위 {regime.get('vol_percentile')}%)")
    print(f"  HMM 레짐: {regime.get('hmm_state')} (신뢰도 {regime.get('hmm_confidence', 0):.1%})")
    print(f"  시장 신뢰도: {cfg.get('market_confidence', 0):.1%}")
    gap = cfg.get("overnight_signal", {})
    print(f"  오버나이트 갭: {gap.get('direction')} (강도 {gap.get('strength')}, "
          f"NASDAQ {gap.get('nasdaq_change', 0):+.1f}%)")

    # VIX
    try:
        from src.strategies.vix_filter import get_vix_filter
        vix = get_vix_filter()
        if vix:
            print(f"  VIX: {vix.value:.1f} ({vix.band}) — {vix.detail.split('—')[1].strip()}")
    except Exception:
        pass

    # 14개 ETF universe 분석
    universe = cfg.get("universe", {}).get("default", [])
    dynamic = cfg.get("dynamic_universe", [])
    all_etfs = universe + [d for d in dynamic if d["symbol"] not in {u["symbol"] for u in universe}]

    params = cfg.get("strategies", {}).get("volatility_breakout", {})
    k = params.get("k", 0.5)
    ma = params.get("trend_ma", 20)
    strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)

    print(f"\n[ETF 시세 + 변동성 돌파 분석 — K={k}, MA={ma}]")
    print(f"  {'종목':<28} {'현재가':>10} {'목표가':>10} {'돌파':>4} {'TA':>6} {'신호':>10}")
    print(f"  {'-' * 70}")

    candidates = []
    for stock in all_etfs[:12]:  # 상위 12개만 (속도)
        sym = stock["symbol"]
        name = stock["name"]
        try:
            history = fetch_recent_history(client, sym, days=70)
            signal = strategy.generate_signal(sym, history)
            ta = compute_ta_score(history)
            cur = int(signal.price)
            target_str = signal.reason.split("목표 ")[1].rstrip(")") if "목표 " in signal.reason else "?"
            breakout = "✓" if signal.type.value == "BUY" else "X"
            ta_str = f"{ta.total:+.0f}"
            verdict = ""
            if signal.type.value == "BUY" and ta.total >= 0:
                verdict = "🟢 매수 후보"
                candidates.append((name, cur, ta.total))
            elif ta.total >= 20:
                verdict = "🟡 TA 강세"
            elif signal.type.value == "BUY":
                verdict = "🟡 돌파"

            print(f"  {name[:26]:<28} {cur:>10,} {target_str:>10} {breakout:>4} {ta_str:>6} {verdict:>10}")
        except Exception as e:
            print(f"  {name[:26]:<28} ERROR: {str(e)[:30]}")

    if candidates:
        print(f"\n[🎯 봇 매수 후보 (현재 시점)]")
        for name, price, ta_score in candidates:
            print(f"  - {name} @ {price:,}원 (TA {ta_score:+.0f})")
        print(f"\n  → 다음 5분 전략 체크 사이클에 봇이 매수 시도 가능")
    else:
        print(f"\n[💤 현재 매수 후보 없음]")
        print(f"  → 변동성 돌파 통과 + TA 양수인 종목 없음")
        print(f"  → 봇이 신호 대기 중. 다음 가격 변화에 따라 매수 가능성")


if __name__ == "__main__":
    main()
