"""오버나이트 갭 전략 — 미국장 종가 → 한국장 방향 예측.

한국 시장은 미국 시장 마감(06:00 KST) 후 3시간 뒤에 열린다.
미국 장 방향은 한국 시장 갭(시가-전종가)에 강한 영향을 미친다.
이 정보 비대칭을 활용하여 매수 진입 판단의 신뢰도를 높인다.

사용:
    장 전 08:30 market-learn에서 호출:
    signal = get_overnight_signal()
    → strategy.yaml에 저장 → 장 중 single_run.py에서 참조

데이터 소스:
    1. yfinance (Yahoo Finance) — NASDAQ, S&P500 종가
    2. 폴백: 한국 야간 선물 데이터 (KIS API)
    3. 폴백: 전일 해외 ETF 변동으로 추정
"""

from __future__ import annotations

from dataclasses import dataclass

from src.utils.logger import log


@dataclass
class OvernightSignal:
    """미국장 종가 기반 오버나이트 신호."""
    nasdaq_change: float      # NASDAQ 전일 대비 변동률 (%)
    sp500_change: float       # S&P500 전일 대비 변동률 (%)
    direction: str            # "bullish" / "bearish" / "neutral"
    strength: float           # 신호 강도 (0.0 ~ 1.0)
    confidence_boost: float   # 시장 신뢰도에 더할 값 (-0.2 ~ +0.2)
    recommended_action: str   # "aggressive_buy" / "normal" / "reduce_size" / "skip"
    detail: str

    @property
    def as_dict(self) -> dict:
        return {
            "nasdaq_change": round(self.nasdaq_change, 2),
            "sp500_change": round(self.sp500_change, 2),
            "direction": self.direction,
            "strength": round(self.strength, 2),
            "confidence_boost": round(self.confidence_boost, 3),
            "recommended_action": self.recommended_action,
        }


def _fetch_us_close_yfinance() -> dict[str, float]:
    """yfinance로 미국 지수 전일 종가 변동률 가져오기."""
    try:
        import yfinance as yf

        tickers = yf.download(
            "^IXIC ^GSPC",
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        closes = tickers["Close"]

        result = {}
        for col, name in [("^IXIC", "nasdaq"), ("^GSPC", "sp500")]:
            if col in closes.columns:
                valid = closes[col].dropna()
                if len(valid) >= 2:
                    prev = float(valid.iloc[-2])
                    last = float(valid.iloc[-1])
                    result[name] = round((last - prev) / prev * 100, 2)

        return result
    except Exception as e:
        log.warning("yfinance_fetch_failed", error=str(e))
        return {}


def _fetch_us_close_from_etf(client) -> dict[str, float]:
    """폴백: 한국 상장 해외지수 ETF의 전일 변동으로 추정.

    KODEX 미국나스닥100TR(395160)의 최근 2일 종가 비교.
    완벽하진 않지만 yfinance 실패 시 대안.
    """
    try:
        from src.bot.runner import fetch_recent_history

        hist = fetch_recent_history(client, "395160", days=5)
        if len(hist) >= 2:
            close = hist["close"].astype(float)
            change = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
            return {"nasdaq": round(change, 2), "sp500": round(change * 0.8, 2)}
    except Exception:
        pass
    return {}


def get_overnight_signal(client=None) -> OvernightSignal:
    """미국장 종가 기반 오버나이트 신호 생성.

    Args:
        client: KISClient (ETF 폴백용). None이면 yfinance만 시도.

    Returns:
        OvernightSignal
    """
    # 1차: yfinance
    us_data = _fetch_us_close_yfinance()

    # 2차 폴백: 한국 ETF 프록시
    if not us_data and client:
        us_data = _fetch_us_close_from_etf(client)

    if not us_data:
        return OvernightSignal(
            nasdaq_change=0, sp500_change=0,
            direction="neutral", strength=0,
            confidence_boost=0,
            recommended_action="normal",
            detail="미국 시장 데이터 조회 실패 — 기본값 사용",
        )

    nasdaq = us_data.get("nasdaq", 0)
    sp500 = us_data.get("sp500", 0)

    # 방향 & 강도 판단
    avg_change = (nasdaq * 0.6 + sp500 * 0.4)  # 나스닥에 가중치

    if avg_change > 1.5:
        direction = "bullish"
        strength = min(1.0, avg_change / 3.0)
        confidence_boost = min(0.2, avg_change * 0.06)
        action = "aggressive_buy"
        detail = f"미국 강세 (NASDAQ {nasdaq:+.1f}%, S&P500 {sp500:+.1f}%) → 적극 매수"
    elif avg_change > 0.5:
        direction = "bullish"
        strength = avg_change / 3.0
        confidence_boost = avg_change * 0.04
        action = "normal"
        detail = f"미국 소폭 상승 (NASDAQ {nasdaq:+.1f}%, S&P500 {sp500:+.1f}%) → 정상 매매"
    elif avg_change < -1.5:
        direction = "bearish"
        strength = min(1.0, abs(avg_change) / 3.0)
        confidence_boost = max(-0.2, avg_change * 0.06)
        action = "skip" if avg_change < -2.5 else "reduce_size"
        detail = f"미국 급락 (NASDAQ {nasdaq:+.1f}%, S&P500 {sp500:+.1f}%) → 매수 자제"
    elif avg_change < -0.5:
        direction = "bearish"
        strength = abs(avg_change) / 3.0
        confidence_boost = avg_change * 0.04
        action = "reduce_size"
        detail = f"미국 소폭 하락 (NASDAQ {nasdaq:+.1f}%, S&P500 {sp500:+.1f}%) → 규모 축소"
    else:
        direction = "neutral"
        strength = 0.1
        confidence_boost = 0
        action = "normal"
        detail = f"미국 보합 (NASDAQ {nasdaq:+.1f}%, S&P500 {sp500:+.1f}%) → 정상 매매"

    return OvernightSignal(
        nasdaq_change=nasdaq,
        sp500_change=sp500,
        direction=direction,
        strength=strength,
        confidence_boost=confidence_boost,
        recommended_action=action,
        detail=detail,
    )
