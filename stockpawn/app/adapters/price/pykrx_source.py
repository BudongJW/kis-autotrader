"""pykrx 시세 소스 (MVP).

주의 — pykrx >= 1.2는 KRX 정보데이터시스템 로그인(KRX_ID/KRX_PW 환경변수)이 있어야
시가총액·종목목록 등 KRX 엔드포인트가 동작한다. 로그인이 없으면:
  - 일봉은 네이버 소스(수정주가)로 받되 거래대금 컬럼이 없어 종가×거래량으로 근사
  - 시가총액·시장구분은 미상 → 평가 엔진이 최고 C등급으로만 인정
관리종목·정리매매 여부는 pykrx로 알 수 없다 → 운영자 차단(Stock.is_blocked) 또는 KIS 소스 사용.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.adapters.price.base import DailyBar, PriceSource, PriceSourceError, StockInfo

log = logging.getLogger(__name__)

_FMT = "%Y%m%d"


def _dec(value: Any) -> Decimal:
    """pandas/numpy 스칼라 → Decimal. float 경유를 피하려 정수로 먼저 변환(원·주 단위는 정수)."""
    return Decimal(int(value))


class PykrxSource(PriceSource):
    def __init__(self, stock_module: Any | None = None) -> None:
        # 테스트에서는 가짜 모듈을 주입한다 (네트워크 금지)
        self._stock_module = stock_module
        self._ticker_lists: dict[tuple[str, str], frozenset[str]] = {}

    @property
    def _stock(self) -> Any:
        if self._stock_module is None:
            from pykrx import stock

            self._stock_module = stock
        return self._stock_module

    def get_daily_bars(self, code: str, end: date, count: int) -> list[DailyBar]:
        # 거래일 count개를 확보할 만큼 넉넉한 달력일 구간 (주말·공휴일 감안 ×1.6 + 여유)
        start = end - timedelta(days=int(count * 1.6) + 20)
        try:
            df = self._stock.get_market_ohlcv(start.strftime(_FMT), end.strftime(_FMT), code)
        except Exception as e:  # noqa: BLE001
            raise PriceSourceError(f"{code}: pykrx 일봉 조회 실패 — {e}") from e
        if df is None or df.empty:
            return []
        has_value = "거래대금" in df.columns
        bars = []
        for ts, row in df.iterrows():
            close = _dec(row["종가"])
            if close <= 0:
                continue
            volume = _dec(row["거래량"])
            value = _dec(row["거래대금"]) if has_value else close * volume
            bars.append(DailyBar(ts.date(), close, volume, value))
        return bars[-count:]

    def get_stock_info(self, code: str, as_of: date) -> StockInfo:
        try:
            name = self._stock.get_market_ticker_name(code)
        except Exception as e:  # noqa: BLE001
            raise PriceSourceError(f"{code}: 종목명 조회 실패 — {e}") from e
        if not isinstance(name, str) or not name:
            raise PriceSourceError(f"{code}: 상장 종목이 아닙니다")

        notes: list[str] = ["관리종목·정리매매 여부는 pykrx로 확인 불가(운영자 차단 플래그로 관리)"]
        market = self._market_of(code, as_of)
        market_cap = self._market_cap(code, as_of)
        if market_cap is None:
            notes.append("시가총액 조회 실패(KRX 로그인 필요)")

        bars = self.get_daily_bars(code, as_of, 1)
        halted = not bars or bars[-1].volume == 0
        return StockInfo(
            code=code,
            name=name,
            market=market,
            market_cap=market_cap,
            is_halted=halted,
            is_spac="스팩" in name,
            notes=tuple(notes),
        )

    def _market_of(self, code: str, as_of: date) -> str:
        for market in ("KOSPI", "KOSDAQ", "KONEX"):
            key = (as_of.strftime(_FMT), market)
            if key not in self._ticker_lists:
                try:
                    tickers = self._stock.get_market_ticker_list(key[0], market=market)
                except Exception as e:  # noqa: BLE001
                    log.warning("pykrx_ticker_list_failed market=%s error=%s", market, e)
                    tickers = []
                self._ticker_lists[key] = frozenset(tickers or [])
            if code in self._ticker_lists[key]:
                return market
        return "UNKNOWN"

    def _market_cap(self, code: str, as_of: date) -> Decimal | None:
        start = as_of - timedelta(days=14)
        try:
            df = self._stock.get_market_cap(start.strftime(_FMT), as_of.strftime(_FMT), code)
        except Exception as e:  # noqa: BLE001
            log.warning("pykrx_market_cap_failed code=%s error=%s", code, e)
            return None
        if df is None or df.empty or "시가총액" not in df.columns:
            return None
        cap = _dec(df["시가총액"].iloc[-1])
        return cap if cap > 0 else None
