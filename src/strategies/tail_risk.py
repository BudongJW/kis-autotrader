"""포트폴리오 테일 리스크 모니터링 — VaR / Expected Shortfall.

Historical Simulation 방식으로 포트폴리오 VaR과 ES를 계산.
일일 리스크 예산 초과 시 신규 매수를 차단하거나 포지션을 축소.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.utils.logger import log

RISK_REPORT_PATH = Path("logs/tail_risk.json")

# VaR 임계값 (포트폴리오 대비 %)
VAR_LIMIT_PCT = 0.05       # 1일 95% VaR이 포트폴리오의 5% 초과 시 경고
ES_CRITICAL_PCT = 0.08     # ES가 8% 초과 시 신규 매수 차단


@dataclass
class TailRiskReport:
    """테일 리스크 리포트."""
    date: str
    var_95: float           # 95% VaR (손실 기준, 양수=손실)
    var_99: float           # 99% VaR
    es_95: float            # 95% Expected Shortfall (CVaR)
    portfolio_vol: float    # 연환산 변동성
    max_drawdown: float     # 최대 낙폭
    risk_level: str         # "normal" / "elevated" / "critical"
    size_mult: float        # 포지션 크기 배율
    detail: str


def compute_portfolio_var(
    held_histories: dict[str, pd.DataFrame],
    weights: dict[str, float] | None = None,
    window: int = 60,
    confidence: float = 0.95,
) -> TailRiskReport:
    """Historical Simulation VaR/ES 계산.

    Args:
        held_histories: {심볼: OHLCV DataFrame}
        weights: {심볼: 비중} (없으면 동일 가중)
        window: 시뮬레이션 기간 (거래일)
        confidence: VaR 신뢰 수준
    """
    today_str = datetime.now().strftime("%Y-%m-%d")

    returns_dict = {}
    for sym, hist in held_histories.items():
        if hist is None or len(hist) < 20:
            continue
        close = hist["close"].astype(float)
        ret = close.pct_change().dropna().tail(window)
        if len(ret) >= 15:
            returns_dict[sym] = ret.values

    if not returns_dict:
        return TailRiskReport(
            date=today_str, var_95=0, var_99=0, es_95=0,
            portfolio_vol=0, max_drawdown=0,
            risk_level="normal", size_mult=1.0,
            detail="보유 종목 없음",
        )

    min_len = min(len(v) for v in returns_dict.values())
    symbols = list(returns_dict.keys())
    aligned = np.array([returns_dict[s][-min_len:] for s in symbols])

    if weights:
        w = np.array([weights.get(s, 1.0 / len(symbols)) for s in symbols])
    else:
        w = np.ones(len(symbols)) / len(symbols)
    w = w / w.sum()

    # 포트폴리오 일간 수익률
    port_returns = aligned.T @ w

    # VaR (Historical Simulation)
    var_95 = -float(np.percentile(port_returns, (1 - 0.95) * 100))
    var_99 = -float(np.percentile(port_returns, (1 - 0.99) * 100))

    # Expected Shortfall (CVaR)
    threshold = np.percentile(port_returns, (1 - confidence) * 100)
    tail = port_returns[port_returns <= threshold]
    es_95 = -float(np.mean(tail)) if len(tail) > 0 else var_95

    # 변동성 (연환산)
    portfolio_vol = float(np.std(port_returns) * np.sqrt(252))

    # 최대 낙폭
    cum_returns = np.cumprod(1 + port_returns)
    peak = np.maximum.accumulate(cum_returns)
    drawdowns = (cum_returns - peak) / peak
    max_dd = float(np.min(drawdowns))

    # 리스크 레벨 판정
    if es_95 >= ES_CRITICAL_PCT:
        risk_level = "critical"
        size_mult = 0.3
        detail = f"VaR₉₅={var_95:.2%} ES₉₅={es_95:.2%} — 테일 리스크 위험, 포지션 대폭 축소"
    elif var_95 >= VAR_LIMIT_PCT:
        risk_level = "elevated"
        size_mult = 0.6
        detail = f"VaR₉₅={var_95:.2%} ES₉₅={es_95:.2%} — 리스크 상승, 포지션 축소"
    else:
        risk_level = "normal"
        size_mult = 1.0
        detail = f"VaR₉₅={var_95:.2%} ES₉₅={es_95:.2%} — 리스크 정상"

    report = TailRiskReport(
        date=today_str,
        var_95=round(var_95, 4),
        var_99=round(var_99, 4),
        es_95=round(es_95, 4),
        portfolio_vol=round(portfolio_vol, 4),
        max_drawdown=round(max_dd, 4),
        risk_level=risk_level,
        size_mult=size_mult,
        detail=detail,
    )

    _save_risk_report(report)
    return report


def get_tail_risk_adjustment() -> tuple[float, str]:
    """저장된 테일 리스크 기반 포지션 조정값 반환.

    Returns:
        (size_multiplier, reason)
    """
    if not RISK_REPORT_PATH.exists():
        return 1.0, "테일 리스크 데이터 없음"

    try:
        data = json.loads(RISK_REPORT_PATH.read_text(encoding="utf-8"))
        current = data.get("current", {})
        if current.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return 1.0, "테일 리스크 미갱신 (어제 데이터)"
        return current.get("size_mult", 1.0), current.get("detail", "")
    except Exception:
        return 1.0, "테일 리스크 로드 실패"


def _save_risk_report(report: TailRiskReport) -> None:
    RISK_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    if RISK_REPORT_PATH.exists():
        try:
            data = json.loads(RISK_REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    data["current"] = {
        "date": report.date,
        "var_95": report.var_95,
        "var_99": report.var_99,
        "es_95": report.es_95,
        "portfolio_vol": report.portfolio_vol,
        "max_drawdown": report.max_drawdown,
        "risk_level": report.risk_level,
        "size_mult": report.size_mult,
        "detail": report.detail,
    }

    history = data.get("history", [])
    history.append({
        "date": report.date,
        "var_95": report.var_95,
        "es_95": report.es_95,
        "risk_level": report.risk_level,
    })
    if len(history) > 90:
        history = history[-90:]
    data["history"] = history

    RISK_REPORT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
