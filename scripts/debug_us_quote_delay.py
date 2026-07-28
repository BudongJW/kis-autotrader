"""US 시세 지연 진단 — KIS 해외 시세가 실시간인지 지연(보통 15분)인지 판정.

**읽기 전용.** 주문 함수는 호출하지 않는다.

왜 필요한가
-----------
KIS 해외 시세는 계정에 실시간 시세가 신청돼 있지 않으면 지연 시세로 내려온다.
지연 시세로 매매하면 마켓터블 지정가를 써도 **기준가 자체가 낡아** 슬리피지가
남는다. 특히 US 모멘텀 스캘프(진입창 90분, 트레일 0.8%, 손절 2.5%)는 15분
지연에서는 구조적으로 성립하지 않는다 — 신호를 본 시점에 이미 그 움직임이
끝나 있기 때문이다.

판정 방법
---------
KIS `output`에는 시세 지연 여부를 알려주는 필드가 없다. 그래서 두 가지를
**독립 소스(yfinance 1분봉)** 와 대조해 경험적으로 추정한다:

  1. 가격 정합: KIS `last`가 최근 몇 분봉의 종가 중 어디와 가장 가까운가
     → 그 분봉이 몇 분 전인지가 지연 추정치
  2. 누적 거래량: KIS `tvol` vs yfinance 당일 누적 거래량
     → 지연이면 KIS 쪽이 체계적으로 작다

정규장 중에 실행해야 의미가 있다. 장외에는 양쪽 다 마지막 종가로 고정된다.

실행
----
  python -m scripts.debug_us_quote_delay
  (GitHub Actions: debug-once.yml 워크플로에 scripts.debug_us_quote_delay 입력)
"""

from __future__ import annotations

import json
from datetime import timedelta

from src.bot.us_session import measure_quote_lag
from src.kis_client import KISClient
from src.utils.clock import ET, now_et, now_kst
from src.utils.market_calendar import is_us_trading_day, us_market_times_kst

PROBES = [("SPLG", "AMEX"), ("QQQM", "NASD"), ("SPY", "AMEX")]


def _kis_quote(client: KISClient, symbol: str, exchange: str) -> dict:
    resp = client.get_overseas_price(symbol, exchange=exchange)
    out = resp.get("output") or {}
    if isinstance(out, list):
        out = out[0] if out else {}
    return {"rt_cd": resp.get("rt_cd"), "msg1": resp.get("msg1", ""), "output": out}


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _yf_minute_bars(symbol: str):
    """오늘 1분봉 (ET 인덱스). 실패 시 None."""
    try:
        import yfinance as yf

        df = yf.download(symbol, period="1d", interval="1m",
                         auto_adjust=False, progress=False)
        if df is None or df.empty:
            return None
        import pandas as pd

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ET)
        return df
    except Exception as e:  # noqa: BLE001
        print(f"    yfinance 조회 실패: {e}")
        return None


# 지연 추정 본체는 봇과 동일 구현을 쓴다(us_session.measure_quote_lag) —
# 진단 결과와 실제 봇 동작이 갈리지 않도록.


def main() -> None:
    now_e, now_k = now_et(), now_kst()
    open_k, close_k = us_market_times_kst()
    session_open = is_us_trading_day()

    print("=" * 68)
    print("US 시세 지연 진단 (읽기 전용 — 주문 없음)")
    print("=" * 68)
    print(f"  현재  KST {now_k:%Y-%m-%d %H:%M:%S} | ET {now_e:%Y-%m-%d %H:%M:%S}")
    print(f"  세션  개장 {open_k:%H:%M} ~ 폐장 {close_k:%H:%M} KST | 거래일={session_open}")

    t = now_k.time()
    in_hours = (t >= open_k or t <= close_k) if open_k > close_k else (open_k <= t <= close_k)
    if not (session_open and in_hours):
        print("\n  ⚠️  정규장 시간이 아니다. 지연 추정은 장중에만 의미가 있다")
        print("     (장외에는 KIS·yfinance 모두 마지막 종가로 고정).")
        print("     아래는 원시 응답 필드 덤프만 참고.\n")

    client = KISClient()
    verdict_lags: list[float] = []

    for symbol, exchange in PROBES:
        print("-" * 68)
        print(f"[{symbol}] ({exchange})")
        q = _kis_quote(client, symbol, exchange)
        if q["rt_cd"] != "0":
            print(f"  KIS 조회 실패 rt_cd={q['rt_cd']} msg={q['msg1']}")
            continue

        out = q["output"]
        kis_last = _f(out.get("last"))
        kis_tvol = _f(out.get("tvol"))
        print(f"  KIS  last=${kis_last:,.2f}  base(전일종가)=${_f(out.get('base')):,.2f}  "
              f"tvol={kis_tvol:,.0f}")
        # 지연 표시 필드가 있는지 전체 키 덤프 (KIS 문서에 명시된 플래그가 없어 육안 확인)
        print(f"  원시 output 키: {sorted(out.keys())}")

        lag_min, detail = measure_quote_lag(client, symbol, exchange)
        if lag_min is None:
            print(f"  지연 추정 불가: {detail.get('error', 'unknown')}")
        else:
            print(f"  참조 yfinance last=${detail['ref_last']:,.2f} "
                  f"(마지막 분봉 {detail['ref_bar_et']} ET, "
                  f"최근접 분봉 {detail['matched_bar_et']} ET)")
            print(f"  가격차(KIS-참조): {detail['price_gap_pct']:+.3f}%")
            print(f"  → 추정 지연: 약 {lag_min:.0f}분")
            verdict_lags.append(lag_min)

        bars = _yf_minute_bars(symbol)
        if bars is not None and not bars.empty:
            ref_vol = float(bars["Volume"].sum())
            if ref_vol > 0 and kis_tvol > 0:
                print(f"  거래량비(KIS/참조): {kis_tvol / ref_vol:.2f} "
                      f"(1.0에 가까우면 실시간, 뚜렷이 작으면 지연 의심)")

    print("=" * 68)
    if not verdict_lags:
        print("판정: 데이터 부족 — 정규장 중에 다시 실행할 것.")
    else:
        med = sorted(verdict_lags)[len(verdict_lags) // 2]
        print(f"판정: 추정 지연 중앙값 {med:.0f}분")
        if med >= 10:
            print("  → 지연 시세로 보인다(전형적으로 15분).")
            print("     KIS Developers에서 해외 실시간 시세 신청을 검토하고,")
            print("     신청 전까지는 configs/strategy.yaml us_session.execution의")
            print("     quote_lag_min을 실측값으로 설정해 방어 모드를 켤 것.")
        elif med >= 3:
            print("  → 경미한 지연. quote_lag_min 설정 권장.")
        else:
            print("  → 실시간에 가깝다. 추가 조치 불필요.")
    print("=" * 68)
    print("\n참고: 이 판정은 yfinance를 참조로 한 경험적 추정이다. yfinance 자체도")
    print("거래소에 따라 지연될 수 있으므로, 값이 애매하면 KIS 고객센터/포털에서")
    print("계정의 해외 실시간 시세 신청 상태를 직접 확인하는 편이 확실하다.")


if __name__ == "__main__":
    main()
