"""KIS REST API 클라이언트 — 시세·주문·잔고 래퍼.

공식 문서: https://apiportal.koreainvestment.com/apiservice
"""

from __future__ import annotations

from typing import Any

import requests

from src.config import settings
from src.kis_auth import auth_headers
from src.utils.rate_limit import rate_limiter

# 모의/실전 공통 TR_ID. 모의는 V로 시작하는 경우가 많아 분기 필요.
TR_INQUIRE_PRICE = "FHKST01010100"  # 현재가 시세 (공통)
TR_INQUIRE_DAILY = "FHKST01010400"  # 일별 시세 (공통)
TR_ORDER_CASH_LIVE_BUY = "TTTC0802U"   # 실전 현금 매수
TR_ORDER_CASH_LIVE_SELL = "TTTC0801U"  # 실전 현금 매도
TR_ORDER_CASH_PAPER_BUY = "VTTC0802U"  # 모의 현금 매수
TR_ORDER_CASH_PAPER_SELL = "VTTC0801U" # 모의 현금 매도
TR_INQUIRE_BALANCE_LIVE = "TTTC8434R"
TR_INQUIRE_BALANCE_PAPER = "VTTC8434R"

# ── 해외주식 TR_ID ──
TR_OS_PRICE = "HHDFS00000300"             # 해외주식 현재가
TR_OS_DAILY = "HHDFS76240000"             # 해외주식 일별 시세
TR_OS_ORDER_BUY_LIVE = "JTTT1002U"        # 실전 해외주식 매수
TR_OS_ORDER_SELL_LIVE = "JTTT1006U"       # 실전 해외주식 매도
TR_OS_ORDER_BUY_PAPER = "VTTT1002U"       # 모의 해외주식 매수
TR_OS_ORDER_SELL_PAPER = "VTTT1006U"      # 모의 해외주식 매도
TR_OS_BALANCE_LIVE = "JTTT3012R"          # 실전 해외주식 잔고
TR_OS_BALANCE_PAPER = "VTTS3012R"         # 모의 해외주식 잔고

# 거래소 코드
EXCHANGE_MAP = {
    "NAS": "NASD",   # 나스닥
    "NYS": "NYSE",   # 뉴욕거래소
    "AMS": "AMEX",   # 아멕스
}
EXCHANGE_ORDER_MAP = {
    "NASD": "NASD",
    "NYSE": "NYSE",
    "AMEX": "AMEX",
}


class KISClient:
    """KIS REST API 호출 래퍼. rate limit 자동 적용."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url or settings.base_url

    def _get(self, path: str, *, tr_id: str, params: dict[str, Any]) -> dict:
        rate_limiter.acquire()
        url = f"{self.base_url}{path}"
        resp = requests.get(url, headers=auth_headers(tr_id), params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, *, tr_id: str, body: dict[str, Any]) -> dict:
        rate_limiter.acquire()
        url = f"{self.base_url}{path}"
        resp = requests.post(url, headers=auth_headers(tr_id), json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _safe_post(self, path: str, *, tr_id: str, body: dict[str, Any]) -> dict:
        """HTTP 에러를 잡아서 rt_cd='E'로 반환. 루프 크래시 방지."""
        try:
            return self._post(path, tr_id=tr_id, body=body)
        except requests.exceptions.HTTPError as e:
            return {"rt_cd": "E", "msg1": f"HTTP 에러: {e}", "msg_cd": "HTTP_ERR"}
        except requests.exceptions.RequestException as e:
            return {"rt_cd": "E", "msg1": f"요청 실패: {e}", "msg_cd": "REQ_ERR"}

    # ------------------------------------------------------------------
    # 시세
    # ------------------------------------------------------------------
    def get_price(self, symbol: str) -> dict:
        """국내 주식 현재가 조회.

        Args:
            symbol: 6자리 종목코드 (예: "005930" = 삼성전자)
        """
        return self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id=TR_INQUIRE_PRICE,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",  # J=주식
                "FID_INPUT_ISCD": symbol,
            },
        )

    def get_daily_price(self, symbol: str, period: str = "D", adj: str = "0") -> dict:
        """국내 주식 일/주/월별 시세.

        Args:
            symbol: 종목코드
            period: D=일, W=주, M=월
            adj: 0=수정주가 미반영, 1=반영
        """
        return self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            tr_id=TR_INQUIRE_DAILY,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": adj,
            },
        )

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    def order_cash(
        self,
        symbol: str,
        qty: int,
        price: int = 0,
        *,
        side: str,
        order_type: str = "01",  # 01=시장가, 00=지정가
    ) -> dict:
        """현금 주문 (매수/매도).

        Args:
            symbol: 종목코드
            qty: 주문 수량
            price: 지정가 가격 (시장가 시 0)
            side: "buy" 또는 "sell"
            order_type: 00=지정가, 01=시장가

        15:20 이후에는 시장가 주문이 거부되므로 자동으로 지정가 전환.
        """
        from datetime import datetime, time as dtime

        if side == "buy":
            tr_id = TR_ORDER_CASH_LIVE_BUY if settings.is_live else TR_ORDER_CASH_PAPER_BUY
        elif side == "sell":
            tr_id = TR_ORDER_CASH_LIVE_SELL if settings.is_live else TR_ORDER_CASH_PAPER_SELL
        else:
            raise ValueError(f"side는 'buy' 또는 'sell'이어야 함: {side}")

        # 15:20 이후 시장가 → 지정가 자동 전환
        now_time = datetime.now().time()
        if order_type == "01" and now_time >= dtime(15, 20):
            if price <= 0:
                # 현재가 조회해서 지정가로 전환
                try:
                    resp = self.get_price(symbol)
                    if resp.get("rt_cd") == "0":
                        price = int(resp["output"]["stck_prpr"])
                except Exception:
                    pass
            if price > 0:
                order_type = "00"
                print(f"  [주문] 15:20 이후 → 지정가 전환 ({symbol} @ {price:,}원)")

        body = {
            "CANO": settings.kis_account_no,
            "ACNT_PRDT_CD": settings.kis_account_prod_code,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        return self._safe_post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            body=body,
        )

    # ------------------------------------------------------------------
    # 해외주식 시세
    # ------------------------------------------------------------------
    def get_overseas_price(self, symbol: str, exchange: str = "NASD") -> dict:
        """해외주식 현재가 조회.

        Args:
            symbol: 해외 종목 티커 (예: "AAPL", "QQQ")
            exchange: NASD / NYSE / AMEX
        """
        return self._get(
            "/uapi/overseas-price/v1/quotations/price",
            tr_id=TR_OS_PRICE,
            params={
                "AUTH": "",
                "EXCD": exchange,
                "SYMB": symbol,
            },
        )

    def get_overseas_daily_price(self, symbol: str, exchange: str = "NASD",
                                  period: str = "D", adj: str = "0",
                                  count: str = "120") -> dict:
        """해외주식 일별 시세.

        Args:
            symbol: 해외 종목 티커
            exchange: NASD / NYSE / AMEX
            period: D=일, W=주, M=월
            count: 요청 건수 (최대 120)
        """
        return self._get(
            "/uapi/overseas-price/v1/quotations/dailyprice",
            tr_id=TR_OS_DAILY,
            params={
                "AUTH": "",
                "EXCD": exchange,
                "SYMB": symbol,
                "GUBN": "0",     # 0=일, 1=주, 2=월
                "BYMD": "",      # 빈 문자열이면 최근부터
                "MODP": "1",     # 1=수정주가 반영
                "KEYB": "",
            },
        )

    # ------------------------------------------------------------------
    # 해외주식 주문
    # ------------------------------------------------------------------
    def order_overseas(
        self,
        symbol: str,
        qty: int,
        price: float = 0,
        *,
        side: str,
        exchange: str = "NASD",
        order_type: str = "00",  # 00=지정가, 32=시장가(MOC)
    ) -> dict:
        """해외주식 현금 주문 (매수/매도).

        Args:
            symbol: 해외 티커 (예: "QQQ")
            qty: 주문 수량
            price: 주문 가격 (USD). 시장가 시 0.
            side: "buy" / "sell"
            exchange: NASD / NYSE / AMEX
            order_type: "00"=지정가, "32"=MOC(장마감시장가)
        """
        if side == "buy":
            tr_id = TR_OS_ORDER_BUY_LIVE if settings.is_live else TR_OS_ORDER_BUY_PAPER
        elif side == "sell":
            tr_id = TR_OS_ORDER_SELL_LIVE if settings.is_live else TR_OS_ORDER_SELL_PAPER
        else:
            raise ValueError(f"side는 'buy' 또는 'sell'이어야 함: {side}")

        body = {
            "CANO": settings.kis_account_no,
            "ACNT_PRDT_CD": settings.kis_account_prod_code,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}" if price > 0 else "0",
            "ORD_SVR_DVSN_CD": "0",
            "CTAC_TLNO": "",
        }
        return self._safe_post(
            "/uapi/overseas-stock/v1/trading/order",
            tr_id=tr_id,
            body=body,
        )

    # ------------------------------------------------------------------
    # 해외주식 잔고
    # ------------------------------------------------------------------
    def get_overseas_balance(self, exchange: str = "NASD") -> dict:
        """해외주식 잔고 조회."""
        tr_id = TR_OS_BALANCE_LIVE if settings.is_live else TR_OS_BALANCE_PAPER
        return self._get(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params={
                "CANO": settings.kis_account_no,
                "ACNT_PRDT_CD": settings.kis_account_prod_code,
                "OVRS_EXCG_CD": exchange,
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )

    # ------------------------------------------------------------------
    # 잔고 (국내)
    # ------------------------------------------------------------------
    def get_balance(self) -> dict:
        """주식 잔고 조회."""
        tr_id = TR_INQUIRE_BALANCE_LIVE if settings.is_live else TR_INQUIRE_BALANCE_PAPER
        return self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params={
                "CANO": settings.kis_account_no,
                "ACNT_PRDT_CD": settings.kis_account_prod_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
