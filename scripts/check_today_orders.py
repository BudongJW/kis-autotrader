"""오늘자 주문·체결 상태 직접 조회 — 봇이 표시한 매수가 실제로 체결됐는지 봇이 스스로 검증.

5-27 사례: 봇이 KODEX 바이오 3주 @ 11,535원 매수 기록 → KIS 잔고 0건.
이게 미체결인지·거부인지·동기화 지연인지 KIS API로 직접 확인.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.kis_client import KISClient


def _print_orders(label: str, resp: dict):
    print(f"\n=== {label} ===")
    print(f"rt_cd: {resp.get('rt_cd')}, msg: {resp.get('msg1', '')[:80]}")

    output1 = resp.get("output1", [])
    if not isinstance(output1, list):
        output1 = [output1] if output1 else []

    if not output1:
        print("  (주문 0건)")
        return

    print(f"  총 {len(output1)}건")
    print(f"  {'시각':<10} {'종목':<10} {'매수/매도':<8} {'주문수량':>8} {'체결수량':>8} {'주문가':>10} {'체결가':>10} {'상태'}")
    for o in output1:
        ord_time = o.get("ord_tmd", "")[:6]
        sym = o.get("pdno", "")
        side = "매수" if o.get("sll_buy_dvsn_cd") == "02" else "매도"
        ord_qty = int(o.get("ord_qty", 0) or 0)
        ccld_qty = int(o.get("tot_ccld_qty", 0) or 0)
        ord_unpr = int(float(o.get("ord_unpr", 0) or 0))
        avg_prvs = int(float(o.get("avg_prvs", 0) or 0))
        status = ""
        if ccld_qty == ord_qty and ord_qty > 0:
            status = "✅ 전부 체결"
        elif ccld_qty > 0:
            status = f"⚠️ 일부 체결 ({ccld_qty}/{ord_qty})"
        elif ord_qty > 0:
            cncl_qty = int(o.get("cncl_yn", 0) or 0)
            if o.get("rjct_qty", "0") != "0":
                status = f"❌ 거부 ({o.get('rjct_qty')}주)"
            elif o.get("cncl_yn", "N") == "Y":
                status = "❌ 취소됨"
            else:
                status = "⏳ 미체결"
        print(f"  {ord_time:<10} {sym:<10} {side:<8} {ord_qty:>8} {ccld_qty:>8} "
              f"{ord_unpr:>10,} {avg_prvs:>10,} {status}")


def main() -> None:
    client = KISClient()
    today = datetime.now().strftime("%Y%m%d")

    print(f"오늘({today}) 주문·체결 조회")
    print("=" * 80)

    # 전체 (체결 + 미체결)
    resp = client.inquire_daily_ccld(today, today, ccld_type="00")
    _print_orders("[1] 오늘 전체 주문", resp)

    # 미체결만
    resp = client.inquire_daily_ccld(today, today, ccld_type="02")
    _print_orders("[2] 미체결 주문만", resp)

    # 체결만
    resp = client.inquire_daily_ccld(today, today, ccld_type="01")
    _print_orders("[3] 체결 완료 주문만", resp)


if __name__ == "__main__":
    main()
