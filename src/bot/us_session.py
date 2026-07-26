"""미국 ETF 야간 매매 전략 — 한국 밤 시간대 미국장 운영.

시간대 (KST 기준) — src/utils/clock.py가 ET 09:30/16:00을 변환해 자동 판정:
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
import math
import time as time_mod
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from src.config import settings
from src.kis_client import KISClient
from src.strategies.volatility_breakout import VolatilityBreakoutStrategy
from src.strategies.morning_momentum import (
    morning_momentum_signal, should_exit_morning, can_reenter,
)
from src.strategies.ta_composite import compute_ta_score
from src.risk_manager import (load_positions, save_positions, record_buy,
                              remove_position, apply_min_position)
from src.tracker import log_trade
from src.experience import log_decision
from src.utils.logger import log
from src.utils.clock import (
    KST, ET, now_kst, kst_stamp, parse_kst, us_session_date_et, is_us_dst,
)
# 캘린더 인식 버전(조기 폐장 반영) — clock.us_market_times_kst의 정규장 전용
# 버전이 아니라 이쪽을 써야 조기 폐장일에 강제청산 타이밍이 맞는다.
from src.utils.market_calendar import us_market_times_kst, is_us_trading_day

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
    """미국 정규장 시간인지 확인 (KST 기준). 휴장일·조기폐장 반영."""
    if not load_us_config().get("enabled", False):
        return False

    now_dt = now_kst()
    if not is_us_trading_day(us_session_date_et(now_dt)):
        return False

    now = now_dt.time()
    open_t, close_t = get_us_market_times(now_dt)

    # 자정 넘어가는 시간 처리
    if open_t > close_t:
        return now >= open_t or now <= close_t
    return open_t <= now <= close_t


def get_us_market_times(now: datetime | None = None) -> tuple[dtime, dtime]:
    """(open_kst, close_kst) 반환. 서머타임은 ET 기준으로 자동 판정.

    예전엔 configs/strategy.yaml의 `summer_time` 불리언을 사람이 연 2회 직접
    뒤집어야 했다. 놓치면 폐장 시각을 오인해 (a) 실제 폐장 1시간 전에 강제 청산하고
    (b) 장이 열린 마지막 1시간 동안 손절이 멈추거나, 반대 방향에선 이미 닫힌 장에
    청산 주문을 넣어 실패 → 의도치 않은 오버나이트 캐리가 났다.
    이제 ET 09:30/16:00을 KST로 변환해 DST 전환을 자동으로 따라간다.

    `summer_time` 키는 남겨두되 계산값과 어긋나면 경고만 남긴다(값은 무시).
    조기 폐장일(13:00 ET)이면 폐장이 3시간 앞당겨진다.
    """
    open_t, close_t = us_market_times_kst(now)
    _warn_stale_summer_flag_once(now, open_t, close_t)
    return open_t, close_t


_summer_flag_warned = False


def _warn_stale_summer_flag_once(now, open_t, close_t) -> None:
    """yaml의 summer_time이 계산값과 어긋나면 경고 (프로세스당 1회).

    get_us_market_times()는 루프에서 분당 여러 번 호출되므로 매번 찍으면
    DST 전환 후 로그가 경고로 도배된다.
    """
    global _summer_flag_warned
    if _summer_flag_warned:
        return
    cfg_summer = load_us_config().get("summer_time")
    if cfg_summer is None or bool(cfg_summer) == is_us_dst(now):
        return
    _summer_flag_warned = True
    log.warning("us_summer_time_flag_stale",
                config_value=bool(cfg_summer), computed_dst=is_us_dst(now),
                open_kst=open_t.strftime("%H:%M"),
                close_kst=close_t.strftime("%H:%M"),
                hint="configs/strategy.yaml us_session.summer_time은 더 이상 "
                     "사용되지 않습니다(ET 기준 자동 계산). 제거해도 됩니다.")


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


# ──────────────────────────────────────────────────────────
# 체결 품질 — 마켓터블 지정가 + 실체결 확인
# ──────────────────────────────────────────────────────────

DEFAULT_LIMIT_BUFFER_PCT = 0.0015   # 0.15% — 유동성 좋은 ETF 스프레드(0.01~0.03%)의 5~10배 여유
DEFAULT_FILL_WAIT_SEC = 6.0
DEFAULT_FILL_POLL_SEC = 1.5


def load_us_execution_config() -> dict:
    return load_us_config().get("execution", {}) or {}


def marketable_limit_price(side: str, last: float, buffer_pct: float | None = None) -> float:
    """스프레드를 건너 즉시 체결되도록 만든 지정가 (센트 단위 정규화).

    **왜 필요한가** — 예전엔 최종체결가(last)에 지정가를 그대로 걸었다. KIS 해외는
    호가(bid/ask) 조회가 없어 스프레드를 볼 수 없는데, last에 건 지정가는
    호가창 안쪽에 수동으로 얹히는 주문이라 **역선택**에 그대로 노출된다:

      - 매수: ask가 내 가격까지 **내려와야** 체결 → 하락 중에만 체결 → 체결 직후
        더 하락. 결과적으로 "비싸게 산" 모양이 된다.
      - 매도: bid가 내 가격까지 **올라와야** 체결 → 상승 중에만 체결 → 체결 직후
        더 상승. "싸게 판" 모양이 된다.
      - 즉 **판단이 틀렸을 때만 체결되고, 맞았을 땐 미체결**로 남는 구조다.

    지정가는 **내 한도가 아니라 호가창의 최우선 가격에 체결**되므로, 한도를
    스프레드 너머로 걸어도 실제 체결가는 ask(매수)/bid(매도)다. buffer는 지불
    가격이 아니라 **최악 체결가의 상한**이다. 유동성 좋은 ETF에서 이 비용은
    한 틱(≈0.01~0.03%) 수준으로, 역선택 비용보다 훨씬 싸다.

    Args:
        side: "buy" / "sell"
        last: KIS 최종체결가 (USD)
        buffer_pct: 상한 여유. None이면 설정값 → 기본 0.15%
    """
    if last <= 0:
        return 0.0
    if buffer_pct is None:
        buffer_pct = float(load_us_execution_config()
                           .get("limit_buffer_pct", DEFAULT_LIMIT_BUFFER_PCT))
    buffer_pct = max(0.0, float(buffer_pct))

    if side == "buy":
        raw = last * (1.0 + buffer_pct)
        # 센트 올림 — 내림하면 ask에 못 닿아 다시 수동 주문이 될 수 있다
        return math.ceil(raw * 100 - 1e-9) / 100
    if side == "sell":
        raw = last * (1.0 - buffer_pct)
        return max(0.01, math.floor(raw * 100 + 1e-9) / 100)
    raise ValueError(f"side는 'buy' 또는 'sell': {side}")


def _held_qty(client: KISClient, symbol: str) -> tuple[int, float]:
    """브로커 잔고 기준 (보유수량, 평단). 조회 실패 시 (-1, 0.0)."""
    try:
        info = (get_us_holdings(client) or {}).get(symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("us_holdings_lookup_failed", symbol=symbol, error=str(e))
        return -1, 0.0
    if not info:
        return 0, 0.0
    try:
        return int(float(info.get("qty", 0) or 0)), float(info.get("avg_price", 0) or 0)
    except (TypeError, ValueError):
        return 0, 0.0


def confirm_us_fill(client: KISClient, symbol: str, side: str, qty_before: int,
                    expected_qty: int) -> tuple[int, float]:
    """주문 후 브로커 잔고를 폴링해 **실제 체결 수량·평단**을 확인.

    `rt_cd == "0"`은 "주문 접수"일 뿐 체결이 아니다. 예전엔 접수만 보고 요청가로
    포지션·거래기록을 남겨서 (a) 미체결인데 보유로 잡히는 유령 포지션, (b) 실제
    체결가와 다른 손절 기준, (c) 저널 손익 왜곡이 생겼다.

    Returns:
        (체결수량, 평단USD). 확인 실패 시 (-1, 0.0) — 호출부가 요청값으로 폴백.
    """
    cfg = load_us_execution_config()
    wait_total = float(cfg.get("fill_wait_sec", DEFAULT_FILL_WAIT_SEC))
    poll = float(cfg.get("fill_poll_sec", DEFAULT_FILL_POLL_SEC))

    deadline = time_mod.monotonic() + max(0.0, wait_total)
    last_qty, last_avg = -1, 0.0
    while True:
        cur_qty, avg = _held_qty(client, symbol)
        if cur_qty >= 0:
            last_qty, last_avg = cur_qty, avg
            filled = (cur_qty - qty_before) if side == "buy" else (qty_before - cur_qty)
            if filled >= expected_qty:          # 전량 체결
                return filled, avg
            if time_mod.monotonic() >= deadline:
                return max(0, filled), avg      # 부분/미체결
        if time_mod.monotonic() >= deadline:
            break
        time_mod.sleep(min(poll, max(0.1, deadline - time_mod.monotonic())))

    if last_qty < 0:
        return -1, 0.0
    filled = (last_qty - qty_before) if side == "buy" else (qty_before - last_qty)
    return max(0, filled), last_avg


DEFAULT_EOD_LIMIT_BUFFER_PCT = 0.004   # 0.4% — 마감청산 미체결 = 오버나이트 캐리라 더 공격적


def _eod_buffer_pct() -> float:
    return float(load_us_execution_config()
                 .get("eod_limit_buffer_pct", DEFAULT_EOD_LIMIT_BUFFER_PCT))


def _sell_and_record(client: KISClient, symbol: str, exchange: str, qty: int,
                     ref_price: float, limit_px: float, reason: str,
                     eod: bool = False) -> bool:
    """US 매도 주문 → 실체결 확인 → 기록. 전량 체결 시에만 포지션을 제거한다.

    예전엔 rt_cd=0(접수)만 보고 곧바로 remove_us_position()을 호출했다. 미체결이면
    실제로는 계속 보유 중인데 봇의 장부에서 사라져 **손절·마감청산 관리 대상에서
    빠지는 유령 포지션**이 됐다.
    """
    qty_before, _ = _held_qty(client, symbol)
    resp = client.order_overseas(symbol, qty, price=limit_px,
                                 side="sell", exchange=exchange, order_type="00")
    rt = resp.get("rt_cd")
    print(f"      응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
    if rt != "0":
        log.warning("us_sell_rejected", symbol=symbol, qty=qty,
                    msg=resp.get("msg1", ""), eod=eod)
        return False

    filled, _ = confirm_us_fill(client, symbol, "sell", max(0, qty_before), qty)
    if filled < 0:                      # 확인 실패 → 기존 동작(접수=성공)으로 폴백
        log.warning("us_sell_fill_check_failed", symbol=symbol, qty=qty)
        filled = qty
    if filled <= 0:
        print(f"      ⚠️ 미체결 — 포지션 유지(다음 주기 재시도)")
        log.warning("us_sell_unfilled", symbol=symbol, qty=qty,
                    limit=limit_px, eod=eod)
        return False

    px = limit_px if ref_price <= 0 else ref_price
    log_trade(symbol, f"US_{symbol}", "sell", filled, int(px * 100),
              market="US", reason=reason)
    if filled < qty:
        # 부분체결 — 남은 수량은 계속 관리해야 한다
        positions = load_us_positions()
        if symbol in positions:
            positions[symbol]["qty"] = max(0, qty - filled)
            save_us_positions(positions)
        print(f"      부분체결 {filled}/{qty}주 — 잔여 {qty - filled}주 계속 관리")
        return False
    remove_us_position(symbol)
    return True


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


def get_us_assets_krw(client: KISClient) -> int:
    """US 자산(USD 현금 + 보유 평가)을 **KIS 제공 환율(exrt)**로 환산한 KRW.

    총자산 합산용. 외부(yfinance) 환율 대신 KIS psamount 응답의 exrt를 직접 써
    계좌와 정확히 일치시킨다(6/25: dnca 577,424 + $205.64×exrt = 실제 총자산).
    순수 USD 현금은 ovrs_ord_psbl_amt(앱 주문가능달러), 매수여력 frcr은 KRW
    이중계상이라 제외.
    """
    try:
        ref_price = get_us_price(client, "QQQ", "NASD") or 500.0
        resp = client.get_overseas_psamount("QQQ", ref_price, exchange="NASD")
        if resp.get("rt_cd") != "0":
            return 0
        o = resp.get("output", {})
        if isinstance(o, list) and o:
            o = o[0]
        usd = float(o.get("ovrs_ord_psbl_amt", 0) or 0)
        exrt = float(o.get("exrt", 0) or 0)
        if exrt <= 0 or usd <= 0:
            return 0
        # US 보유분 평가(USD) 추가
        uh = get_us_holdings(client) or {}
        for s, p in (load_us_positions() or {}).items():
            d = uh.get(s, {})
            usd += float(d.get("qty", p.get("qty", 0))) * \
                float(d.get("current_price", p.get("buy_price", 0)))
        return int(usd * exrt)
    except Exception as e:
        log.warning("us_assets_krw_failed", error=str(e))
    return 0


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
        "buy_time": kst_stamp(),
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


def adopt_us_carried_positions(broker_holdings: dict, universe_symbols: set,
                               traded_symbols: set) -> int:
    """이전 세션에서 산 US 캐리 포지션을 us_positions.json에 흡수(손절·청산 관리 대상화).

    KR -14% 방치 사태(6-08)와 같은 버그 클래스의 US판 방지: 세션이 중간에 죽으면
    (타임아웃·스케줄 공백·취소) 보유 포지션이 다음 run의 빈 us_positions에 안 잡혀
    손절도 마감청산도 안 되는 유령이 된다. KR adopt_carried_positions와 동일 정책 —
    **봇 거래이력(traded)** 보유분만 흡수, 거래이력 없는 수동 보유분은 보호.
    universe_symbols는 참고용.

    Args:
        broker_holdings: get_us_holdings() 결과 {sym: {qty, avg_price, current_price, exchange}}
        universe_symbols: US 유니버스 심볼 집합 (참고용)
        traded_symbols: 봇이 거래한 심볼 집합 (canonical trades.csv 기준)
    Returns: 흡수한 포지션 수
    """
    positions = load_us_positions()
    adopted = 0
    for sym, info in (broker_holdings or {}).items():
        if sym in positions:
            continue
        if sym not in traded_symbols:
            continue  # 봇 거래이력 없는 수동 보유분 → 보호
        try:
            buy_p = float(info.get("avg_price", 0) or 0)
            qty = int(float(info.get("qty", 0) or 0))
        except (TypeError, ValueError):
            continue
        if buy_p <= 0 or qty <= 0:
            continue
        cur = float(info.get("current_price", buy_p) or buy_p)
        positions[sym] = {
            "buy_price": buy_p,
            "buy_time": kst_stamp(),
            "qty": qty,
            "exchange": info.get("exchange", "NASD"),
            "asset_type": "us_long",
            "peak_price": max(buy_p, cur),
            "adopted": True,
        }
        adopted += 1
    if adopted:
        save_us_positions(positions)
    return adopted


def adopt_us_carry_and_verify(client: KISClient) -> None:
    """US 세션 시작 시 캐리 포지션 흡수 — broker 잔고와 대조해 유령 포지션 복구."""
    try:
        from src.merge_trades import traded_symbols
        broker = get_us_holdings(client)
        if not broker:
            return
        cfg = load_us_config()
        uni = {s["symbol"] for s in (cfg.get("universe") or [])}
        traded = traded_symbols("logs/trades.csv")
        n = adopt_us_carried_positions(broker, uni, traded)
        if n:
            print(f"  [US 캐리 흡수] 이전 세션 보유분 {n}개를 손절·청산 관리 대상으로 복구")
            log.info("us_carry_adopted", count=n)
    except Exception as e:
        log.warning("us_carry_adopt_skipped", error=str(e))


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

    # 하드 익절: 큰 이익은 마감 안 기다리고 장중 바로 확보
    take_profit = cfg.get("strategy", {}).get("take_profit_pct", 0.0)
    if take_profit > 0 and pnl_pct >= take_profit:
        return True, f"US 익절 ({pnl_pct:+.1%} ≥ +{take_profit:.1%})"

    # 손절
    if pnl_pct <= -stop_pct:
        return True, f"US 손절 ({pnl_pct:+.1%} ≤ -{stop_pct:.1%})"

    # 추적 손절
    peak_pnl = (peak_price - buy_price) / buy_price
    if peak_pnl >= trailing_activate:
        drop = (current_price - peak_price) / peak_price
        if drop <= -trailing_stop:
            return True, f"US 추적손절 (고점 ${peak_price:.2f}에서 {drop:+.1%})"

    # 본전 보존: 한번 +breakeven_trigger 이익권에 올랐다 반전하면 본전+에서 청산.
    # 사용자 지적(XLF 마감청산 손실): 이겼다 손실/본전이하로 넘어가기 전에 이익권 확보.
    be_trigger = cfg.get("strategy", {}).get("breakeven_trigger_pct", 0.007)
    be_buffer = cfg.get("strategy", {}).get("breakeven_buffer_pct", 0.001)
    if be_trigger > 0 and peak_pnl >= be_trigger and current_price <= buy_price * (1 + be_buffer):
        return True, (f"US 본전이익 보존 (고점 +{peak_pnl:.1%}였다가 반전 → "
                      f"{pnl_pct:+.1%}, 손절 전 이익권 청산)")

    return False, ""


def eod_us_hold_decision(buy_price: float, cur_price: float,
                         cfg: dict) -> tuple[bool, str]:
    """미국장 마감 시 오버나이트 보유 여부 (순수 함수).

    매일 전량청산하면 왕복 0.5% 수수료가 매일 빠져 얕은 거래는 구조적 적자
    (6-10~16 SCHG/XLF churn). 대신 **수익+추세가 살아있는 winner만 보유**하고,
    손실·약세는 청산해 churn·오버나이트 갭리스크를 피한다.

    보유 조건: 수익률 ≥ trailing_activate_pct (추세 winner). 그 외 청산.
    cfg.eod_hold_winners=false면 구 동작(전량청산).

    Returns: (keep_overnight, reason)
    """
    if not cfg.get("eod_hold_winners", True):
        return False, "전량청산(eod_hold_winners=false)"
    if not (buy_price and buy_price > 0) or not (cur_price and cur_price > 0):
        return False, "가격 불명 → 청산"
    pnl_pct = (cur_price - buy_price) / buy_price
    activate = float(cfg.get("strategy", {}).get("trailing_activate_pct", 0.02))
    if pnl_pct >= activate:
        return True, f"수익 {pnl_pct:+.1%} ≥ +{activate:.0%} — 추세 winner 오버나이트 보유"
    return False, f"수익 {pnl_pct:+.1%} < +{activate:.0%} — 청산(churn·갭리스크 회피)"


# ──────────────────────────────────────────────────────────
# 미국장 전략 실행
# ──────────────────────────────────────────────────────────

def check_us_daily_loss_limit(client: KISClient | None = None) -> tuple[bool, str]:
    """US 세션 당일 손실이 한도를 넘었는지. True면 신규 매수 차단.

    한국장에는 check_daily_loss_limit이 있었지만 US 세션에는 아무 한도가 없었다.
    원인은 시간대다 — 기존 함수는 KST 날짜로 trades.csv를 필터링하는데, US 세션은
    KST 자정을 가로지르므로 **세션 도중 00:00에 당일 손익이 리셋**된다.
    여기서는 US 거래일(ET)로 키잉해 세션 전체를 하나로 집계한다.

    한도 의미는 국내판과 동일하게 맞춘다 — 같은 risk.daily_loss_limit_pct를
    공유하는데 분모가 다르면 "5%"가 시장마다 다른 뜻이 되기 때문이다:
      - 실현 + 미실현 손익 합계를
      - **US 평가자산(보유 평가액 + 가용 현금)** 으로 나눈다.
    client가 없으면 미실현·자산 조회를 건너뛰고 실현손익만 원가 대비로 본다
    (보수적 폴백).

    trades.csv의 US 금액은 센트(=int(price*100)) 단위다.
    """
    import csv
    from src.tracker import TRADE_LOG_PATH, is_kr_symbol

    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            limit_pct = (yaml.safe_load(f).get("risk", {})
                         .get("daily_loss_limit_pct", 0.05))
    except Exception:
        limit_pct = 0.05
    if not limit_pct or limit_pct <= 0 or not TRADE_LOG_PATH.exists():
        return False, "한도 비활성 또는 거래 기록 없음"

    session = us_session_date_et(now_kst()).isoformat()
    buys: dict[str, list[int]] = {}
    realized_cents = 0
    cost_cents = 0

    try:
        with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = row.get("symbol", "")
                if is_kr_symbol(sym):          # 국내 체결(원)은 제외
                    continue
                try:
                    if us_session_date_et(parse_kst(row.get("timestamp", ""))) \
                            .isoformat() != session:
                        continue
                    price = int(row.get("price", 0) or 0)
                    qty = int(row.get("qty", 0) or 0)
                except Exception:  # noqa: BLE001
                    continue
                if row.get("side") == "buy":
                    buys.setdefault(sym, []).append(price * qty)
                    cost_cents += price * qty
                elif row.get("side") == "sell" and buys.get(sym):
                    realized_cents += price * qty - buys[sym].pop(0)
    except Exception as e:  # noqa: BLE001
        log.warning("us_daily_loss_check_failed", error=str(e))
        return False, "집계 실패"

    realized = realized_cents / 100.0        # USD

    # 미실현 + 평가자산 (국내판과 동일하게 미실현 손실도 반영)
    unrealized = 0.0
    equity = 0.0
    if client is not None:
        try:
            for info in (get_us_holdings(client) or {}).values():
                qty = float(info.get("qty", 0) or 0)
                avg = float(info.get("avg_price", 0) or 0)
                cur = float(info.get("current_price", 0) or 0)
                if qty > 0 and cur > 0:
                    equity += qty * cur
                    if avg > 0:
                        unrealized += (cur - avg) * qty
            equity += get_us_available_cash(client)
        except Exception as e:  # noqa: BLE001
            log.warning("us_equity_lookup_failed", error=str(e))
            equity = 0.0

    total_loss = realized + min(0.0, unrealized)
    if total_loss >= 0:
        return False, f"당일 손익 {total_loss:+.2f} USD (이익 중)"

    # 분모: 평가자산 우선, 조회 실패 시 당일 투입원가로 폴백
    denom = equity if equity > 0 else cost_cents / 100.0
    if denom <= 0:
        return False, f"당일 손익 {total_loss:+.2f} USD (기준자산 미상)"

    loss_pct = abs(total_loss) / denom
    basis = "평가자산" if equity > 0 else "당일원가"
    if loss_pct >= limit_pct:
        return True, (f"US 일일 손실 한도 초과: {total_loss:+.2f} USD "
                      f"(실현 {realized:+.2f} / 미실현 {unrealized:+.2f}, "
                      f"{loss_pct:.1%} ≥ {limit_pct:.1%} of {basis}, 세션 {session})")
    return False, (f"당일 손익 {total_loss:+.2f} USD "
                   f"({loss_pct:.1%} / 한도 {limit_pct:.1%}, {basis})")


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

    # 일일 손실 한도 — 초과 시 신규 매수만 차단(청산·손절은 계속)
    loss_exceeded, loss_reason = check_us_daily_loss_limit(client)
    if loss_exceeded:
        print(f"  [US] ⚠️  {loss_reason} → 신규 매수 차단")
        log.warning("us_daily_loss_limit_hit", reason=loss_reason)
        return 0

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

    # 재진입 쿨다운: 최근 마감청산된 종목 재진입 금지(US 일일 churn 방지, 6-10~12 SCHG)
    #
    # 날짜는 반드시 **US 거래일(ET)** 공간에서 비교한다. 거래 기록은 KST 타임스탬프인데
    # US 마감청산은 항상 KST 자정 이후(04:45/05:45)에 일어나므로, KST 날짜로 비교하면
    # 청산 기록이 세션 날짜보다 하루 뒤로 찍힌다. 그 결과 cooldown_days=2가 실제로는
    # 3개 세션을 막았다. 게다가 기준 날짜를 함수 진입 시 한 번만 잡아서, 세션이 자정을
    # 넘으면 이후 호출에서 쿨다운 창이 하루 밀렸다.
    cooldown_days = int(strat_cfg.get("reentry_cooldown_days", 2) or 0)
    _recent_sells = []
    if cooldown_days > 0:
        try:
            from src.merge_trades import _read
            _recent_sells = [_to_us_session_row(t) for t in _read("logs/trades.csv")
                             if t.get("side") == "sell"]
        except Exception:
            _recent_sells = []
    _today = us_session_date_et(now_kst()).isoformat()

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

    # ── 인버스 churn 차단: 약세 확인 시에만 인버스 진입 ──
    # 순환매/상승장(2026 US: Nasdaq 약세지만 광의지수 상승)에서 인버스 데이트레이드는
    # 엣지가 약해 본전·수수료갈이. 한국장 BEAR(prefer_inverse) 또는 美 오버나이트 약세일
    # 때만 인버스 매수 허용, 그 외엔 인버스 스킵.
    inverse_requires_bearish = strat_cfg.get("inverse_requires_bearish", True)
    allow_inverse = (not inverse_requires_bearish) or prefer_inverse
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            _full = yaml.safe_load(f) or {}
        _ov = _full.get("overnight_signal", {}) or {}
        if _ov.get("direction") == "bearish" or \
                _ov.get("recommended_action") in ("skip", "reduce_size"):
            allow_inverse = True
    except Exception:
        pass
    if not allow_inverse:
        print("  [US] 인버스 진입 차단(약세조건 아님 — 순환매/상승장 churn 회피)")

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

        # 인버스 churn 차단: 약세조건 아니면 인버스 진입 스킵
        if asset_type == "us_inverse" and not allow_inverse:
            log_decision(symbol, name, "skip",
                         "인버스 스킵: 약세조건 아님(순환매/상승장 churn 차단)",
                         0, strategy="us_etf")
            continue

        # 이미 보유 중이면 스킵
        if symbol in us_positions:
            continue

        # 재진입 쿨다운: 최근 마감청산된 종목이면 churn 방지 위해 스킵
        if cooldown_days > 0:
            from src.strategies.cost_gate import recently_force_closed
            if recently_force_closed(symbol, _recent_sells, _today, cooldown_days):
                print(f"  [US] {symbol} 최근 {cooldown_days}일 내 마감청산 — 재진입 쿨다운(churn 방지)")
                log_decision(symbol, name, "skip", f"재진입 쿨다운 {cooldown_days}일",
                             0, strategy="us_etf")
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
                # 돌파라도 TA가 약하면 진입 금지(약한 TA+4 churn 차단, 6-16 SCHG).
                bo_ta_min = strat_cfg.get("breakout_ta_min", 10)
                if ta.total < bo_ta_min:
                    log_decision(symbol, name, "skip",
                                 f"US 돌파했으나 TA 약함 ({ta.total:+.0f} < {bo_ta_min})",
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

            # ── 수수료 인지 게이트: 기대변동(ATR%)이 미국 왕복수수료(~0.5%)를
            #    못 넘으면 진입 스킵. 데이트레이딩에서 수수료에 먹히는 얕은 거래 차단.
            try:
                from src.strategies.cost_gate import edge_clears_cost, atr_pct
                _h = history.tail(15)
                _avg_range = float((_h["high"] - _h["low"]).mean())
                _em = atr_pct(_avg_range, cur_price)
                _ok, _reason = edge_clears_cost(_em, "US")
                if not _ok:
                    print(f"    [수수료게이트] {_reason}")
                    log_decision(symbol, name, "skip", f"수수료게이트: {_reason}",
                                 cur_price, strategy="us_etf")
                    continue
            except Exception:
                pass

            # 마켓터블 지정가 — 스프레드를 건너 즉시 체결. 사이징도 이 가격 기준으로
            # 해야 한도 상향분만큼 예수금이 모자라 거부되는 일이 없다.
            buy_px = marketable_limit_price("buy", cur_price)

            # 매수 수량 계산
            qty = int(budget // buy_px)
            if qty <= 0:
                continue

            # 최소 포지션 금액 floor (단타: 소액 회피 → %수익이 수수료 넘게, KR과 동일 정책)
            _min_usd = float(strat_cfg.get("min_position_usd", 0) or 0)
            if _min_usd > 0:
                _avail_usd = get_us_available_cash(client)
                _q2 = apply_min_position(qty, buy_px, _avail_usd, _min_usd)
                if _q2 > qty:
                    print(f"    [최소포지션] {qty}주→{_q2}주 (≥${_min_usd:.0f}, 단타 수수료 대비 이득)")
                    qty = _q2

            total_usd = qty * buy_px
            print(f"    [US BUY] {name} {qty}주 @ 한도 ${buy_px:.2f} "
                  f"(현재가 ${cur_price:.2f}) ≤ ${total_usd:.2f} (TA={ta.total:+.0f})")

            if not dry_run:
                qty_before, _ = _held_qty(client, symbol)
                resp = client.order_overseas(
                    symbol, qty, price=buy_px,
                    side="buy", exchange=exchange, order_type="00",
                )
                rt = resp.get("rt_cd")
                print(f"      응답: rt_cd={rt}, msg={resp.get('msg1', '')}")
                if rt == "0":
                    # rt_cd=0은 '접수'일 뿐 체결이 아니다. 실제 체결수량·평단을
                    # 확인해 그 값으로 기록해야 손절 기준·저널 손익이 맞는다.
                    filled, avg_px = confirm_us_fill(client, symbol, "buy",
                                                     max(0, qty_before), qty)
                    if filled == 0:
                        print(f"      ⚠️ 미체결 — 포지션 기록 안 함 (다음 주기 재시도)")
                        log.warning("us_buy_unfilled", symbol=symbol, qty=qty,
                                    limit=buy_px)
                        continue
                    if filled < 0:                    # 확인 실패 → 요청값으로 폴백
                        filled, avg_px = qty, buy_px
                        log.warning("us_fill_check_failed_fallback",
                                    symbol=symbol, assumed_qty=qty, assumed_px=buy_px)
                    elif avg_px <= 0:
                        avg_px = buy_px
                    if filled < qty:
                        print(f"      부분체결 {filled}/{qty}주")
                    print(f"      체결: {filled}주 @ ${avg_px:.2f} "
                          f"(슬리피지 {(avg_px - cur_price) / cur_price:+.2%})")

                    log_trade(symbol, name, "buy", filled, int(avg_px * 100),  # cents로 기록
                              market="US",
                              reason=f"US 매수: 변동성 돌파 + TA {ta.total:+.0f}")
                    record_us_buy(symbol, avg_px, filled, exchange, asset_type)
                    log_decision(symbol, name, "buy",
                                 f"US 매수 (TA={ta.total:+.0f})",
                                 avg_px, qty=filled, strategy="us_etf")
                    return int(filled * avg_px * 100)
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
            sell_px = marketable_limit_price("sell", cur_price)
            print(f"  [US 리스크] {symbol} {qty}주 @ 한도 ${sell_px:.2f} "
                  f"(현재가 ${cur_price:.2f}) — {reason}")
            if not dry_run:
                _sell_and_record(client, symbol, exchange, qty, cur_price, sell_px,
                                 f"매도: {reason}")
            else:
                print("    (dry-run)")
                remove_us_position(symbol)


def close_us_positions(client: KISClient, dry_run: bool) -> None:
    """미국장 마감 처리 — 선별청산: 수익+추세 winner는 오버나이트 보유, 나머지 청산.

    매일 전량청산은 왕복 0.5% 수수료가 매일 빠져 얕은 거래가 구조적 적자.
    winner만 보유해 수수료를 큰 추세에 분산하고 churn을 줄인다(eod_us_hold_decision).
    """
    cfg = load_us_config()
    positions = load_us_positions()
    if not positions:
        return

    for symbol, pos in list(positions.items()):
        exchange = pos.get("exchange", "NASD")
        qty = pos.get("qty", 0)
        cur_price = get_us_price(client, symbol, exchange)
        if cur_price <= 0:
            cur_price = pos.get("buy_price", 0)

        keep, why = eod_us_hold_decision(pos.get("buy_price", 0), cur_price, cfg)
        if keep:
            print(f"  [US 마감보유] {symbol} {qty}주 @ ${cur_price:.2f} — {why}")
            continue

        # 마감청산은 미체결이 곧 의도치 않은 오버나이트 캐리다 → 더 공격적인 한도.
        sell_px = marketable_limit_price("sell", cur_price, _eod_buffer_pct())
        print(f"  [US 마감청산] {symbol} {qty}주 @ 한도 ${sell_px:.2f} "
              f"(현재가 ${cur_price:.2f}) — {why}")
        if not dry_run:
            _sell_and_record(client, symbol, exchange, qty, cur_price, sell_px,
                             "매도: 미국장 마감 청산", eod=True)
        else:
            print("      (dry-run)")
            remove_us_position(symbol)


# ══════════════════════════════════════════════════════════════════════
# 미장 방향성 모멘텀 스캘프 (KR 조간 엔진의 US판)
#   나스닥(QQQM 기준) 상승 → QQQM 롱 / 하락 → PSQ 숏(1x 인버스).
#   순수 함수(morning_momentum_signal/should_exit_morning/can_reenter) 재사용.
#   자정을 넘는 US 세션이라 시간청산은 문자열 비교 대신 마감 잔여분으로 판정.
# ══════════════════════════════════════════════════════════════════════

US_MOM_POSITIONS_PATH = Path("logs/us_momentum_positions.json")
OVERRIDES_PATH = Path("configs/user_overrides.yaml")


def load_us_momentum_config() -> dict:
    """us_momentum 설정 — strategy.yaml + user_overrides 오버레이(자동클로버 방지)."""
    base: dict = {}
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            base = (yaml.safe_load(f) or {}).get("us_momentum", {}) or {}
    except Exception:
        base = {}
    try:
        if OVERRIDES_PATH.exists():
            with OVERRIDES_PATH.open(encoding="utf-8") as f:
                ov = (yaml.safe_load(f) or {}).get("us_momentum")
            if isinstance(ov, dict):
                base = {**base, **ov}
    except Exception:
        pass
    return base


def _load_us_mom() -> dict:
    try:
        if US_MOM_POSITIONS_PATH.exists():
            return json.loads(US_MOM_POSITIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_us_mom(d: dict) -> None:
    try:
        US_MOM_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        US_MOM_POSITIONS_PATH.write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("us_mom_save_failed", error=str(e))


def _us_quote(client: KISClient, symbol: str, exchange: str) -> tuple[float, float]:
    """(전일종가 base, 현재가 last) — KIS 해외 현재가. 실패 시 (0,0)."""
    try:
        resp = client.get_overseas_price(symbol, exchange=exchange)
        if resp.get("rt_cd") == "0":
            o = resp.get("output", {}) or {}
            base = float(str(o.get("base", 0) or 0).replace(",", ""))
            last = float(str(o.get("last", 0) or 0).replace(",", ""))
            return base, last
    except Exception:
        pass
    return 0.0, 0.0


def _us_current_regime() -> str | None:
    """best-effort 레짐(bear_state.json). 역레짐 롱 금지에 사용. 없으면 None."""
    try:
        bs = json.loads(Path("logs/bear_state.json").read_text(encoding="utf-8"))
        return bs.get("regime")
    except Exception:
        return None


def _to_us_session_row(trade: dict) -> dict:
    """거래 기록의 KST 타임스탬프를 **US 거래일(ET)** 로 바꾼 사본을 반환.

    US 마감청산은 항상 KST 자정 이후에 찍히므로, KST 날짜 그대로 비교하면 같은
    세션의 청산이 하루 뒤 날짜로 잡혀 쿨다운이 한 세션 더 길어진다.
    """
    raw = trade.get("timestamp") or trade.get("date") or ""
    try:
        session = us_session_date_et(parse_kst(raw)).isoformat()
    except Exception:  # noqa: BLE001
        return trade
    return {**trade, "date": session, "timestamp": session}


def _minutes_until_us_close(now_: datetime, close_str: str) -> float:
    """폐장까지 남은 분(KST). 자정 넘는 폐장(예 05:00)은 다음날로 보정."""
    try:
        ch, cm = int(close_str[:2]), int(close_str[3:5])
    except Exception:
        ch, cm = 5, 0
    close_dt = now_.replace(hour=ch, minute=cm, second=0, microsecond=0)
    if ch < 12 and now_.hour >= 12:
        close_dt += timedelta(days=1)
    return (close_dt - now_).total_seconds() / 60.0


def _minutes_since_us_open(now_: datetime, open_str: str) -> float:
    """US 개장 후 경과 분(KST). 자정 넘는 세션이라 개장시각보다 이른 새벽이면 개장은 전날.

    진입창을 문자열 시각("01:00" ∉ "22:30~23:59")으로 판정하던 게 버그 —
    자정 이후엔 창이 영영 안 열려 US 모멘텀이 전혀 진입 못 했다. 이 경과분으로 대체.
    """
    try:
        oh, om = int(open_str[:2]), int(open_str[3:5])
    except Exception:
        oh, om = 22, 30
    open_dt = now_.replace(hour=oh, minute=om, second=0, microsecond=0)
    if now_ < open_dt:                      # 01:00 < 22:30 → 개장은 전날 밤
        open_dt -= timedelta(days=1)
    return (now_ - open_dt).total_seconds() / 60.0


def run_us_momentum_strategy(client: KISClient, dry_run: bool) -> bool:
    """미장 방향성 스캘프 1틱. 상승→QQQM 롱, 하락→PSQ 숏. 매수/매도 있으면 True.

    KR 조간과 동일 엔진. 진입은 개장 첫 90분(윈도), 청산은 트레일/본전/TP/SL + 세션말.
    """
    cfg = load_us_momentum_config()
    if not cfg.get("enabled", False):
        return False
    if not is_us_market_hours():
        return False

    long_sym = str(cfg.get("long_symbol", "QQQM"))
    long_exch = str(cfg.get("long_exchange", "NASD"))
    inv_sym = str(cfg.get("inverse_symbol", "PSQ"))
    inv_exch = str(cfg.get("inverse_exchange", "AMEX"))
    now = now_kst()
    # 개장/폐장은 ET 기준 자동 계산 — yaml의 market_open_kst/market_close_kst는
    # DST 전환 때 같이 썩는 하드코딩이라 더 이상 읽지 않는다.
    open_t, close_t = get_us_market_times(now)
    open_str = open_t.strftime("%H:%M")
    close_str = close_t.strftime("%H:%M")

    now_hhmm = now.strftime("%H:%M")
    # 세션 상태는 KST 날짜가 아니라 US 거래일(ET)로 키잉 — US 세션은 KST 자정을
    # 가로지르므로 KST 날짜로 잡으면 세션 도중에 상태가 리셋된다.
    today = us_session_date_et(now).isoformat()

    state = _load_us_mom()
    meta = state.get("_meta", {})
    if meta.get("date") != today:
        meta = {"cycles": 0, "last_exit_hhmm": None, "date": today, "session_open": {}}
        state = {"_meta": meta}
    session_open = meta.setdefault("session_open", {})

    mins_left = _minutes_until_us_close(now, close_str)
    force_eod = mins_left <= float(cfg.get("session_exit_min_before", 15))

    # 진입창: 개장 후 N분 이내(개장경과분 기준 — 자정 넘김/루프지연에도 정상 동작)
    mins_since_open = _minutes_since_us_open(now, open_str)
    entry_window_min = float(cfg.get("entry_window_min", 180))
    in_entry_window = 0 <= mins_since_open <= entry_window_min

    acted = False
    holdings_api = get_us_holdings(client)

    # ── 1) 청산 (보유 모멘텀 포지션) ──
    for sym in [k for k in state.keys() if k != "_meta"]:
        pos = state[sym]
        exch = pos.get("exchange", long_exch if sym == long_sym else inv_exch)
        held = int(holdings_api.get(sym, {}).get("qty", 0) or 0)
        if held <= 0:
            # 외부 청산됨(마감청산/수동 등) → 상태 정리 + 사이클 계상 + 쿨다운
            print(f"  [US-MOM] {sym} 외부 청산 감지 — 상태 정리")
            del state[sym]
            meta["cycles"] = int(meta.get("cycles", 0)) + 1
            meta["last_exit_hhmm"] = now_hhmm
            continue
        _, cur = _us_quote(client, sym, exch)
        if cur <= 0:
            continue
        entry = float(pos["entry"])
        peak = max(float(pos.get("peak", entry)), cur)
        pos["peak"] = peak
        direction = pos.get("direction", "long")
        do_exit, why = should_exit_morning(
            entry_price=entry, cur_price=cur, direction=direction,
            now_hhmm=now_hhmm, cfg=cfg, peak_price=peak)
        if force_eod and not do_exit:
            do_exit = True
            why = (f"세션말 강제청산 (마감 {mins_left:.0f}분 전, "
                   f"손익 {(cur - entry) / entry * 100:+.2f}%)")
        if not do_exit:
            state[sym] = pos  # peak 갱신 보존
            continue
        qty = int(pos["qty"])
        print(f"  [US-MOM] {sym} 청산: {why}")
        if not dry_run:
            # 세션말 강제청산은 미체결이 곧 오버나이트 캐리 → 더 공격적인 한도
            _buf = _eod_buffer_pct() if force_eod else None
            _px = marketable_limit_price("sell", cur, _buf)
            if _sell_and_record(client, sym, exch, qty, cur, _px,
                                f"US모멘텀 청산: {why}", eod=force_eod):
                del state[sym]
                meta["cycles"] = int(meta.get("cycles", 0)) + 1
                meta["last_exit_hhmm"] = now_hhmm
                acted = True
            else:
                state[sym] = pos      # 미체결/거부 → 계속 관리
        else:
            print("      (dry-run 매도)")
            del state[sym]
            meta["cycles"] = int(meta.get("cycles", 0)) + 1
            meta["last_exit_hhmm"] = now_hhmm
            acted = True

    # ── 2) 진입 (플랫 + 재진입 가능 + 세션말 아님) ──
    holding_now = [k for k in state.keys() if k != "_meta"]
    if not holding_now and not force_eod and not in_entry_window:
        print(f"  [US-MOM] 진입창 밖 (개장 후 {mins_since_open:.0f}분 > {entry_window_min:.0f}분)")
    elif not holding_now and not force_eod:
        ok, why = can_reenter(meta=meta, now_hhmm=now_hhmm, cfg=cfg)
        if not ok:
            print(f"  [US-MOM] 재진입 보류: {why}")
        else:
            prev_close, cur = _us_quote(client, long_sym, long_exch)
            so = session_open.get(long_sym)
            if not so and cur > 0:
                session_open[long_sym] = cur
                so = cur
            today_open = so or cur
            # 신호 내부의 문자열 윈도 체크는 무력화(개장경과분으로 이미 판정) — 자정 넘김 버그 회피
            sig_cfg = {**cfg, "window_start_kst": "00:00", "entry_end_kst": "23:59"}
            sig = morning_momentum_signal(
                prev_close=prev_close, today_open=today_open, cur_price=cur,
                now_hhmm=now_hhmm, cfg=sig_cfg, regime=_us_current_regime())
            print(f"  [US-MOM] 판단: {sig.direction} — {sig.reason}")
            if sig.is_entry:
                if sig.direction == "long":
                    tsym, texch, atype, tdir = long_sym, long_exch, "us_mom_long", "long"
                else:
                    tsym, texch, atype, tdir = inv_sym, inv_exch, "us_mom_inverse", "inverse"
                _, tprice = _us_quote(client, tsym, texch)
                if tprice > 0:
                    avail = get_us_available_cash(client)
                    pos_usd = min(float(cfg.get("position_usd", 300)), avail)
                    tbuy_px = marketable_limit_price("buy", tprice)
                    qty = int(pos_usd // tbuy_px)
                    if qty < 1 and avail >= tbuy_px:
                        qty = 1  # 최소 1주 (가용이 1주는 감당할 때)
                    if qty >= 1:
                        cost = qty * tbuy_px
                        print(f"    [US-MOM BUY] {tsym} {qty}주 @ 한도 ${tbuy_px:.2f} "
                              f"(현재가 ${tprice:.2f}) ≤ ${cost:.2f} ({tdir})")
                        if not dry_run:
                            _qb, _ = _held_qty(client, tsym)
                            resp = client.order_overseas(tsym, qty, price=tbuy_px,
                                                         side="buy", exchange=texch,
                                                         order_type="00")
                            if resp.get("rt_cd") == "0":
                                # 접수 != 체결. 실체결 수량·평단으로 기록해야
                                # 트레일/본전 기준과 저널 손익이 맞는다.
                                _f, _avg = confirm_us_fill(client, tsym, "buy",
                                                           max(0, _qb), qty)
                                if _f == 0:
                                    print("      ⚠️ 미체결 — 진입 취소(다음 틱 재시도)")
                                    log.warning("us_mom_buy_unfilled", symbol=tsym,
                                                qty=qty, limit=tbuy_px)
                                    _f = 0
                                else:
                                    if _f < 0:
                                        _f, _avg = qty, tbuy_px
                                    elif _avg <= 0:
                                        _avg = tbuy_px
                                    print(f"      체결: {_f}주 @ ${_avg:.2f} "
                                          f"(슬리피지 {(_avg - tprice) / tprice:+.2%})")
                                    log_trade(tsym, tsym, "buy", _f, int(_avg * 100),
                                              market="US",
                                              reason=f"US모멘텀 {tdir}: {sig.reason}")
                                    record_us_buy(tsym, _avg, _f, texch, atype)
                                    state[tsym] = {"entry": _avg, "qty": _f,
                                                   "peak": _avg, "direction": tdir,
                                                   "exchange": texch}
                                    acted = True
                            else:
                                print(f"      매수 실패 rt_cd={resp.get('rt_cd')} "
                                      f"{resp.get('msg1', '')}")
                        else:
                            print("      (dry-run 매수)")
                            state[tsym] = {"entry": tprice, "qty": qty,
                                           "peak": tprice, "direction": tdir,
                                           "exchange": texch}
                            acted = True
                    else:
                        print(f"    [US-MOM] 예산 부족: 가용 ${avail:.2f}, "
                              f"{tsym} ${tprice:.2f}")

    state["_meta"] = meta
    _save_us_mom(state)
    return acted
