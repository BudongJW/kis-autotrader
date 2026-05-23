"""미국 개별주 펀더멘털 게이트 — 기본 체력 검증 후 진입 허용.

yfinance로 EPS 성장률, PER, 매출 성장률 등을 확인.
ETF는 게이트 면제, 개별주(NVDA, AAPL 등)만 적용.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.utils.logger import log

ETF_TICKERS = {"QQQ", "SPY", "SQQQ", "TLT", "GLD", "SHY", "IEF", "TQQQ"}


@dataclass
class FundamentalCheck:
    passed: bool
    reason: str
    eps_growth: float | None = None
    pe_ratio: float | None = None
    revenue_growth: float | None = None


def check_fundamentals(ticker: str) -> FundamentalCheck:
    """yfinance로 개별주 펀더멘털 게이트 확인.

    통과 조건 (하나라도 실패 시 차단):
      - EPS YoY 성장 > -20% (실적 급락 아닌지)
      - Forward PER < 80 (과도한 고평가 아닌지)
      - 매출 성장 > -30% (사업 축소 아닌지)

    ETF는 무조건 통과.
    """
    if ticker.upper() in ETF_TICKERS:
        return FundamentalCheck(passed=True, reason="ETF — 펀더멘털 게이트 면제")

    try:
        import yfinance as yf
    except ImportError:
        return FundamentalCheck(passed=True, reason="yfinance 미설치, 게이트 스킵")

    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        pe = info.get("forwardPE") or info.get("trailingPE")
        eps_growth = info.get("earningsQuarterlyGrowth")
        rev_growth = info.get("revenueGrowth")

        reasons = []

        if pe is not None and pe > 80:
            reasons.append(f"PER={pe:.1f} > 80")

        if eps_growth is not None and eps_growth < -0.20:
            reasons.append(f"EPS 성장={eps_growth:+.0%} < -20%")

        if rev_growth is not None and rev_growth < -0.30:
            reasons.append(f"매출 성장={rev_growth:+.0%} < -30%")

        if reasons:
            return FundamentalCheck(
                passed=False,
                reason=f"펀더멘털 차단: {', '.join(reasons)}",
                eps_growth=eps_growth,
                pe_ratio=pe,
                revenue_growth=rev_growth,
            )

        detail_parts = []
        if pe is not None:
            detail_parts.append(f"PER={pe:.1f}")
        if eps_growth is not None:
            detail_parts.append(f"EPS={eps_growth:+.0%}")
        if rev_growth is not None:
            detail_parts.append(f"매출={rev_growth:+.0%}")

        return FundamentalCheck(
            passed=True,
            reason=f"펀더멘털 통과 ({', '.join(detail_parts) or 'N/A'})",
            eps_growth=eps_growth,
            pe_ratio=pe,
            revenue_growth=rev_growth,
        )

    except Exception as e:
        log.warning("fundamental_check_failed", ticker=ticker, error=str(e))
        return FundamentalCheck(passed=True, reason=f"펀더멘털 조회 실패 ({e}), 게이트 스킵")
