"""리스크 관리 모듈 — 손절매, 추적 손절, 동적 ROI, 터뷸런스 필터.

매 5분 실행 시 보유 종목에 대해:
  1. 손절매: 매수가 대비 -3% 도달 시 즉시 매도
  2. 추적 손절: 수익 1.5%↑ 도달 후 고점 대비 -1% 하락 시 매도
  3. 동적 ROI: 보유 시간별 최소 수익률 미달 시 매도
  4. 터뷸런스 필터: KOSPI200 변동성 급등 시 신규 매수 차단

매수 정보 저장:
  logs/positions.json에 매수가·시각을 기록하여 실행 간 상태 유지.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.kis_client import KISClient
from src.bot.runner import fetch_recent_history
from src.utils.logger import log

POSITIONS_PATH = Path("logs/positions.json")

# ── 리스크 파라미터 ──
STOP_LOSS_PCT = -0.03        # -3% 손절
TRAILING_ACTIVATE_PCT = 0.015  # +1.5% 도달 시 추적 손절 활성화
TRAILING_STOP_PCT = 0.01      # 고점 대비 -1% 하락 시 매도

# 동적 ROI 테이블: (보유 시간(분), 최소 수익률)
# 보유 시간이 길어질수록 낮은 수익률에서도 청산
ROI_TABLE = [
    (240, 0.005),   # 4시간 후: +0.5% 이상이면 청산
    (180, 0.008),   # 3시간 후: +0.8%
    (120, 0.012),   # 2시간 후: +1.2%
    (60, 0.02),     # 1시간 후: +2.0%
]

# 터뷸런스: KOSPI200 변동성이 60일 평균의 N배 초과 시 매수 차단
TURBULENCE_MULTIPLIER = 1.5
KOSPI200_SYMBOL = "069500"  # KODEX 200


def load_positions() -> dict:
    """매수 포지션 정보 로드."""
    if POSITIONS_PATH.exists():
        with POSITIONS_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_positions(positions: dict) -> None:
    """매수 포지션 정보 저장."""
    POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with POSITIONS_PATH.open("w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def record_buy(symbol: str, price: int, qty: int) -> None:
    """매수 체결 시 포지션 기록."""
    positions = load_positions()
    positions[symbol] = {
        "buy_price": price,
        "buy_time": datetime.now().isoformat(),
        "buy_date": datetime.now().strftime("%Y-%m-%d"),
        "qty": qty,
        "peak_price": price,  # 추적 손절용 최고가
        "hold_days": 0,       # 보유 일수 (장 시작 시 증가)
        "max_hold_days": 5,   # 최대 보유 일수
    }
    save_positions(positions)


def remove_position(symbol: str) -> None:
    """매도 완료 시 포지션 제거."""
    positions = load_positions()
    positions.pop(symbol, None)
    save_positions(positions)


def check_stop_loss(symbol: str, current_price: int) -> tuple[bool, str]:
    """손절매 + 추적 손절 + 동적 ROI 확인.

    Returns:
        (should_sell, reason)
    """
    positions = load_positions()
    pos = positions.get(symbol)
    if not pos:
        return False, ""

    buy_price = pos["buy_price"]
    peak_price = pos.get("peak_price", buy_price)
    buy_time = datetime.fromisoformat(pos["buy_time"])
    now = datetime.now()
    hold_minutes = (now - buy_time).total_seconds() / 60

    pnl_pct = (current_price - buy_price) / buy_price

    # 최고가 갱신
    if current_price > peak_price:
        peak_price = current_price
        positions[symbol]["peak_price"] = peak_price
        save_positions(positions)

    # 1. 손절매: -3%
    if pnl_pct <= STOP_LOSS_PCT:
        return True, f"손절매 ({pnl_pct:+.1%} ≤ {STOP_LOSS_PCT:.0%})"

    # 2. 추적 손절: 고점 대비 하락
    peak_pnl = (peak_price - buy_price) / buy_price
    if peak_pnl >= TRAILING_ACTIVATE_PCT:
        drop_from_peak = (current_price - peak_price) / peak_price
        if drop_from_peak <= -TRAILING_STOP_PCT:
            return True, (f"추적 손절 (고점 {peak_price:,}원에서 "
                          f"{drop_from_peak:+.1%} 하락, 수익 {pnl_pct:+.1%})")

    # 3. 동적 ROI: 보유 시간별 최소 수익률
    if pnl_pct > 0:
        for minutes, min_roi in ROI_TABLE:
            if hold_minutes >= minutes and pnl_pct >= min_roi:
                return True, (f"ROI 청산 ({hold_minutes:.0f}분 보유, "
                              f"수익 {pnl_pct:+.1%} ≥ {min_roi:.1%})")

    return False, ""


def should_hold_overnight(symbol: str, current_price: int) -> tuple[bool, str]:
    """시가 매도 대신 계속 보유할지 판단.

    조건:
      - 현재 수익 중 (+0.3% 이상)
      - 최대 보유 일수(5일) 미만
      - 고점 대비 -2% 이내

    Returns:
        (should_hold, reason)
    """
    positions = load_positions()
    pos = positions.get(symbol)
    if not pos:
        return False, "포지션 정보 없음"

    buy_price = pos["buy_price"]
    peak_price = pos.get("peak_price", buy_price)
    hold_days = pos.get("hold_days", 0)
    max_hold = pos.get("max_hold_days", 5)

    pnl_pct = (current_price - buy_price) / buy_price
    drop_from_peak = (current_price - peak_price) / peak_price if peak_price > 0 else 0

    # 최대 보유 일수 초과 → 매도
    if hold_days >= max_hold:
        return False, f"최대 보유 일수 도달 ({hold_days}/{max_hold}일)"

    # 손실 중이면 매도 (-0.5% 이하)
    if pnl_pct < -0.005:
        return False, f"손실 중 ({pnl_pct:+.1%}), 매도"

    # 수익 중이고 고점 대비 적정 범위 → 보유 계속
    if pnl_pct >= 0.003 and drop_from_peak > -0.02:
        # 보유 일수 갱신
        pos["hold_days"] = hold_days + 1
        positions[symbol] = pos
        save_positions(positions)
        return True, (f"수익 {pnl_pct:+.1%}, 고점 대비 {drop_from_peak:+.1%} "
                      f"→ 보유 계속 ({hold_days + 1}/{max_hold}일)")

    # 보합이면 1일차까지는 보유, 이후 매도
    if hold_days < 1 and pnl_pct >= -0.003:
        pos["hold_days"] = hold_days + 1
        positions[symbol] = pos
        save_positions(positions)
        return True, f"보합 ({pnl_pct:+.1%}), 1일 더 관찰 ({hold_days + 1}/{max_hold}일)"

    return False, f"보유 조건 미충족 (수익 {pnl_pct:+.1%})"


def check_turbulence(client: KISClient) -> tuple[bool, str]:
    """시장 터뷸런스 확인. True면 매수 차단.

    KOSPI200 ETF의 최근 변동성이 장기 평균 대비 급등했는지 판별.
    """
    try:
        history = fetch_recent_history(client, KOSPI200_SYMBOL, days=70)
        if len(history) < 65:
            return False, "데이터 부족, 필터 미적용"

        close = history["close"].astype(float)
        returns = close.pct_change().dropna()

        # 최근 5일 변동성 vs 60일 평균 변동성
        recent_vol = float(returns.tail(5).std())
        long_vol = float(returns.tail(60).std())

        if long_vol == 0:
            return False, "변동성 계산 불가"

        ratio = recent_vol / long_vol

        if ratio > TURBULENCE_MULTIPLIER:
            return True, (f"터뷸런스 감지 (변동성 {ratio:.1f}x, "
                          f"최근={recent_vol:.4f}, 평균={long_vol:.4f})")
        else:
            return False, f"정상 (변동성 {ratio:.1f}x)"

    except Exception as e:
        log.error("turbulence_check_failed", error=str(e))
        return False, f"확인 실패: {e}"


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float,
                   half: bool = True) -> float:
    """Kelly Criterion 최적 투입 비율.

    f* = p - (1-p)/b  where p=win_rate, b=avg_win/avg_loss
    half=True면 Half-Kelly (f*/2) 사용 — 현실적 안전 마진.

    Returns:
        0.0 ~ 1.0 사이 최적 투입 비율. 음수이면 0 반환 (배팅 부적합).
    """
    if avg_loss <= 0 or win_rate <= 0:
        return 0.0

    b = avg_win / avg_loss  # payoff ratio
    f_star = win_rate - (1 - win_rate) / b

    if f_star <= 0:
        return 0.0

    if half:
        f_star *= 0.5  # Half-Kelly

    # 상한 클램프: 최대 25% (과도한 집중 방지)
    return min(f_star, 0.25)


def get_kelly_position_size(strategy: str = "combined") -> float:
    """최근 거래 이력으로부터 Kelly 기반 포지션 비율 계산.

    Args:
        strategy: "etf", "surge", "combined"

    Returns:
        0.0 ~ 0.25 사이 투입 비율
    """
    import csv
    from src.tracker import TRADE_LOG_PATH

    if not TRADE_LOG_PATH.exists():
        return 0.10  # 기본값: 10%

    buys: dict[str, list] = {}
    pnl_list: list[float] = []

    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get("symbol", "")
            side = row.get("side", "")
            price = int(row.get("price", 0))
            name = row.get("name", "")

            if side == "buy":
                buys.setdefault(symbol, []).append({"price": price, "name": name})
            elif side == "sell" and symbol in buys and buys[symbol]:
                buy_info = buys[symbol].pop(0)
                pnl_pct = (price - buy_info["price"]) / buy_info["price"]
                is_etf = any(kw in buy_info["name"]
                             for kw in ["KODEX", "TIGER", "KOSEF", "ACE"])
                if strategy == "combined":
                    pnl_list.append(pnl_pct)
                elif strategy == "etf" and is_etf:
                    pnl_list.append(pnl_pct)
                elif strategy == "surge" and not is_etf:
                    pnl_list.append(pnl_pct)

    if len(pnl_list) < 5:
        return 0.10  # 샘플 부족 → 보수적

    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]
    win_rate = len(wins) / len(pnl_list)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.01

    fraction = kelly_fraction(win_rate, avg_win, avg_loss, half=True)
    log.info("kelly_sizing",
             strategy=strategy,
             win_rate=f"{win_rate:.1%}",
             avg_win=f"{avg_win:+.2%}",
             avg_loss=f"{avg_loss:.2%}",
             kelly_f=f"{fraction:.1%}")
    return fraction


def get_strategy_expectancy() -> dict[str, float]:
    """전략별 기대값 계산 (최근 거래 기반).

    기대값 = 승률 × 평균수익률 - 패률 × 평균손실률
    반환: {"etf": ratio, "surge": ratio}
    """
    import csv
    from src.tracker import TRADE_LOG_PATH

    results = {"etf": 0.6, "surge": 0.4}  # 기본값
    if not TRADE_LOG_PATH.exists():
        return results

    trades_by_strategy: dict[str, list[float]] = {"etf": [], "surge": []}

    # 매수·매도 쌍 매칭 (단순 FIFO)
    buys: dict[str, list] = {}
    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get("symbol", "")
            side = row.get("side", "")
            price = int(row.get("price", 0))
            name = row.get("name", "")

            if side == "buy":
                buys.setdefault(symbol, []).append({"price": price, "name": name})
            elif side == "sell" and symbol in buys and buys[symbol]:
                buy_info = buys[symbol].pop(0)
                pnl_pct = (price - buy_info["price"]) / buy_info["price"]
                # ETF인지 급등주인지 판별 (name에 KODEX/TIGER 등이 있으면 ETF)
                is_etf = any(kw in buy_info["name"] for kw in ["KODEX", "TIGER", "KOSEF", "ACE"])
                key = "etf" if is_etf else "surge"
                trades_by_strategy[key].append(pnl_pct)

    for key, trades in trades_by_strategy.items():
        if len(trades) < 3:
            continue
        wins = [t for t in trades if t > 0]
        losses = [t for t in trades if t <= 0]
        win_rate = len(wins) / len(trades) if trades else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0

        if avg_loss > 0:
            expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
        else:
            expectancy = win_rate * avg_win

        # 기대값을 배분 비율로 변환 (양수면 자본 배분, 음수면 0)
        results[key] = max(0.0, expectancy + 0.5)  # baseline 0.5 + expectancy

    # 정규화
    total = sum(results.values())
    if total > 0:
        for key in results:
            results[key] = round(results[key] / total, 2)

    return results
