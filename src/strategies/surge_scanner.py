"""급등주 스크리너 — KIS API 등락률 순위 기반.

KIS API `/ranking/fluctuation` (tr_id: FHPST01700000) 엔드포인트로
당일 급등 종목을 실시간 스크리닝.

스크리닝 조건:
  1. 가격 상승: 등락률 +3% 이상
  2. 거래량 필터: 최소 10,000주 이상
  3. 가격 필터: 1,000원 ~ 100,000원
  4. ETF·우선주·스팩 등 제외

매매 규칙:
  - 장중 스크리닝 통과 종목 중 상위 1개 매수
  - 익일 시가 매도 (오버나이트 홀딩)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.kis_client import KISClient
from src.utils.logger import log


@dataclass
class SurgeCandidate:
    symbol: str
    name: str
    price: int
    change_pct: float     # 전일 대비 등락률
    volume: int           # 거래량
    volume_ratio: float   # 거래량 비율 (표시용, API에서 직접 제공 안 함)
    score: float          # 종합 점수 (등락률 기반)


# 제외할 키워드 (ETF, 스팩, 우선주 등)
EXCLUDE_KEYWORDS = [
    "KODEX", "TIGER", "KOSEF", "KBSTAR", "ARIRANG", "SOL", "HANARO",
    "ACE", "TIMEFOLIO", "PLUS", "스팩", "SPAC",
]


def scan_surge_candidates(client: KISClient | None = None) -> list[SurgeCandidate]:
    """KIS API로 당일 급등주 후보를 스캔.

    Args:
        client: KISClient 인스턴스. None이면 새로 생성.

    Returns:
        급등주 후보 리스트 (점수 내림차순)
    """
    if client is None:
        client = KISClient()

    candidates = []

    for market_code in ["0301", "0302"]:  # 코스피, 코스닥
        try:
            resp = client._get(
                "/uapi/domestic-stock/v1/ranking/fluctuation",
                tr_id="FHPST01700000",
                params={
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "20170",
                    "fid_input_iscd": market_code,
                    "fid_rank_sort_cls_code": "0",   # 상승률 순
                    "fid_input_cnt_1": "0",           # 전체
                    "fid_prc_cls_code": "1",          # 가격 범위 사용
                    "fid_input_price_1": "1000",      # 최소 1,000원
                    "fid_input_price_2": "100000",    # 최대 100,000원
                    "fid_vol_cnt": "10000",           # 최소 거래량 10,000주
                    "fid_trgt_cls_code": "0",
                    "fid_trgt_exls_cls_code": "0",
                    "fid_div_cls_code": "0",
                    "fid_rsfl_rate1": "",
                    "fid_rsfl_rate2": "",
                },
            )

            if resp.get("rt_cd") != "0":
                log.warning("surge_ranking_failed",
                            market=market_code,
                            msg=resp.get("msg1", ""))
                continue

            for item in resp.get("output", []):
                try:
                    symbol = item.get("stck_shrn_iscd", "")
                    name = item.get("hts_kor_isnm", "").strip()
                    price = int(item.get("stck_prpr", "0"))
                    change_pct = float(item.get("prdy_ctrt", "0"))
                    volume = int(item.get("acml_vol", "0"))

                    # 기본 필터
                    if price < 1000 or price > 100000:
                        continue
                    if change_pct < 3.0:
                        continue
                    if volume < 10000:
                        continue

                    # ETF·스팩·우선주 제외
                    if any(kw in name for kw in EXCLUDE_KEYWORDS):
                        continue
                    if name.endswith("우") or name.endswith("우B"):
                        continue

                    # 점수: 등락률 기반 (거래량은 이미 필터링됨)
                    score = change_pct * (1 + min(volume / 1_000_000, 10))

                    candidates.append(SurgeCandidate(
                        symbol=symbol,
                        name=name,
                        price=price,
                        change_pct=round(change_pct, 2),
                        volume=volume,
                        volume_ratio=0.0,  # API에서 직접 제공하지 않음
                        score=round(score, 2),
                    ))

                except (ValueError, KeyError):
                    continue

        except Exception as e:
            log.error("surge_scan_error", market=market_code, error=str(e))
            continue

    # 점수 내림차순 정렬
    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates
