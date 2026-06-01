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
TR_INQUIRE_DAILY = "FHKST01010400"  # 일별 시세 (최근 30거래일, 공통)
TR_INQUIRE_DAILY_CHART = "FHKST03010100"  # 기간별 일/주/월 차트 (최대 100건, 공통)
TR_INQUIRE_ASKING = "FHKST01010200"  # 호가/예상체결 (10단계 매수·매도 잔량, 공통)
TR_ORDER_CASH_LIVE_BUY = "TTTC0802U"   # 실전 현금 매수
TR_ORDER_CASH_LIVE_SELL = "TTTC0801U"  # 실전 현금 매도
TR_ORDER_CASH_PAPER_BUY = "VTTC0802U"  # 모의 현금 매수
TR_ORDER_CASH_PAPER_SELL = "VTTC0801U" # 모의 현금 매도
TR_INQUIRE_BALANCE_LIVE = "TTTC8434R"
TR_INQUIRE_BALANCE_PAPER = "VTTC8434R"
TR_INQUIRE_PSBL_ORDER_LIVE = "TTTC8908R"   # 국내 매수가능조회 (실전)
TR_INQUIRE_PSBL_ORDER_PAPER = "VTTC8908R"  # 국내 매수가능조회 (모의)
TR_DAILY_CCLD_LIVE = "TTTC8001R"           # 일별 체결·미체결 조회 (실전, 3개월 이내)
TR_DAILY_CCLD_PAPER = "VTTC8001R"          # 일별 체결·미체결 조회 (모의)

# ── 해외주식 TR_ID ──
TR_OS_PRICE = "HHDFS00000300"             # 해외주식 현재가
TR_OS_DAILY = "HHDFS76240000"             # 해외주식 일별 시세
TR_OS_ORDER_BUY_LIVE = "TTTT1002U"        # 실전 해외주식 매수 (was JTTT — KIS 공식 sample은 TTTT)
TR_OS_ORDER_SELL_LIVE = "TTTT1006U"       # 실전 해외주식 매도
TR_OS_ORDER_BUY_PAPER = "VTTT1002U"       # 모의 해외주식 매수
TR_OS_ORDER_SELL_PAPER = "VTTT1006U"      # 모의 해외주식 매도
TR_OS_BALANCE_LIVE = "JTTT3012R"          # 실전 해외주식 잔고
TR_OS_BALANCE_PAPER = "VTTS3012R"         # 모의 해외주식 잔고
TR_OS_PSAMOUNT_LIVE = "TTTS3007R"         # 실전 해외주식 매수가능금액 조회 (통합증거금 반영)
TR_OS_PSAMOUNT_PAPER = "VTTS3007R"        # 모의 해외주식 매수가능금액 조회

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

# 시세 endpoint(HHDFS*)는 3글자 코드를 요구. 잔고·주문 endpoint(JTTT*)는 4글자.
# debug 결과 (5-27): NASD로 호출 시 일봉 endpoint가 silent fail로 빈 응답 반환.
EXCHANGE_QUOTE_MAP = {
    "NASD": "NAS",   # NASD → NAS (시세 endpoint용)
    "NYSE": "NYS",
    "AMEX": "AMS",
    # 이미 3글자면 그대로 패스
    "NAS": "NAS",
    "NYS": "NYS",
    "AMS": "AMS",
}


def _to_quote_excd(exchange: str) -> str:
    """시세 endpoint용 3글자 거래소 코드로 정규화."""
    return EXCHANGE_QUOTE_MAP.get(exchange, exchange)


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
        """국내 주식 일/주/월별 시세 — 최근 30거래일만 반환.

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

    def get_orderbook(self, symbol: str) -> dict:
        """호가/예상체결 — 10단계 매수·매도 잔량 + 예상 체결가.

        주요 응답 필드 (output1):
          - total_askp_rsqn: 매도호가 총 잔량
          - total_bidp_rsqn: 매수호가 총 잔량
          - askp_rsqn1~10: 매도 1~10단계 잔량
          - bidp_rsqn1~10: 매수 1~10단계 잔량
          - askp1~10: 매도 1~10단계 호가
          - bidp1~10: 매수 1~10단계 호가
        """
        return self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            tr_id=TR_INQUIRE_ASKING,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            },
        )

    def get_daily_itemchartprice(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        period: str = "D",
        adj: str = "0",
    ) -> dict:
        """기간 지정 일/주/월/년 차트 — 한 번에 최대 100건 반환.

        Args:
            symbol: 6자리 종목코드
            start_date: 시작일 YYYYMMDD
            end_date: 종료일 YYYYMMDD
            period: D=일, W=주, M=월, Y=년
            adj: 0=수정주가 미반영, 1=반영
        """
        return self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id=TR_INQUIRE_DAILY_CHART,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
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

        # 시장가(01)는 KIS가 ORD_UNPR="0"을 요구한다.
        # 0이 아닌 단가를 보내면 rt_cd=7 "주문단가를 0으로 입력하세요"로 전량 거부됨
        # (호출부가 사이징/안전체크용 price를 넘기는데, 시장가엔 단가를 실으면 안 됨).
        # 지정가(00)만 단가를 싣고, 원화 단가는 정수여야 하므로 int로 정규화.
        ord_unpr = "0" if order_type == "01" else str(int(round(price)))
        body = {
            "CANO": settings.kis_account_no,
            "ACNT_PRDT_CD": settings.kis_account_prod_code,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": ord_unpr,
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
            exchange: NASD / NYSE / AMEX (자동으로 3글자로 변환)
        """
        # 시세 endpoint는 3글자 코드 요구 (NAS/NYS/AMS). 4글자는 silent fail.
        excd = _to_quote_excd(exchange)
        return self._get(
            "/uapi/overseas-price/v1/quotations/price",
            tr_id=TR_OS_PRICE,
            params={
                "AUTH": "",
                "EXCD": excd,
                "SYMB": symbol,
            },
        )

    def get_overseas_daily_price(self, symbol: str, exchange: str = "NASD",
                                  period: str = "D", adj: str = "0",
                                  count: str = "120") -> dict:
        """해외주식 일별 시세.

        Args:
            symbol: 해외 종목 티커
            exchange: NASD / NYSE / AMEX (자동으로 3글자로 변환)
            period: D=일, W=주, M=월
            count: 요청 건수 (최대 120)
        """
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        # 시세 endpoint는 3글자 코드 요구. 4글자(NASD)는 rt_cd=0이지만 빈 데이터.
        excd = _to_quote_excd(exchange)
        # BYMD(조회 기준일)는 미국 동부 기준 오늘로 설정.
        # KST date.today()는 한국 야간 세션 동안 미국보다 하루 앞서가서
        # KIS가 "미래 일자"로 인식 → rt_cd=0인데 output2 빈 데이터(nrec=0) 반환.
        bymd = _dt.now(_ZI("America/New_York")).strftime("%Y%m%d")
        return self._get(
            "/uapi/overseas-price/v1/quotations/dailyprice",
            tr_id=TR_OS_DAILY,
            params={
                "AUTH": "",
                "EXCD": excd,
                "SYMB": symbol,
                "GUBN": "0",     # 0=일, 1=주, 2=월
                "BYMD": bymd,
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

    def get_overseas_psamount(
        self,
        symbol: str,
        price: float,
        exchange: str = "NASD",
    ) -> dict:
        """해외주식 매수가능금액 조회 — 통합증거금 적용 후 USD 가용 금액 포함.

        Response (output)에 포함되는 핵심 필드:
          - frcr_ord_psbl_amt1: 외화 주문가능금액 (USD 잔고만)
          - echm_af_ord_psbl_amt: 환전이후 주문가능금액 (KRW 통합증거금 환산 포함)
          - echm_af_ord_psbl_qty: 환전이후 주문가능수량

        통합증거금이 활성화된 계좌면 echm_af_ord_psbl_amt가 frcr_ord_psbl_amt1보다 큼.
        """
        tr_id = TR_OS_PSAMOUNT_LIVE if settings.is_live else TR_OS_PSAMOUNT_PAPER
        return self._get(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            tr_id=tr_id,
            params={
                "CANO": settings.kis_account_no,
                "ACNT_PRDT_CD": settings.kis_account_prod_code,
                "OVRS_EXCG_CD": exchange,
                "OVRS_ORD_UNPR": f"{price:.4f}",
                "ITEM_CD": symbol,
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

    def inquire_daily_ccld(
        self,
        start_date: str,
        end_date: str,
        symbol: str = "",
        ccld_type: str = "00",  # 00=전체, 01=체결, 02=미체결
        side: str = "00",        # 00=전체, 01=매도, 02=매수
        sort: str = "00",        # 00=역순, 01=정순
    ) -> dict:
        """국내 주식 일별 체결·미체결 조회 — 3개월 이내.

        Args:
            start_date: YYYYMMDD
            end_date:   YYYYMMDD
            symbol:     특정 종목 필터 (빈 문자열 = 전체)
            ccld_type:  00=전체, 01=체결만, 02=미체결만
            side:       00=전체, 01=매도, 02=매수
        """
        tr_id = TR_DAILY_CCLD_LIVE if settings.is_live else TR_DAILY_CCLD_PAPER
        return self._get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id=tr_id,
            params={
                "CANO": settings.kis_account_no,
                "ACNT_PRDT_CD": settings.kis_account_prod_code,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": side,
                "INQR_DVSN": sort,
                "PDNO": symbol,
                "CCLD_DVSN": ccld_type,
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )

    def inquire_psbl_order(
        self,
        symbol: str,
        price: int,
        order_division: str = "00",
        include_overseas: str = "N",
    ) -> dict:
        """국내 주식 매수가능조회 — 통합증거금 영향 진단용.

        Args:
            symbol: 종목코드
            price: 주문 단가
            order_division: 00=지정가, 01=시장가
            include_overseas: Y/N — 해외 통합증거금 포함 여부

        주요 응답 필드:
            ord_psbl_cash: 주문 가능 현금
            nrcvb_buy_amt: 미수동 매수금액
            nrcvb_buy_qty: 미수동 매수수량
            max_buy_amt: 최대 매수금액 (신용 포함)
            max_buy_qty: 최대 매수수량
            cma_evlu_amt: CMA 평가금액
            ovrs_re_use_amt_wcrc: 해외 재사용 가능금액
        """
        tr_id = TR_INQUIRE_PSBL_ORDER_LIVE if settings.is_live else TR_INQUIRE_PSBL_ORDER_PAPER
        return self._get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=tr_id,
            params={
                "CANO": settings.kis_account_no,
                "ACNT_PRDT_CD": settings.kis_account_prod_code,
                "PDNO": symbol,
                "ORD_UNPR": str(price),
                "ORD_DVSN": order_division,
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": include_overseas,
            },
        )
