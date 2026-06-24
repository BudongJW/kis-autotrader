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
import pandas_ta as pta

from src.kis_client import KISClient
from src.bot.runner import fetch_recent_history
from src.utils.logger import log

POSITIONS_PATH = Path("logs/positions.json")

# ── 리스크 파라미터 ──
STOP_LOSS_PCT = -0.03        # -3% 손절 (ATR 없을 때 폴백)
TRAILING_ACTIVATE_PCT = 0.02   # +2% 도달 시 추적 손절 활성화 (ATR 없을 때 폴백)
TRAILING_STOP_PCT = 0.012     # 고점 대비 -1.2% 하락 시 매도 (ATR 없을 때 폴백)

# ── ATR 기반 동적 손절 파라미터 ──
ATR_STOP_MULTIPLIER = 2.0     # 손절 = 매수가 - ATR × 2.0 (Turtle 기준 확대)
ATR_TRAILING_ACTIVATE = 2.5   # 추적 손절 활성화 = ATR × 2.5 수익 도달 시
ATR_TRAILING_DROP = 1.5       # 고점 대비 ATR × 1.5 하락 시 매도

# 동적 ROI 테이블: 추세추종 원칙 — 승자를 오래 보유
# 당일 ROI 청산은 비활성, 최소 1일 이상 보유 후 작동
ROI_TABLE = [
    (1440, 0.015),  # 1일(24h) 후: +1.5% 이상이면 청산 고려
    (720, 0.025),   # 12시간 후: +2.5%
    (360, 0.035),   # 6시간 후: +3.5%
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


def record_buy(symbol: str, price: int, qty: int, atr: float = 0.0,
               asset_type: str = "long") -> None:
    """매수 체결 시 포지션 기록.

    Args:
        atr: 매수 시점의 ATR(14) 값. 0이면 고정 비율 손절로 폴백.
        asset_type: "long" / "inverse_1x" / "inverse_2x" / "defensive"
    """
    # 자산 유형별 최대 보유일 (추세추종 원칙 — 승자를 오래 보유)
    max_hold = {"long": 15, "inverse_1x": 20, "inverse_2x": 10,
                "defensive": 60, "commodity": 20, "income": 30}

    positions = load_positions()
    positions[symbol] = {
        "buy_price": price,
        "buy_time": datetime.now().isoformat(),
        "buy_date": datetime.now().strftime("%Y-%m-%d"),
        "qty": qty,
        "peak_price": price,  # 추적 손절용 최고가
        "hold_days": 0,       # 보유 일수 (장 시작 시 증가)
        "max_hold_days": max_hold.get(asset_type, 5),
        "atr_at_buy": round(atr, 2),  # ATR 기반 동적 손절용
        "asset_type": asset_type,     # 자산 유형 (하락장 전략 구분)
        "pyramid_count": 0,           # 피라미딩 횟수
        "initial_risk": round(atr * ATR_STOP_MULTIPLIER, 2) if atr > 0 else round(price * abs(STOP_LOSS_PCT), 2),
    }
    save_positions(positions)


def adopt_carried_positions(broker_holdings: dict, universe_symbols: set,
                            traded_symbols: set) -> int:
    """이전 run에서 산 캐리 포지션을 positions.json에 흡수해 손절 관리 대상화.

    positions.json이 매 run 리셋되어(아티팩트 미복원) 봇이 자기 보유분을 잊고
    손절을 안 거는 문제 해결(6-05 091160 -6.47% 방치 사례). 흡수 대상은
    broker 보유분 중 (봇 유니버스 ∩ 봇 거래이력)이면서 positions에 없는 것.
    유니버스 밖이거나 봇 거래이력 없는 종목 = 진짜 수동 보유분 → 흡수 안 함(봇이 안 건드림).

    Args:
        broker_holdings: {symbol: {"qty": int, "buy_price": int, "current_price": int}}
        universe_symbols: 봇이 매매하는 종목 집합
        traded_symbols: 봇이 과거에 거래한 종목 집합 (canonical trades.csv 기준)
    Returns: 흡수한 포지션 수
    """
    positions = load_positions()
    adopted = 0
    for sym, info in (broker_holdings or {}).items():
        if sym in positions:
            continue
        # 봇이 거래한 종목이면 현재 유니버스에서 빠졌어도 청산까지 관리(6-08 091160 사례).
        # 봇 거래이력 없는 종목 = 진짜 수동 보유분 → 보호(흡수 안 함). universe는 참고용.
        if sym not in traded_symbols:
            continue
        try:
            buy_p = int(float(info.get("buy_price", 0) or 0))
            qty = int(float(info.get("qty", 0) or 0))
        except (TypeError, ValueError):
            continue
        if buy_p <= 0 or qty <= 0:
            continue
        cur = int(float(info.get("current_price", buy_p) or buy_p))
        positions[sym] = {
            "buy_price": buy_p,
            "buy_time": datetime.now().isoformat(),
            "buy_date": datetime.now().strftime("%Y-%m-%d"),
            "qty": qty,
            "peak_price": max(buy_p, cur),
            "hold_days": 1,
            "max_hold_days": 15,
            "atr_at_buy": 0.0,  # ATR 모름 → 고정 비율 손절로 폴백
            "asset_type": "long",
            "pyramid_count": 0,
            "initial_risk": round(buy_p * abs(STOP_LOSS_PCT), 2),
            "adopted": True,
        }
        adopted += 1
    if adopted:
        save_positions(positions)
    return adopted


def record_pyramid(symbol: str, add_price: int, add_qty: int, atr: float = 0.0) -> None:
    """피라미딩 매수 시 기존 포지션에 평단가·수량 갱신.

    Turtle Trading 방식: 평균 매수가 갱신, pyramid_count 증가.
    """
    positions = load_positions()
    pos = positions.get(symbol)
    if not pos:
        record_buy(symbol, add_price, add_qty, atr=atr)
        return

    old_price = pos["buy_price"]
    old_qty = pos["qty"]
    new_qty = old_qty + add_qty
    avg_price = int((old_price * old_qty + add_price * add_qty) / new_qty)

    pos["buy_price"] = avg_price
    pos["qty"] = new_qty
    pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
    pos["peak_price"] = max(pos.get("peak_price", avg_price), add_price)
    if atr > 0:
        pos["atr_at_buy"] = round(atr, 2)
        pos["initial_risk"] = round(atr * ATR_STOP_MULTIPLIER, 2)
    positions[symbol] = pos
    save_positions(positions)


def compute_atr_for_position(history: pd.DataFrame, length: int = 14) -> float:
    """매수 시점에서 ATR(14) 값을 계산.

    Returns:
        ATR 값 (원 단위). 계산 불가 시 0.0.
    """
    if history is None or len(history) < length + 1:
        return 0.0
    try:
        high = history["high"].astype(float)
        low = history["low"].astype(float)
        close = history["close"].astype(float)
        atr_series = pta.atr(high, low, close, length=length)
        if atr_series is not None and not atr_series.empty:
            val = float(atr_series.iloc[-1])
            return val if not pd.isna(val) else 0.0
    except Exception:
        pass
    return 0.0


def remove_position(symbol: str) -> None:
    """매도 완료 시 포지션 제거."""
    positions = load_positions()
    positions.pop(symbol, None)
    save_positions(positions)


# 분할 매도 기준: 1차 익절 시 50% 매도, 나머지는 추적 손절
PARTIAL_SELL_RATIO = 0.5
PARTIAL_PROFIT_TRIGGER = 0.03  # +3% 수익 시 1차 분할 매도


def check_stop_loss(symbol: str, current_price: int) -> tuple[bool, str]:
    """손절매 + 추적 손절 + 분할 매도 + 동적 ROI 확인.

    ATR 정보가 있으면 ATR 기반 동적 손절/추적 손절을 사용하고,
    없으면 기존 고정 비율로 폴백한다.

    Returns:
        (should_sell, reason)
        reason에 "[분할]" 접두어가 있으면 PARTIAL_SELL_RATIO만큼만 매도.
    """
    positions = load_positions()
    pos = positions.get(symbol)
    if not pos:
        return False, ""

    buy_price = pos["buy_price"]
    if buy_price <= 0:
        return False, "매수가 정보 없음"
    peak_price = pos.get("peak_price", buy_price)
    buy_time = datetime.fromisoformat(pos["buy_time"])
    now = datetime.now()
    hold_minutes = (now - buy_time).total_seconds() / 60

    pnl_pct = (current_price - buy_price) / buy_price
    atr = pos.get("atr_at_buy", 0.0)

    # 최고가 갱신
    if current_price > peak_price:
        peak_price = current_price
        positions[symbol]["peak_price"] = peak_price
        save_positions(positions)

    # ── 1. 손절매: ATR 기반 or 고정 비율 (전량 매도) ──
    if atr > 0 and buy_price > 0:
        stop_distance = atr * ATR_STOP_MULTIPLIER
        stop_price = buy_price - stop_distance
        if current_price <= stop_price:
            stop_pct = stop_distance / buy_price
            return True, (f"ATR 손절 ({current_price:,}원 ≤ {stop_price:,.0f}원, "
                          f"ATR={atr:.0f}×{ATR_STOP_MULTIPLIER}, -{stop_pct:.1%})")
    else:
        if pnl_pct <= STOP_LOSS_PCT:
            return True, f"손절매 ({pnl_pct:+.1%} ≤ {STOP_LOSS_PCT:.0%})"

    # ── 2. 분할 매도: +3% 도달 시 50% 1차 익절 (나머지는 추적 손절로) ──
    if not pos.get("partial_sold") and pnl_pct >= PARTIAL_PROFIT_TRIGGER:
        positions[symbol]["partial_sold"] = True
        save_positions(positions)
        return True, (f"[분할] 1차 익절 ({pnl_pct:+.1%} ≥ {PARTIAL_PROFIT_TRIGGER:.0%}, "
                      f"보유분의 {PARTIAL_SELL_RATIO:.0%} 매도)")

    # ── 3. 추적 손절: ATR 기반 or 고정 비율 (잔여분 전량) ──
    if atr > 0 and buy_price > 0:
        trailing_activate_price = buy_price + atr * ATR_TRAILING_ACTIVATE
        if peak_price >= trailing_activate_price:
            trailing_stop_price = peak_price - atr * ATR_TRAILING_DROP
            if current_price <= trailing_stop_price:
                return True, (f"ATR 추적 손절 (고점 {peak_price:,}원, "
                              f"ATR 기준 {trailing_stop_price:,.0f}원 이탈, "
                              f"수익 {pnl_pct:+.1%})")
    else:
        peak_pnl = (peak_price - buy_price) / buy_price
        if peak_pnl >= TRAILING_ACTIVATE_PCT:
            drop_from_peak = (current_price - peak_price) / peak_price
            if drop_from_peak <= -TRAILING_STOP_PCT:
                return True, (f"추적 손절 (고점 {peak_price:,}원에서 "
                              f"{drop_from_peak:+.1%} 하락, 수익 {pnl_pct:+.1%})")

    # ── 4. 동적 ROI: 보유 시간별 최소 수익률 ──
    if pnl_pct > 0:
        for minutes, min_roi in ROI_TABLE:
            if hold_minutes >= minutes and pnl_pct >= min_roi:
                return True, (f"ROI 청산 ({hold_minutes:.0f}분 보유, "
                              f"수익 {pnl_pct:+.1%} ≥ {min_roi:.1%})")

    # ── 5. 인버스 ETF 보유기간 강제 청산 ──
    asset_type = pos.get("asset_type", "long")
    hold_days = pos.get("hold_days", 0)
    max_hold = pos.get("max_hold_days", 15)
    if asset_type.startswith("inverse") and hold_days >= max_hold:
        return True, (f"인버스 보유기간 만료 ({hold_days}/{max_hold}일, "
                      f"{asset_type}, 수익 {pnl_pct:+.1%})")

    # 일반 ETF도 max_hold 초과 시 청산 (추세 종료 간주)
    if not asset_type.startswith("inverse") and hold_days >= max_hold and pnl_pct < 0.03:
        return True, (f"최대 보유기간 도달 ({hold_days}/{max_hold}일, "
                      f"수익 {pnl_pct:+.1%} < +3% — 추세 약화)")

    return False, ""


def _load_hold_rules() -> dict:
    """strategy.yaml에서 적응적 보유 규칙 로드."""
    try:
        import yaml
        config_path = Path("configs/strategy.yaml")
        with config_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("hold_adaptive_rules", {})
    except Exception:
        return {}


def should_hold_overnight(symbol: str, current_price: int) -> tuple[bool, str]:
    """시가 매도 대신 계속 보유할지 판단.

    적응적 규칙 (학습 데이터 충분 시):
      - min_profit_to_hold: 보유 유지 최소 수익률 (학습으로 조정)
      - max_hold_days: 최대 보유 일수 (학습으로 조정)

    기본 규칙:
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
    if buy_price <= 0:
        return False, "매수가 정보 없음"
    peak_price = pos.get("peak_price", buy_price)
    hold_days = pos.get("hold_days", 0)

    # 적응적 규칙 로드
    rules = _load_hold_rules()
    min_profit = rules.get("min_profit_to_hold", 0.001)
    max_hold = rules.get("max_hold_days", pos.get("max_hold_days", 15))

    pnl_pct = (current_price - buy_price) / buy_price
    drop_from_peak = (current_price - peak_price) / peak_price if peak_price > 0 else 0

    # 최대 보유 일수 초과 → 매도
    if hold_days >= max_hold:
        return False, f"최대 보유 일수 도달 ({hold_days}/{max_hold}일)"

    # 큰 손실 중이면 매도 (-1.5% 이하, 손절은 risk_manager가 별도 처리)
    if pnl_pct < -0.015:
        return False, f"손실 확대 ({pnl_pct:+.1%}), 매도"

    # 수익 중이고 고점 대비 적정 범위 → 보유 계속 (추세추종 원칙)
    if pnl_pct >= min_profit and drop_from_peak > -0.025:
        pos["hold_days"] = hold_days + 1
        positions[symbol] = pos
        save_positions(positions)
        return True, (f"수익 {pnl_pct:+.1%}, 고점 대비 {drop_from_peak:+.1%} "
                      f"→ 보유 계속 ({hold_days + 1}/{max_hold}일)")

    # 보합(-0.5%~+0.1%)이면 3일차까지 관찰 (추세 형성 대기)
    if hold_days < 3 and pnl_pct >= -0.005:
        pos["hold_days"] = hold_days + 1
        positions[symbol] = pos
        save_positions(positions)
        return True, f"보합 ({pnl_pct:+.1%}), 추세 대기 ({hold_days + 1}/{max_hold}일)"

    return False, f"보유 조건 미충족 (수익 {pnl_pct:+.1%})"


def check_daily_loss_limit(client: KISClient) -> tuple[bool, str]:
    """일일 손실 한도 초과 여부 확인. True면 매수 차단.

    strategy.yaml의 risk.daily_loss_limit_pct 값 사용 (기본 5%).
    당일 실현 손실이 한도 초과 시 신규 매수 차단.
    """
    import csv
    import yaml
    from src.tracker import TRADE_LOG_PATH

    try:
        config_path = Path("configs/strategy.yaml")
        with config_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        limit_pct = cfg.get("risk", {}).get("daily_loss_limit_pct", 0.05)
    except Exception:
        limit_pct = 0.05

    if not TRADE_LOG_PATH.exists():
        return False, "거래 기록 없음"

    today_str = datetime.now().strftime("%Y-%m-%d")
    buys: dict[str, list[int]] = {}
    daily_pnl = 0

    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("timestamp", "").startswith(today_str):
                continue
            symbol = row.get("symbol", "")
            price = int(row.get("price", 0))
            qty = int(row.get("qty", 0))
            side = row.get("side", "")
            if side == "buy":
                buys.setdefault(symbol, []).append(price * qty)
            elif side == "sell" and symbol in buys and buys[symbol]:
                buy_cost = buys[symbol].pop(0)
                sell_amount = price * qty
                daily_pnl += sell_amount - buy_cost

    if daily_pnl >= 0:
        return False, f"당일 손익 {daily_pnl:+,}원 (이익 중)"

    # 보유 포지션의 미실현 손실도 반영
    positions = load_positions()
    unrealized_loss = 0
    for symbol, pos in positions.items():
        buy_price = pos.get("buy_price", 0)
        qty = pos.get("qty", 0)
        if buy_price > 0 and qty > 0:
            try:
                resp = client.get_price(symbol)
                if resp.get("rt_cd") == "0":
                    cur_price = int(resp["output"]["stck_prpr"])
                    unrealized_loss += (cur_price - buy_price) * qty
            except Exception:
                pass

    total_loss = daily_pnl + min(0, unrealized_loss)

    # 총 자산 대비 비율 계산
    try:
        resp = client.get_balance()
        if resp.get("rt_cd") == "0":
            cash_list = resp.get("output2", [{}])
            total_asset = int(cash_list[0].get("tot_evlu_amt", 0)) if cash_list else 0
            if total_asset <= 0:
                total_asset = int(cash_list[0].get("dnca_tot_amt", 1_000_000)) if cash_list else 1_000_000
        else:
            total_asset = 1_000_000
    except Exception:
        total_asset = 1_000_000

    loss_pct = abs(total_loss) / total_asset if total_asset > 0 else 0

    if loss_pct >= limit_pct:
        return True, (f"일일 손실 한도 초과 ({loss_pct:.1%} >= {limit_pct:.0%}, "
                       f"실현 {daily_pnl:+,}원, 미실현 {unrealized_loss:+,}원)")

    return False, f"당일 손익 {total_loss:+,}원 ({loss_pct:.1%} / 한도 {limit_pct:.0%})"


def check_max_positions(max_positions: int = 5) -> tuple[bool, str]:
    """최대 동시 포지션 수 초과 여부 확인. True면 매수 가능.

    Returns:
        (can_buy, reason)
    """
    positions = load_positions()
    count = len(positions)
    if count >= max_positions:
        return False, f"최대 포지션 도달 ({count}/{max_positions})"
    return True, f"포지션 여유 ({count}/{max_positions})"


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


def high_vol_size_factor(base_factor: float, is_high_vol: bool,
                         cfg: dict | None = None) -> tuple[float, bool]:
    """변동성 급등(터뷸런스/VIX 패닉) 시 사이즈 결정.

    기존엔 변동성이 튀면 신규매수를 '전면 차단'했으나, 그게 곧 큰 기회(V회복·강돌파)가
    나는 구간이라 봇이 기회마다 빠지는 문제가 있었다. 이제 차단 대신 **사이즈 축소**로
    여전히 진입하게 한다(손실은 사이즈·손절·수수료게이트로 제한). config로 동작 선택:
      risk.high_vol_action: "size_down"(기본, 축소 진입) | "block"(구 동작, 전면차단)
      risk.high_vol_size_mult: 축소 배수(기본 0.5)

    Returns:
        (adjusted_factor, blocked) — blocked=True면 호출부가 매수 스킵(구 동작).
    """
    c = cfg or {}
    if not is_high_vol:
        return base_factor, False
    if c.get("high_vol_action", "size_down") == "block":
        return 0.0, True
    mult = float(c.get("high_vol_size_mult", 0.5))
    return base_factor * mult, False


def conviction_position_cap_krw(equity: float, fusion_prob: float,
                                breakout: bool, cfg: dict | None = None) -> int:
    """확신도 연동 단일종목 비중 상한(원). 약신호에 큰 비중을 금지한다.

    069500을 약신호(돌파X·융합64%)에 16% 비중으로 잡았다가 -3.1% 손절로 누적
    최저점을 찍은 사례 보완. 강신호(돌파+고융합)만 큰 비중까지 허용하고, 중·약
    신호는 상한을 낮춰 — 상한이 min_position_krw보다 작으면 호출부가 진입을
    스킵하게 한다(약신호는 아예 안 삼).

    Args:
        equity: 총자본(현금+보유평가), fusion_prob: 융합 확률, breakout: 돌파 여부
        cfg: risk dict (max_position_weight / conviction_*_weight)
    Returns: 단일종목 최대 투입액(원).
    """
    r = cfg or {}
    base = float(r.get("max_position_weight", 0.35) or 0.35)
    fp = float(fusion_prob or 0.0)
    if breakout and fp >= 0.70:
        pct = float(r.get("conviction_strong_weight", base))
    elif fp >= 0.70 or (breakout and fp >= 0.62):
        pct = float(r.get("conviction_mid_weight", 0.22))
    else:
        pct = float(r.get("conviction_weak_weight", 0.12))
    pct = min(pct, base)  # 하드 상한 초과 금지
    return int(max(0.0, equity) * pct)


def apply_min_position(qty: int, price: float, avail_cash: float,
                       min_krw: float, max_weight: float = 0.0,
                       equity: float = 0.0) -> int:
    """진입 수량에 최소 포지션 금액 floor 적용 (소액 1주 회피).

    단타는 포지션이 충분히 커야 %수익이 왕복수수료를 넘어 실이득이 남는다(작은
    1주 거래는 수수료에 먹혀 적자). 진입이 게이트를 통과한 뒤, 투입액이 min_krw
    미만이면 min_krw 수준으로 키운다. 단 가용현금(95%) + 비중상한으로 캡.

    Args:
        qty: 신호 기반 산정 수량, price: 현재가, avail_cash: 주문가능현금,
        min_krw: 최소 투입 금액(0이면 비활성),
        max_weight/equity: 단일종목 비중상한·총자본(둘 다>0이면 캡 적용).
    Returns: 조정 수량(>= 기존 qty, 절대 줄이지 않음).
    """
    if min_krw <= 0 or price <= 0 or avail_cash <= 0:
        return qty
    desired = max(qty * price, min_krw)
    cap = avail_cash * 0.95
    if max_weight > 0 and equity > 0:
        cap = min(cap, equity * max_weight)
    bumped = int(min(desired, cap) // price)
    return max(qty, bumped)


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

    # 상한 클램프: 최대 35% (소액 자본에서 1주도 못 사는 문제 완화)
    return min(f_star, 0.35)


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
        return 0.20  # 기본값: 20% (거래 이력 없을 때 소액 자본 매수 가능성 확보)

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
        return 0.20  # 샘플 부족 → 20% (이전 10%는 소액 자본에서 매수 불가)

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


def get_drawdown_scale() -> tuple[float, str]:
    """최근 거래의 연속 손실/수익에 따라 포지션 스케일링 계수 반환.

    - 최근 3연속 손실: 50% 축소
    - 최근 2연속 손실: 70% 축소
    - 최근 3연속 수익: 120% 확대 (상한 존재)
    - 그 외: 100% (기본)

    Returns:
        (scale_factor, reason)
    """
    import csv
    from src.tracker import TRADE_LOG_PATH

    if not TRADE_LOG_PATH.exists():
        return 1.0, "거래 기록 없음"

    pnl_list: list[float] = []
    buys: dict[str, list[int]] = {}

    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get("symbol", "")
            side = row.get("side", "")
            price = int(row.get("price", 0))

            if side == "buy":
                buys.setdefault(symbol, []).append(price)
            elif side == "sell" and symbol in buys and buys[symbol]:
                buy_price = buys[symbol].pop(0)
                if buy_price > 0:
                    pnl_list.append((price - buy_price) / buy_price)

    if len(pnl_list) < 3:
        return 1.0, f"거래 {len(pnl_list)}건, 스케일링 미적용"

    # 최근 N건의 연속 결과 확인
    recent = pnl_list[-5:]  # 최근 5건
    consecutive_loss = 0
    consecutive_win = 0

    for pnl in reversed(recent):
        if pnl <= 0:
            if consecutive_win > 0:
                break
            consecutive_loss += 1
        else:
            if consecutive_loss > 0:
                break
            consecutive_win += 1

    if consecutive_loss >= 3:
        return 0.5, f"3연속 손실 → 50% 축소"
    elif consecutive_loss >= 2:
        return 0.7, f"2연속 손실 → 70% 축소"
    elif consecutive_win >= 3:
        return 1.2, f"3연속 수익 → 120% 확대"

    return 1.0, "정상 스케일링"
