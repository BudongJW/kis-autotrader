"""KIS 잔고 즉시 조회 — 진행 중 봇 안 죽이고 현재 보유분 확인."""

from __future__ import annotations

from src.kis_client import KISClient


def main() -> None:
    client = KISClient()

    print("=" * 60)
    print("KIS 잔고 조회 (44609714-01)")
    print("=" * 60)

    resp = client.get_balance()
    rt_cd = resp.get("rt_cd")
    print(f"rt_cd: {rt_cd}, msg: {resp.get('msg1', '')[:80]}")

    if rt_cd != "0":
        print("❌ 잔고 조회 실패")
        return

    # 보유 종목
    output1 = resp.get("output1", [])
    print(f"\n=== 보유 종목 ({len(output1)}건) ===")
    if not output1:
        print("  (없음)")
    for item in output1:
        qty = int(item.get("hldg_qty", 0))
        if qty <= 0:
            continue
        sym = item.get("pdno", "")
        name = item.get("prdt_name", "")
        avg = int(float(item.get("pchs_avg_pric", 0)))
        cur = int(item.get("prpr", 0))
        eval_amt = int(item.get("evlu_amt", 0))
        pnl = int(item.get("evlu_pfls_amt", 0))
        pnl_rt = float(item.get("evlu_pfls_rt", 0))
        print(f"  {sym} {name}")
        print(f"    수량: {qty}주 | 평단: {avg:,}원 | 현재가: {cur:,}원")
        print(f"    평가금액: {eval_amt:,}원 | 손익: {pnl:+,}원 ({pnl_rt:+.2f}%)")

    # 계좌 요약
    output2 = resp.get("output2", [])
    if output2:
        o2 = output2[0] if isinstance(output2, list) else output2
        cash = int(o2.get("dnca_tot_amt", 0))
        ord_psbl = int(o2.get("ord_psbl_cash", 0))
        total_eval = int(o2.get("tot_evlu_amt", 0))
        scts_eval = int(o2.get("scts_evlu_amt", 0))
        print(f"\n=== 계좌 요약 ===")
        print(f"  예수금: {cash:,}원")
        print(f"  주문가능: {ord_psbl:,}원")
        print(f"  유가증권 평가: {scts_eval:,}원")
        print(f"  총평가: {total_eval:,}원")


if __name__ == "__main__":
    main()
