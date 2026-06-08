"""미국 ETF 야간 매매 전략 — 한국 밤 시간대 미국장 운영.

시간대 (KST 기준):
  서머타임: 22:30~05:00  (3월 둘째 일요일 ~ 11월 첫째 일요일)
  동절기:   23:30~06:00

전략: 변동성 돌파 + TA 복합 점수 (국내 ETF와 동일 로직)
  - 미국 ETF(QQQ, SPY 등)에 변동성 돌파 적용
  - 한국장 레짐이 bear면 인버스 ETF(SH) 우선

리스크:
  - USD 기준 손절 -2.5%
  - 최대 동시 2종목
  - 장 마감 전 미청산 포지션 강제 매도
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from src.config import settings
from src.kis_client import KISClient
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.strategies.ta_composite import compute_ta_score
from src.risk_manager import load_positions, save_positions, record_buy, remove_position
from src.tracker import log_trade
from src.experience import log_decision
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
EST = ZoneInfo("America/New_York")

CONFIG_PATH = Path("configs/strategy.yaml")
US_STATE_PATH = Path("logs/us_session_state.json")
US_POSITIONS_PATH = Path("logs/us_positions.json")


# ──────────────────────────────────────────────────────────
# 설정 로드
# ──────────────────────────────────────────────────────────

def load_us_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("us_session", {})


def load_us_universe() -> list[dict]:
    return load_us_config().get("universe", [])


def is_us_market_hours() -> bool:
    """미국 정규장 시간인지 확인 (KST 기준)."""
    cfg = load_us_config()
    if not cfg.get("enabled", False):
        return False

    now = datetime.now(KST).time()
    summer = cfg.get("summer_time", False)

    if summer:
        open_t = dtime(22, 30)
        close_t = dtime(5, 0)
    else:
        open_t = dtime(23, 30)
        close_t = dtime(6, 0)

    # 자정 넘어가는 시간 처리
    if open_t > close_t:
        return now >= open_t or now <= close_t
    return open_t <= now <= close_t


def get_us_market_times() -> tuple[dtime, dtime]:
    """(open_kst, close_kst) 반환."""
    cfg = load_us_config()
    summer = cfg.get("summer_time", False)
    if summer:
        return dtime(22, 30), dtime(5, 0)
    return dtime(23, 30), dtime(6, 0)


# ──────────────────────────────────────────────────────────
# 해외 히스토리 조회
# ──────────────────────────────────────────────────────────

def fetch_us_history(client: KISClient, symbol: str, exchange: str = "NASD",
                     days: int = 70) -> pd.DataFrame:
    """해외주식 일별 시세를 DataFrame으로 변환.

    1차: KIS 해외 일봉 endpoint.
    2차(폴백): KIS가 빈 데이터/오류를 반환하면 yfinance로 OHLCV 조회.
      (yfinance는 requirements에 포함. 신호 생성용 일봉만 쓰고, 실제 주문가는
       KIS 현재가(get_us_price)를 그대로 사용하므로 가격 정합성 문제 없음.)
    """
    try:
        df = _fetch_us_history_kis(client, symbol, exchange, days)
        if df is not None and len(df) >= 5:
            return df
        log.warning("us_daily_kis_empty_fallback_yf", symbol=symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("us_daily_kis_failed_fallback_yf", symbol=symbol, error=str(e))

    df = _fetch_us_history_yf(symbol, days)
    if df is None or df.empty:
        raise RuntimeError(f"해외 일봉 데이터 비어있음 (KIS+yfinance): {symbol}")
    return df


def _fetch_us_history_kis(client: KISClient, symbol: str, exchange: str,
                          days: int) -> pd.DataFrame | None:
    """KIS 해외 일봉 조회. 빈 데이터면 None (rt_cd!=0이면 RuntimeError)."""
    resp = client.get_overseas_daily_price(symbol, exchange=exchange)
    if resp.get("rt_cd") != "0":
        raise RuntimeError(f"해외 일봉 실패: {resp.get('msg1', 'unknown')}")

    rows = resp.get("output2", [])
    if not rows:
        log.warning("us_daily_empty", symbol=symbol, rt_cd=resp.get("rt_cd"),
                     msg=resp.get("msg1", ""), output1_keys=list(resp.get("output1", {}).keys()) if resp.get("output1") else None)
        return None

    df = pd.DataFrame(rows)
    # KIS 해외 일봉 컬럼: xymd(날짜), open, high, low, clos(종가), tvol(거래량)
    rename_map = {
        "xymd": "date",
        "open": "open",
        "high": "high",
        "low": "low",
        "clos": "close",
        "tvol": "volume",
    }
    df = df.rename(columns=rename_map)

    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").set_index("date")
    return df[["open", "high", "low", "close", "volume"]].tail(days)


def _fetch_us_history_yf(symbol: str, days: int = 70) -> pd.DataFrame | None:
    """yfinance 폴백 — 미국 일봉 OHLCV. 실패 시 None."""
    try:
        import yfinance as yf

        period_days = max(days * 2, 120)  # 여유 조회 후 tail
        df = yf.download(symbol, period=f"{period_days}d", interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            return None
        # yfinance 신버전은 단일 티커도 MultiIndex 컬럼을 줄 수 있음 → 평탄화
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        if "close" not in cols:
            return None
        df = df[cols].copy()
        df.index = pd.to_datetime(df.index)
        df.index.name = "date"
        return df.tail(days)
    except Exception as e:  # noqa: BLE001
        log.warning("us_daily_yf_failed", symbol=symbol, error=str(e))
        return None


def get_us_price(client: KISClient, symbol: str, exchange: str = "NASD") -> float:
    """해외주식 현재가 (USD)."""
    try:
        resp = client.get_overseas_price(symbol, exchange=exchange)
        if resp.get("rt_cd") == "0":
            return float(resp.get("output", {}).get("last", 0))
    except Exception:
        pass
    return 0.0


# ──────────────────────────────────────────────────────────
# 미국장 잔고 / 포지션
# ──────────────────────────────────────────────────────────

def get_us_holdings(client: KISClient) -> dict[str, dict]:
    """미국 ETF 보유 현황. {심볼: {qty, avg_price, ...}}"""
    result = {}
    try:
        for excd in ["NASD", "NYSE", "AMEX"]:
            resp = client.get_overseas_balance(exchange=excd)
            if resp.get("rt_cd") == "0":
                for item in resp.get("output1", []):
                    qty = int(item.get("ovrs_cblc_qty", 0))
                    if qty > 0:
                        sym = item.get("ovrs_pdno", "")
                        result[sym] = {
                            "qty": qty,
                            "avg_price": float(item.get("pchs_avg_pric", 0)),
                            "current_price": float(item.get("now_pric2", 0)),
                            "pnl_pct": float(item.get("evlu_pfls_rt", 0)),
                            "exchange": excd,
                        }
    except Exception as e:
        log.error("us_balance_failed", error=str(e))
    return result


def get_us_available_cash(client: KISClient) -> float:
    """미국장 주문 가능 USD 잔고.

    통합증거금 신청 계좌는 KRW 잔고도 USD로 환산해서 매수 가능하므로
    inquire-psamount endpoint의 frcr_ord_psbl_amt1 (외화 주문가능금액)을
    우선 사용. 이 필드가 통합증거금 환산값까지 포함해서 반환됨.

    echm_af_ord_psbl_amt는 (예약된 환전 이후 추가 가용 금액)인데 보통 0,
    실제 매수 가능한 금액은 frcr_ord_psbl_amt1에 잡힌다.

    중요: KIS overseas price endpoint는 미국장 마감 후 0 반환할 수 있어
    QQQ 가격 fetch 실패해도 reference price ($500)로 psamount 호출.
    psamount의 OVRS_ORD_UNPR는 매수가능수량 계산용일 뿐 가용 잔고 계산엔 무관.
    """
    # 1차: inquire-psamount (통합증거금 반영된 외화 가용 금액)
    try:
        # 가격 fetch 실패해도 reference price로 호출. KIS는 ITEM_CD + price를 요구하지만
        # 가용 금액 계산엔 price가 영향 안 줌. (수량 계산용)
        ref_price = get_us_price(client, "QQQ", "NASD") or 500.0
        resp = client.get_overseas_psamount("QQQ", ref_price, exchange="NASD")
        if resp.get("rt_cd") == "0":
            output = resp.get("output", {})
            if isinstance(output, list) and output:
                output = output[0]
            # frcr_ord_psbl_amt1: 통합증거금 적용된 외화 주문가능금액 (KIS가 환산)
            # echm_af_ord_psbl_amt: 환전 이후 추가 가용 (보통 0, 명시적 환전 신청 후만 비제로)
            frcr = float(output.get("frcr_ord_psbl_amt1", 0) or 0)
            echm = float(output.get("echm_af_ord_psbl_amt", 0) or 0)
            # 둘 중 큰 값을 사용 (보수적 매수 가능 추정)
            available = max(frcr, echm)
            if available > 0:
                return available
    except Exception as e:
        log.warning("us_psamount_failed", error=str(e))

    # 2차 fallback: 잔고 조회의 외화 단독 잔고 (통합증거금 미반영)
    try:
        resp = client.get_overseas_balance(exchange="NASD")
        if resp.get("rt_cd") == "0":
            output2 = resp.get("output2", {})
            if isinstance(output2, list) and output2:
                output2 = output2[0]
            usd = float(output2.get("frcr_ord_psbl_amt1", 0))
            if usd > 0:
                return usd
    except Exception as e:
        log.error("us_cash_failed", error=str(e))

    # 3차 fallback: 국내 예수금(KRW)을 보수적 환율로 환산
    # 통합증거금 미신청 + USD $0일 때도 예산 추정 가능하게 함
    try:
        resp = client.get_balance()
        if resp.get("rt_cd") == "0":
            output2 = resp.get("output2", [])
            if isinstance(output2, list) and output2:
                output2 = output2[0]
            krw = int(output2.get("dnca_tot_amt", 0))
            if krw > 10000:
                est_usd = krw / 1450
                log.info("us_cash_krw_fallback", krw=krw, est_usd=round(est_usd, 2))
                return est_usd
    except Exception as e:
        log.error("us_cash_krw_fallback_failed", error=str(e))

    return 0.0


# ──────────────────────────────────────────────────────────
# 미국장 포지션 관리 (국내와 별도)
# ──────────────────────────────────────────────────────────

def load_us_positions() -> dict:
    if US_POSITIONS_PATH.exists():
        try:
            with US_POSITIONS_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_us_positions(positions: dict) -> None:
    US_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with US_POSITIONS_PATH.open("w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def record_us_buy(symbol: str, price: float, qty: int, exchange: str = "NASD",
                  asset_type: str = "us_long") -> None:
    positions = load_us_positions()
    positions[symbol] = {
        "buy_price": price,
        "buy_time": datetime.now(KST).isoformat(),
        "qty": qty,
        "exchange": exchange,
        "asset_type": asset_type,
        "peak_price": price,
    }
    save_us_positions(positions)


def remove_us_position(symbol: str) -> None:
    positions = load_us_positions()
    positions.pop(symbol, None)
    save_us_positions(positions)


# ──────────────────────────────────────────────────────────
# 미국장 리스크 관리
# ──────────────────────────────────────────────────────────

def check_us_stop_loss(symbol: str, current_price: float, cfg: dict) -> tuple[bool, str]:
    """미국 ETF 손절/추적손절 확인."""
    positions = load_us_positions()
    pos = positions.get(symbol)
    if not pos:
        return False, ""

    buy_price = pos["buy_price"]
    if buy_price <= 0:
        return False, ""
    peak_price = pos.get("peak_price", buy_price)

    pnl_pct = (current_price - buy_price) / buy_price
    stop_pct = cfg.get("strategy", {}).get("stop_loss_pct", 0.025)
    trailing_activate = cfg.get("strategy", {}).get("trailing_activate_pct", 0.02)
    trailing_stop = cfg.get("strategy", {}).get("trailing_stop_pct", 0.01)

    # 최고가 갱신
    if current_price > peak_price:
        positions[symbol]["peak_price"] = current_price
        save_us_positions(positions)
        peak_price = current_price

    # 손절
    if pnl_pct <= -stop_pct:
        return True, f"US 손절 ({pnl_pct:+.1%} ≤ -{stop_pct:.1%})"

    # 추적 손절
    peak_pnl = (peak_price - buy_price) / buy_price
    if peak_pnl >= trailing_activate:
        drop = (current_price - peak_price) / peak_price
        if drop <= -trailing_stop:
            return True, f"US 추적손절 (고점 ${peak_price:.2f}에서 {drop:+.1%})"

    return False, ""


# ──────────────────────────────────────────────────────────
# 미국장 전략 실행
# ──────────────────────────────────────────────────────────

def run_us_strategy(client: KISClient, dry_run: bool) -> int:
    """미국 ETF 변동성 돌파 전략 1회 실행.

    Returns:
        매수 사용 금액 (USD cents 기준, 0이면 미매수)
    """
    cfg = load_us_config()
    if not cfg.get("enabled", False):
        return 0

    universe = cfg.get("universe", [])
    strat_cfg = cfg.get("strategy", {})
    k = strat_cfg.get("k", 0.5)
    ma = strat_cfg.get("trend_ma", 20)
    ta_min = strat_cfg.get("ta_min_score", 15)
    max_pos = cfg.get("max_positions", 2)

    strategy = VolatilityBreakoutStrategy(k=k, trend_ma=ma)

    # 현재 보유 확인
    us_positions = load_us_positions()
    if len(us_positions) >= max_pos:
        print(f"  [US] 최대 포지션 도달 ({len(us_positions)}/{max_pos})")
        return 0

    # 예산 계산
    cash_usd = get_us_available_cash(client)
    budget_pct = cfg.get("budget_pct", 0.40)
    budget = cash_usd * budget_pct
    if budget < 10:
        print(f"  [US] 예산 부족 (${cash_usd:.2f} × {budget_pct:.0%} = ${budget:.2f})")
        return 0

    print(f"  [US] 예산: ${budget:.2f} (총 ${cash_usd:.2f}) | K={k}, MA={ma}")

    # 레짐 연동: 한국장 bear면 인버스 우선
    regime_linked = cfg.get("regime_linked", True)
    prefer_inverse = False
    if regime_linked:
        try:
            with CONFIG_PATH.open(encoding="utf-8") as f:
                full_cfg = yaml.safe_load(f)
            bear_state_path = Path("logs/bear_state.json")
            if bear_state_path.exists():
                with bear_state_path.open("r", encoding="utf-8") as f:
                    bear_state = json.load(f)
                if bear_state.get("regime") in ("BEAR", "CRISIS"):
                    prefer_inverse = True
                    print(f"  [US] 한국장 {bear_state['regime']} → 인버스(SH) 우선")
        except Exception:
            pass

    # 인버스 우선일 때 유니버스 재정렬
    if prefer_inverse:
        inverse_first = [s for s in universe if s.get("type") == "inverse"]
        others = [s for s in universe if s.get("type") != "inverse"]
        universe = inverse_first + others

    for stock in universe:
        symbol = stock["symbol"]
        name = stock["name"]
        exchange = stock.get("exchange", "NASD")
        asset_type = stock.get("type", "us_long")
        if asset_type == "inverse":
            asset_type = "us_inverse"
        elif asset_type == "defensive":
            asset_type = "us_defensive"
        else:
            asset_type = "us_long"

        # 이미 보유 중이면 스킵
        if symbol in us_positions:
            continue

        try:
            history = fetch_us_history(client, symbol, exchange=exchange)
            signal = strategy.generate_signal(symbol, history)
            cur_price = float(signal.price)

            print(f"  [US] {name} {signal.type.value} @ ${cur_price:.2f} — {signal.reason}")

            if signal.type.value != "BUY":
                # TA 보조 확인 (돌파 없어도 TA 강하면 평가)
                ta = compute_ta_score(history)
                if ta.total < ta_min:
                    log_decision(symbol, name, "skip",
                                 f"US 미돌파 + TA 부족 ({ta.total:+.0f})",
                                 cur_price, strategy="us_etf")
                    continue
                print(f"    TA={ta.total:+.0f} 강함, 추가 평가")
            else:
                ta = compute_ta_score(history)
                if ta.total < 0:
                    log_decision(symbol, name, "skip",
                                 f"US 돌파했으나 TA 음수 ({ta.total:+.0f})",
                                 cur_price, strategy="us_etf")
                    continue

            # 펀더멘털 게이트 (개별주만)
            try:
                from src.strategies.fundamental_gate import check_fundamentals
                fund = check_fundamentals(symbol)
                if not fund.passed:
                    print(f"    [펀더멘털] {fund.reason}")
                    log_decision(symbol, name, "skip", fund.reason,
                                 cur_price, strategy="us_etf")
                    continue
                if "통과" in fund.reason:
                    print(f"    [펀더멘털] {fund.reason}")
            except Exception:
                pass

            # 매수 수량 계산
            qty = int(budget // cur_price)
            if qty <= 0:
                continue

            total_usd = qty * cur_price
            print(f"    [US BUY] {name} {qty}주 @ ${cur_price:.2f} = ${total_usd:.2f} "
                  f"(TA={ta.total:+.0f})")

            if not dry_run:
                resp = client.order_overseas(
                    symbol, qty, price=cur_price,
                    side="buy", exchange=exchange, order_type="00",
                )
                rt = resp.get("rt_cd")
                print(f"      응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, name, "buy", qty, int(cur_price * 100),  # cents로 기록
                              market="US",
                              reason=f"US 매수: 변동성 돌파 + TA {ta.total:+.0f}")
                    record_us_buy(symbol, cur_price, qty, exchange, asset_type)
                    log_decision(symbol, name, "buy",
                                 f"US 매수 (TA={ta.total:+.0f})",
                                 cur_price, qty=qty, strategy="us_etf")
                    return int(total_usd * 100)
                elif rt == "E":
                    log.warning("us_buy_error", symbol=symbol, msg=resp.get("msg1", ""))
            else:
                print("      (dry-run)")
                record_us_buy(symbol, cur_price, qty, exchange, asset_type)
                log_decision(symbol, name, "buy",
                             f"US 매수 dry-run (TA={ta.total:+.0f})",
                             cur_price, qty=qty, strategy="us_etf")
                return int(total_usd * 100)

        except Exception as e:
            print(f"    ERROR: {e}")

    print("  [US] 돌파 종목 없음.")
    return 0


def check_us_risk(client: KISClient, dry_run: bool) -> None:
    """미국 보유 종목 리스크 체크 + 매도."""
    cfg = load_us_config()
    positions = load_us_positions()
    if not positions:
        return

    for symbol, pos in list(positions.items()):
        exchange = pos.get("exchange", "NASD")
        cur_price = get_us_price(client, symbol, exchange)
        if cur_price <= 0:
            continue

        should_sell, reason = check_us_stop_loss(symbol, cur_price, cfg)
        if should_sell:
            qty = pos.get("qty", 0)
            print(f"  [US 리스크] {symbol} {qty}주 @ ${cur_price:.2f} — {reason}")
            if not dry_run:
                resp = client.order_overseas(
                    symbol, qty, price=cur_price,
                    side="sell", exchange=exchange, order_type="00",
                )
                rt = resp.get("rt_cd")
                print(f"    응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    log_trade(symbol, f"US_{symbol}", "sell", qty, int(cur_price * 100),
                              market="US", reason=f"매도: {reason}")
                    remove_us_position(symbol)
            else:
                print("    (dry-run)")
                remove_us_position(symbol)


def close_us_positions(client: KISClient, dry_run: bool) -> None:
    """미국장 마감 전 전체 청산."""
    positions = load_us_positions()
    if not positions:
        return

    print(f"  [US 청산] {len(positions)}개 포지션 청산")
    for symbol, pos in list(positions.items()):
        exchange = pos.get("exchange", "NASD")
        qty = pos.get("qty", 0)
        cur_price = get_us_price(client, symbol, exchange)
        if cur_price <= 0:
            cur_price = pos.get("buy_price", 0)

        print(f"    {symbol} {qty}주 @ ${cur_price:.2f}")
        if not dry_run:
            resp = client.order_overseas(
                symbol, qty, price=cur_price,
                side="sell", exchange=exchange, order_type="00",
            )
            rt = resp.get("rt_cd")
            print(f"      응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
            if rt == "0":
                log_trade(symbol, f"US_{symbol}", "sell", qty, int(cur_price * 100),
                          market="US", reason="매도: 미국장 마감 청산")
                remove_us_position(symbol)
        else:
            print("      (dry-run)")
            remove_us_position(symbol)
