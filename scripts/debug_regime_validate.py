"""HMM 레짐 방향 예측력 워크포워드 검증.

'HMM 확신도/상태'가 실제로 다음날 방향을 맞히는지 out-of-sample로 채점.
각 시점 t까지의 데이터로만 레짐을 판단하고(과거만 사용), t+1 실제 방향과 대조한다.
55% 넘으면 예측력 있음, 50% 근처면 동전던지기(레짐≠방향예측).
debug-once: script=scripts.debug_regime_validate
"""
from __future__ import annotations

import pandas as pd

from src.strategies.hmm_regime import detect_regime


def _load_kospi() -> pd.Series:
    import yfinance as yf
    for tkr in ("^KS200", "^KS11", "069500.KS"):
        try:
            df = yf.download(tkr, period="3y", interval="1d",
                             auto_adjust=True, progress=False)
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                s = df["Close"].dropna()
                if len(s) > 200:
                    print(f"데이터: {tkr} {len(s)}일 ({s.index[0].date()} ~ {s.index[-1].date()})")
                    return s
        except Exception as e:  # noqa: BLE001
            print(f"  {tkr} 실패: {e}")
    raise RuntimeError("KOSPI 일봉 로드 실패")


def main() -> None:
    print("=" * 60)
    print("HMM 레짐 → 다음날 방향 예측력 검증 (워크포워드)")
    print("=" * 60)
    close = _load_kospi()
    returns = close.pct_change().dropna()

    up_rate = float((returns > 0).mean())
    print(f"기준선(상승일 비율): {up_rate:.1%}  <- '무조건 롱'의 적중률(비교 기준)")

    hits = {"bull": [0, 0], "bear": [0, 0], "sideways": [0, 0]}
    dir_h = [0, 0]      # 방향판단(bull=up 예측, bear=down 예측)만 집계
    conf_h = [0, 0]     # 고확신(>0.85) 방향판단 적중
    n = len(returns)
    tests = list(range(70, n - 1))
    # 너무 많으면 균등 샘플링(HMM 재학습 비용)
    if len(tests) > 180:
        step = len(tests) // 180
        tests = tests[::step]
    print(f"검증 시점 수: {len(tests)} (각 시점 과거데이터만으로 레짐 판단)")

    for t in tests:
        try:
            r = detect_regime(returns.iloc[:t + 1])
        except Exception:
            continue
        nxt = float(returns.iloc[t + 1])
        st = r.state
        if st == "bull":
            ok = nxt > 0
        elif st == "bear":
            ok = nxt < 0
        else:
            hits["sideways"][1] += 1
            continue
        hits[st][0] += int(ok); hits[st][1] += 1
        dir_h[0] += int(ok); dir_h[1] += 1
        if r.confidence > 0.85:
            conf_h[0] += int(ok); conf_h[1] += 1

    print("\n[상태별 다음날 방향 적중률]")
    for st in ("bull", "bear"):
        h, tot = hits[st]
        if tot:
            print(f"  {st:<8} {h}/{tot} = {h / tot:.1%}  (예측: {'상승' if st=='bull' else '하락'})")
    print(f"  sideways(방향판단 안함): {hits['sideways'][1]}건")
    if dir_h[1]:
        print(f"\n  ▶ 방향판단 전체 적중률: {dir_h[0]}/{dir_h[1]} = {dir_h[0]/dir_h[1]:.1%}")
    if conf_h[1]:
        print(f"  ▶ 고확신(>85%)만: {conf_h[0]}/{conf_h[1]} = {conf_h[0]/conf_h[1]:.1%}")
    print("\n판정: 55%+ 면 예측력 있음(튜닝 가치). 50% 근처면 레짐은 방향예측이 아님(동전던지기).")


if __name__ == "__main__":
    main()
