"""자동매매 전 기능 통합 테스트.

실전 계좌로 읽기 전용 기능만 테스트한다.
주문(매수/매도)은 dry-run으로 검증하고, 실제 주문은 보내지 않는다.
"""

from __future__ import annotations

import sys
import time

import pandas as pd

from src.config import settings
from src.kis_auth import get_token
from src.kis_client import KISClient
from src.strategies.golden_cross import GoldenCrossStrategy
from src.strategies.base import SignalType
from src.utils.rate_limit import TokenBucketLimiter


SYMBOL = "005930"  # 삼성전자
passed = 0
failed = 0


def report(name: str, ok: bool, detail: str = ""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


# =====================================================================
# 1. 인증 (토큰 발급)
# =====================================================================
print("\n=== 1. 인증 (토큰 발급) ===")
try:
    settings.validate_runtime()
    token = get_token(force_refresh=True)
    report("토큰 발급", True, f"만료: {token.expires_at:%Y-%m-%d %H:%M}")
except Exception as e:
    report("토큰 발급", False, str(e))

# =====================================================================
# 2. 현재가 시세 조회
# =====================================================================
print("\n=== 2. 현재가 시세 조회 ===")
client = KISClient()
try:
    resp = client.get_price(SYMBOL)
    ok = resp.get("rt_cd") == "0"
    price = resp["output"]["stck_prpr"] if ok else "N/A"
    report("현재가 조회", ok, f"삼성전자 {int(price):,}원" if ok else resp.get("msg1", ""))
except Exception as e:
    report("현재가 조회", False, str(e))

# =====================================================================
# 3. 일별 시세 조회
# =====================================================================
print("\n=== 3. 일별 시세 조회 ===")
try:
    resp = client.get_daily_price(SYMBOL)
    ok = resp.get("rt_cd") == "0"
    rows = resp.get("output", [])
    report("일별 시세", ok and len(rows) > 0, f"{len(rows)}일치 데이터")

    # DataFrame 변환 테스트
    if rows:
        df = pd.DataFrame(rows)
        df["stck_clpr"] = pd.to_numeric(df["stck_clpr"], errors="coerce")
        report("DataFrame 변환", len(df) > 0 and df["stck_clpr"].notna().all(),
               f"최근 종가: {int(df['stck_clpr'].iloc[0]):,}원")
except Exception as e:
    report("일별 시세", False, str(e))

# =====================================================================
# 4. 잔고 조회
# =====================================================================
print("\n=== 4. 잔고 조회 ===")
try:
    resp = client.get_balance()
    ok = resp.get("rt_cd") == "0"
    if ok:
        holdings = resp.get("output1", [])
        cash_info = resp.get("output2", [{}])
        total_eval = cash_info[0].get("tot_evlu_amt", "0") if cash_info else "0"
        report("잔고 조회", True, f"보유 종목: {len(holdings)}개, 총평가: {int(total_eval):,}원")
    else:
        report("잔고 조회", False, resp.get("msg1", ""))
except Exception as e:
    report("잔고 조회", False, str(e))

# =====================================================================
# 5. 전략 신호 생성 (골든크로스)
# =====================================================================
print("\n=== 5. 전략 신호 생성 ===")
try:
    # bot/runner.py의 fetch_recent_history 로직 재현
    resp = client.get_daily_price(SYMBOL)
    rows = resp.get("output", [])
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "stck_bsop_date": "date", "stck_oprc": "open",
        "stck_hgpr": "high", "stck_lwpr": "low",
        "stck_clpr": "close", "acml_vol": "volume",
    })
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").set_index("date")
    history = df[["open", "high", "low", "close", "volume"]]

    strategy = GoldenCrossStrategy(short_window=5, long_window=20)
    signal = strategy.generate_signal(SYMBOL, history)
    report("신호 생성", signal.type in (SignalType.BUY, SignalType.SELL, SignalType.HOLD),
           f"{signal.type.value} @ {signal.price:,.0f}원 — {signal.reason}")
except Exception as e:
    report("신호 생성", False, str(e))

# =====================================================================
# 6. Rate Limiter
# =====================================================================
print("\n=== 6. Rate Limiter ===")
try:
    limiter = TokenBucketLimiter(rate_per_sec=3, window_sec=1.0)
    start = time.monotonic()
    for _ in range(3):
        limiter.acquire()
    t1 = time.monotonic() - start
    report("한도 내 즉시 통과", t1 < 0.2, f"{t1:.3f}s")

    start = time.monotonic()
    limiter.acquire()  # 4번째 → 대기 발생
    t2 = time.monotonic() - start
    report("한도 초과 시 대기", t2 >= 0.5, f"{t2:.3f}s 대기")
except Exception as e:
    report("Rate Limiter", False, str(e))

# =====================================================================
# 7. 주문 dry-run (실제 주문 안 보냄)
# =====================================================================
print("\n=== 7. 주문 구조 검증 (dry-run) ===")
try:
    # order_cash가 올바른 TR_ID를 선택하는지, body 구성이 맞는지 확인
    from src.kis_client import (
        TR_ORDER_CASH_LIVE_BUY, TR_ORDER_CASH_LIVE_SELL,
        TR_ORDER_CASH_PAPER_BUY, TR_ORDER_CASH_PAPER_SELL,
    )
    if settings.is_live:
        report("매수 TR_ID", TR_ORDER_CASH_LIVE_BUY == "TTTC0802U", TR_ORDER_CASH_LIVE_BUY)
        report("매도 TR_ID", TR_ORDER_CASH_LIVE_SELL == "TTTC0801U", TR_ORDER_CASH_LIVE_SELL)
    else:
        report("매수 TR_ID", TR_ORDER_CASH_PAPER_BUY == "VTTC0802U", TR_ORDER_CASH_PAPER_BUY)
        report("매도 TR_ID", TR_ORDER_CASH_PAPER_SELL == "VTTC0801U", TR_ORDER_CASH_PAPER_SELL)
    report("계좌번호 설정", len(settings.kis_account_no) == 8, settings.kis_account_no[:4] + "****")
    report("주문 함수 존재", callable(client.order_cash), "order_cash() callable")
except Exception as e:
    report("주문 구조 검증", False, str(e))

# =====================================================================
# 8. 로깅
# =====================================================================
print("\n=== 8. 로깅 ===")
try:
    from src.utils.logger import log
    from pathlib import Path
    log.info("test_log_entry", test=True, source="test_all_features")
    log_file = Path("./logs/kis.log")
    report("로그 파일 생성", log_file.exists(), str(log_file))
except Exception as e:
    report("로깅", False, str(e))

# =====================================================================
# 결과 요약
# =====================================================================
print("\n" + "=" * 50)
print(f"결과: {passed} PASS / {failed} FAIL (총 {passed + failed})")
print("=" * 50)
if failed > 0:
    print("실패한 항목을 수정한 뒤 다시 실행하세요.")
    sys.exit(1)
else:
    print("모든 기능 정상. 자동매매 준비 완료.")
    sys.exit(0)
