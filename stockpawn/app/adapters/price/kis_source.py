"""KIS OpenAPI 시세 소스 — stub (운영 전환 시 구현).

같은 저장소의 kis-autotrader(`src/kis_client.py`)가 이미 아래 TR을 감싸고 있으므로
운영 전환 시 그 클라이언트를 주입해 매핑만 채우면 된다:

  - 현재가  FHKST01010100 `inquire-price` (KISClient.get_price)
      stck_prpr(현재가), hts_avls(시가총액, 억원), temp_stop_yn(임시정지),
      iscd_stat_cls_code(종목상태: 51 관리종목, 52 투자위험, 58 거래정지 …)
  - 기간 일봉 FHKST03010100 `inquire-daily-itemchartprice` (KISClient.get_daily_itemchartprice)
      stck_clpr(종가), acml_vol(거래량), acml_tr_pbmn(거래대금) — 1회 최대 100건

실시간 시세 라이선스·호출 한도(실전 초당 20건)는 운영 전 별도 확인.
"""

from __future__ import annotations

from datetime import date

from app.adapters.price.base import DailyBar, PriceSource, StockInfo


class KISSource(PriceSource):
    def get_daily_bars(self, code: str, end: date, count: int) -> list[DailyBar]:
        raise NotImplementedError("KISSource는 아직 stub입니다 — PRICE_SOURCE=pykrx를 쓰세요")

    def get_stock_info(self, code: str, as_of: date) -> StockInfo:
        raise NotImplementedError("KISSource는 아직 stub입니다 — PRICE_SOURCE=pykrx를 쓰세요")
