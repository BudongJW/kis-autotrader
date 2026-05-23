"""포트폴리오 상관관계 가드 — 종목간 상관계수 기반 분산 강제.

보유 종목들의 수익률 상관관계가 높으면 시스템 리스크 집중.
신규 매수 시 기존 보유와 상관관계 체크 → 높으면 거부 또는 축소.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.utils.logger import log

CORR_THRESHOLD_BLOCK = 0.85
CORR_THRESHOLD_REDUCE = 0.65


@dataclass
class CorrelationCheck:
    """상관관계 체크 결과."""
    allowed: bool
    max_corr: float
    corr_with: str        # 가장 상관관계 높은 기존 보유 심볼
    size_mult: float      # 포지션 크기 배율 (1.0=정상, <1.0=축소)
    detail: str


def compute_pairwise_correlation(
    histories: dict[str, pd.DataFrame],
    window: int = 20,
) -> pd.DataFrame:
    """종목별 일간 수익률 상관행렬 계산.

    Args:
        histories: {심볼: OHLCV DataFrame (close 컬럼 필수)}
        window: 상관관계 계산 기간 (거래일)

    Returns:
        상관행렬 DataFrame
    """
    returns = {}
    for sym, hist in histories.items():
        if hist is None or len(hist) < window:
            continue
        close = hist["close"].astype(float)
        ret = close.pct_change().dropna().tail(window)
        if len(ret) >= window - 2:
            returns[sym] = ret.values[:min(len(r) for r in returns.values())] if returns else ret.values

    if len(returns) < 2:
        return pd.DataFrame()

    min_len = min(len(v) for v in returns.values())
    aligned = {k: v[-min_len:] for k, v in returns.items()}

    df = pd.DataFrame(aligned)
    return df.corr()


def check_correlation_before_buy(
    new_symbol: str,
    new_history: pd.DataFrame,
    held_histories: dict[str, pd.DataFrame],
    window: int = 20,
) -> CorrelationCheck:
    """신규 매수 전 기존 보유와 상관관계 체크.

    Args:
        new_symbol: 매수 대상 심볼
        new_history: 매수 대상 OHLCV DataFrame
        held_histories: {보유 심볼: OHLCV DataFrame}
        window: 상관관계 계산 기간

    Returns:
        CorrelationCheck with allowed, size_mult, detail
    """
    if not held_histories:
        return CorrelationCheck(
            allowed=True, max_corr=0.0, corr_with="",
            size_mult=1.0, detail="보유 종목 없음 — 상관관계 체크 스킵",
        )

    new_close = new_history["close"].astype(float)
    new_ret = new_close.pct_change().dropna().tail(window)
    if len(new_ret) < window - 2:
        return CorrelationCheck(
            allowed=True, max_corr=0.0, corr_with="",
            size_mult=1.0, detail="데이터 부족 — 상관관계 체크 스킵",
        )

    max_corr = 0.0
    max_corr_sym = ""

    for sym, hist in held_histories.items():
        if hist is None or len(hist) < window:
            continue
        held_close = hist["close"].astype(float)
        held_ret = held_close.pct_change().dropna().tail(window)

        min_len = min(len(new_ret), len(held_ret))
        if min_len < 10:
            continue

        corr = float(np.corrcoef(
            new_ret.values[-min_len:],
            held_ret.values[-min_len:],
        )[0, 1])

        if abs(corr) > abs(max_corr):
            max_corr = corr
            max_corr_sym = sym

    if max_corr >= CORR_THRESHOLD_BLOCK:
        return CorrelationCheck(
            allowed=False,
            max_corr=round(max_corr, 3),
            corr_with=max_corr_sym,
            size_mult=0.0,
            detail=f"{new_symbol}↔{max_corr_sym} 상관={max_corr:.2f} ≥ {CORR_THRESHOLD_BLOCK} — 매수 차단",
        )
    elif max_corr >= CORR_THRESHOLD_REDUCE:
        reduction = 1.0 - (max_corr - CORR_THRESHOLD_REDUCE) / (CORR_THRESHOLD_BLOCK - CORR_THRESHOLD_REDUCE)
        size_mult = max(0.3, round(reduction, 2))
        return CorrelationCheck(
            allowed=True,
            max_corr=round(max_corr, 3),
            corr_with=max_corr_sym,
            size_mult=size_mult,
            detail=f"{new_symbol}↔{max_corr_sym} 상관={max_corr:.2f} — 포지션 {size_mult:.0%}로 축소",
        )
    else:
        return CorrelationCheck(
            allowed=True,
            max_corr=round(max_corr, 3),
            corr_with=max_corr_sym,
            size_mult=1.0,
            detail=f"상관관계 양호 (최대 {max_corr:.2f} with {max_corr_sym})",
        )


def compute_portfolio_risk_metrics(
    held_histories: dict[str, pd.DataFrame],
    weights: dict[str, float] | None = None,
    window: int = 20,
) -> dict:
    """포트폴리오 리스크 지표 계산.

    Returns:
        {
            "portfolio_vol": 포트폴리오 연환산 변동성,
            "diversification_ratio": 분산 비율 (>1이면 분산 효과 있음),
            "avg_correlation": 평균 상관계수,
            "max_correlation": 최대 상관계수 쌍,
        }
    """
    returns_dict = {}
    for sym, hist in held_histories.items():
        if hist is None or len(hist) < window:
            continue
        close = hist["close"].astype(float)
        ret = close.pct_change().dropna().tail(window)
        if len(ret) >= window - 2:
            returns_dict[sym] = ret.values

    if len(returns_dict) < 2:
        return {"portfolio_vol": 0, "diversification_ratio": 1.0,
                "avg_correlation": 0, "max_correlation": (None, None, 0)}

    min_len = min(len(v) for v in returns_dict.values())
    symbols = list(returns_dict.keys())
    aligned = np.array([returns_dict[s][-min_len:] for s in symbols])

    # 가중치 (없으면 동일 가중)
    if weights:
        w = np.array([weights.get(s, 1.0 / len(symbols)) for s in symbols])
    else:
        w = np.ones(len(symbols)) / len(symbols)
    w = w / w.sum()

    cov = np.cov(aligned)
    individual_vols = np.sqrt(np.diag(cov))

    # 포트폴리오 변동성
    port_var = w @ cov @ w
    port_vol = float(np.sqrt(port_var) * np.sqrt(252))

    # 분산 비율: 개별 가중 변동성 합 / 포트폴리오 변동성
    weighted_vol_sum = float(np.sum(w * individual_vols) * np.sqrt(252))
    div_ratio = weighted_vol_sum / port_vol if port_vol > 0 else 1.0

    # 상관행렬
    corr = np.corrcoef(aligned)
    np.fill_diagonal(corr, 0)
    avg_corr = float(np.mean(np.abs(corr[np.triu_indices(len(symbols), k=1)])))

    max_idx = np.unravel_index(np.argmax(np.abs(corr)), corr.shape)
    max_pair = (symbols[max_idx[0]], symbols[max_idx[1]], float(corr[max_idx]))

    return {
        "portfolio_vol": round(port_vol, 4),
        "diversification_ratio": round(div_ratio, 3),
        "avg_correlation": round(avg_corr, 3),
        "max_correlation": max_pair,
    }
