"""사용자 지시 수동 리포지션 — 069500 롱 전량 매도 후 인버스(114800) 사이즈 매수.

배경(2026-07-14): 폭락+이란/호르무즈 격화로 리스크오프 지속인데 봇이 롱을 반복
진입해 물림(HMM이 급락을 sideways/bull로 오분류 → 롱차단 게이트 미작동). 사용자가
"인덱스 정리하고 포지션 새로" 지시. 트렌드 정렬 = 인버스.

올인 금지(하드룰): 목표 매수액 TARGET_KRW 상한 + 주문가능현금 한도 내에서만. 실전(MODE=live).
debug-once: script=scripts.reposition_inverse
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from src.kis_client import KISClient

LONG_SYMBOL = "069500"          # 정리 대상(롱)
INV_SYMBOL = "114800"           # 신규 포지션(KODEX 인버스, 1x)
INV_NAME = "KODEX 인버스"
TARGET_KRW = 250_000            # 인버스 목표 매수액 상한(올인 방지, 총자본 ~457k의 약 55%)


def _held(c: KISClient, sym: str) -> tuple[int, int]:
    bal = c.get_balance()
    for it in (bal.get("output1") or []):
        if it.get("pdno") == sym:
            return (int(float(it.get("hldg_qty", 0) or 0)),
                    int(float(it.get("pchs_avg_pric", 0) or 0)))
    return 0, 0


def _ord_psbl(c: KISClient) -> int:
    bal = c.get_balance()
    o2 = bal.get("output2") or []
    o2 = (o2[0] if isinstance(o2, list) else o2) or {}
    return int(float(o2.get("ord_psbl_cash", 0) or 0))


def main() -> None:
    c = KISClient()

    # 1) 069500 롱 전량 매도
    lq, la = _held(c, LONG_SYMBOL)
    if lq > 0:
        print(f"[1] 롱 정리: {LONG_SYMBOL} {lq}주(매입 {la:,}) 시장가 매도")
        r = c.order_cash(LONG_SYMBOL, lq, side="sell", order_type="01")
        print(f"    rt_cd={r.get('rt_cd')} msg={r.get('msg1','')}")
        if r.get("rt_cd") != "0":
            print("!! 롱 매도 실패 — 중단(인버스 진입 안 함) !!")
            return
        time.sleep(2.5)
        try:
            from src.risk_manager import remove_position
            remove_position(LONG_SYMBOL)
        except Exception as e:  # noqa: BLE001
            print("    remove_position 경고:", e)
        try:
            from src.tracker import log_trade
            pr = c.get_price(LONG_SYMBOL)
            px = int(float(pr.get("output", {}).get("stck_prpr", 0) or 0))
            log_trade(LONG_SYMBOL, "KODEX 200", "sell", lq, (px or la) * 100,
                      market="KR", reason="사용자 지시 리포지션: 롱 정리(폭락장 역행 손절)")
        except Exception as e:  # noqa: BLE001
            print("    log_trade 경고:", e)
    else:
        print(f"[1] {LONG_SYMBOL} 보유 없음 — 매도 스킵")

    # 2) 인버스 매수 사이즈 계산 (올인 금지: 목표 상한 & 주문가능 한도)
    time.sleep(1.0)
    psbl = _ord_psbl(c)
    pr = c.get_price(INV_SYMBOL)
    price = int(float(pr.get("output", {}).get("stck_prpr", 0) or 0)) if pr.get("rt_cd") == "0" else 0
    if price <= 0:
        print("!! 인버스 현재가 조회 실패 — 매수 중단 !!")
        return
    budget = min(TARGET_KRW, psbl)
    qty = budget // price
    print(f"[2] 인버스 매수 계산: 현재가 {price:,} | 주문가능 {psbl:,} | 목표상한 {TARGET_KRW:,} "
          f"→ 예산 {budget:,} → {qty}주")
    if qty < 1:
        print("!! 주문가능 부족으로 인버스 매수 불가(롱 매도대금 미정산 가능). 현재 플랫 상태로 종료 !!")
        return

    # 이미 인버스 보유분 있으면 합산 방지 위해 참고 출력
    hq, ha = _held(c, INV_SYMBOL)
    if hq > 0:
        print(f"    [참고] 기존 인버스 {hq}주(매입 {ha:,}) 보유 중 — 추가 매수됨")

    r = c.order_cash(INV_SYMBOL, int(qty), side="buy", order_type="01")
    print(f"    [매수주문] rt_cd={r.get('rt_cd')} msg={r.get('msg1','')}")
    if r.get("rt_cd") != "0":
        print("!! 인버스 매수 실패 !!")
        return

    time.sleep(2.5)
    h2, a2 = _held(c, INV_SYMBOL)
    print(f"    [체결확인] {INV_SYMBOL} 보유 {h2}주 | 매입가 {a2:,} | 매입금액 {a2*h2:,}")

    # 3) 봇 청산관리 등록 (손절/트레일 관리)
    try:
        from src.risk_manager import record_buy
        record_buy(INV_SYMBOL, a2 or price, h2 or int(qty))
    except Exception as e:  # noqa: BLE001
        print("    record_buy 경고:", e)
    try:
        from src.tracker import log_trade
        log_trade(INV_SYMBOL, INV_NAME, "buy", h2 or int(qty), (a2 or price) * 100,
                  market="KR", reason="사용자 지시 리포지션: 인버스 신규(리스크오프 정렬)")
    except Exception as e:  # noqa: BLE001
        print("    log_trade 경고:", e)
    mp = Path("logs/morning_positions.json")
    st = {}
    if mp.exists():
        try:
            st = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            st = {}
    st.pop(LONG_SYMBOL, None)
    st[INV_SYMBOL] = {"direction": "inverse", "entry_price": a2 or price,
                      "qty": h2 or int(qty), "name": INV_NAME, "peak": a2 or price,
                      "date": datetime.now().strftime("%Y-%m-%d")}
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[3] 등록완료. 봇이 손절/트레일로 관리.")
    print("리포지션 완료: 롱 정리 → 인버스 신규.")


if __name__ == "__main__":
    main()
