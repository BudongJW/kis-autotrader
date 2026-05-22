"""호가 임밸런스 신호 — 매수/매도 잔량 비율로 단기 방향성 추정.

KIS REST 호가 endpoint (inquire-asking-price-exp-ccn)로 일회성 조회.
WebSocket 없이도 매 전략 체크마다 ms 단위로 받아올 수 있어 매수 직전
timing filter로 효과적.

Imbalance score:
  raw = (bid_total - ask_total) / (bid_total + ask_total)
  range: -1.0 (강한 매도세) ~ +1.0 (강한 매수세)

호가 단계별 가중치:
  1단계 가까운 호가일수록 영향 큼 → weight = 1.0 / step
  weighted_imbalance = sum(weight * (bid_qty - ask_qty)) / sum(weight * (bid_qty + ask_qty))

사용 (single_run.py 매수 직전):
  from src.strategies.orderbook_imbalance import get_imbalance, should_skip_buy
  imb = get_imbalance(client, symbol)
  if should_skip_buy(imb):
      print(f"  [호가 임밸런스] {imb.weighted:+.2f} 약세 → 매수 SKIP")
      continue
"""

from __future__ import annotations

from dataclasses import dataclass

from src.kis_client import KISClient
from src.utils.logger import log


# 매수 차단 임계값 (이 미만이면 매수 보류)
SKIP_BUY_THRESHOLD = -0.30
# 매도 가속 임계값 (이 미만이면 손절 더 빨리)
ACCELERATE_SELL_THRESHOLD = -0.50
# 강한 매수 시그널 임계값
STRONG_BUY_THRESHOLD = 0.30


@dataclass
class Imbalance:
    raw: float                  # 총량 기반 단순 비율 (-1 ~ +1)
    weighted: float             # 호가단계 가중 (1단계 우선)
    bid_total: int              # 매수 총 잔량
    ask_total: int              # 매도 총 잔량
    spread_bp: float            # 1호가 스프레드 (basis points)
    ok: bool                    # 데이터 정상 수신 여부
    reason: str                 # 설명


def get_imbalance(client: KISClient, symbol: str) -> Imbalance:
    """호가 조회 → imbalance score 계산."""
    try:
        resp = client.get_orderbook(symbol)
    except Exception as e:
        return Imbalance(0, 0, 0, 0, 0, False, f"호가 조회 실패: {e}")

    if resp.get("rt_cd") != "0":
        return Imbalance(0, 0, 0, 0, 0, False, f"호가 응답 오류: {resp.get('msg1', '')}")

    output = resp.get("output1") or resp.get("output") or {}
    if isinstance(output, list):
        output = output[0] if output else {}

    try:
        bid_total = int(output.get("total_bidp_rsqn", 0) or 0)
        ask_total = int(output.get("total_askp_rsqn", 0) or 0)
    except (TypeError, ValueError):
        return Imbalance(0, 0, 0, 0, 0, False, "잔량 파싱 실패")

    if bid_total + ask_total <= 0:
        return Imbalance(0, 0, bid_total, ask_total, 0, False, "잔량 0")

    raw = (bid_total - ask_total) / (bid_total + ask_total)

    # 호가단계 가중치: 1단계 = 1.0, 2단계 = 0.8, ..., 10단계 = 0.1
    weighted_bid_sum = 0.0
    weighted_ask_sum = 0.0
    for step in range(1, 11):
        weight = max(0.1, 1.0 - (step - 1) * 0.1)
        try:
            b = int(output.get(f"bidp_rsqn{step}", 0) or 0)
            a = int(output.get(f"askp_rsqn{step}", 0) or 0)
            weighted_bid_sum += b * weight
            weighted_ask_sum += a * weight
        except (TypeError, ValueError):
            continue

    weighted = 0.0
    if weighted_bid_sum + weighted_ask_sum > 0:
        weighted = (weighted_bid_sum - weighted_ask_sum) / (weighted_bid_sum + weighted_ask_sum)

    # 1호가 스프레드 (basis points)
    spread_bp = 0.0
    try:
        bid1 = int(output.get("bidp1", 0) or 0)
        ask1 = int(output.get("askp1", 0) or 0)
        if bid1 > 0 and ask1 > 0:
            spread_bp = (ask1 - bid1) / bid1 * 10000  # bp
    except (TypeError, ValueError):
        pass

    reason = (f"매수잔량={bid_total:,} 매도잔량={ask_total:,} "
              f"raw={raw:+.2f} 가중={weighted:+.2f} 스프레드={spread_bp:.0f}bp")

    return Imbalance(
        raw=round(raw, 3),
        weighted=round(weighted, 3),
        bid_total=bid_total,
        ask_total=ask_total,
        spread_bp=round(spread_bp, 1),
        ok=True,
        reason=reason,
    )


def should_skip_buy(imb: Imbalance) -> bool:
    """매수 신호가 있어도 호가가 약세면 SKIP 권고."""
    if not imb.ok:
        return False  # 데이터 없으면 차단 안 함 (안전 기본값)
    return imb.weighted < SKIP_BUY_THRESHOLD


def is_strong_buy(imb: Imbalance) -> bool:
    """매수 잔량이 매도 잔량보다 훨씬 우세."""
    if not imb.ok:
        return False
    return imb.weighted > STRONG_BUY_THRESHOLD


def should_accelerate_sell(imb: Imbalance) -> bool:
    """매도 잔량이 압도적 → 손절 가속 권고."""
    if not imb.ok:
        return False
    return imb.weighted < ACCELERATE_SELL_THRESHOLD
