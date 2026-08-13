"""장부 정합성 감사 실행 — 거래기록 vs 실제 보유 대조.

기록이 조용히 어긋나는 걸 막는다(2026-08: PSQ -11주, 114800 -449주 발견).
debug-once: script=scripts.audit_ledger
"""
from __future__ import annotations

import sys
from pathlib import Path

from src.ledger_audit import audit, format_report, load_rows

CANONICAL = "https://raw.githubusercontent.com/BudongJW/kis-trading-journal/main/state/trades.csv"


def _broker_holdings() -> dict[str, int] | None:
    """실계좌 보유(국내+해외). 조회 실패 시 None(부호 검사만)."""
    try:
        from src.kis_client import KISClient
        c = KISClient()
        out: dict[str, int] = {}
        bal = c.get_balance()
        for it in (bal.get("output1") or []):
            q = int(float(it.get("hldg_qty", 0) or 0))
            if q > 0:
                out[str(it.get("pdno"))] = q
        for exch in ("NASD", "AMEX"):
            try:
                ov = c.get_overseas_balance(exch)
                for it in (ov.get("output1") or []):
                    q = int(float(it.get("ovrs_cblc_qty", 0) or 0))
                    if q > 0:
                        out[str(it.get("ovrs_pdno"))] = q
            except Exception:  # noqa: BLE001
                pass
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 실보유 조회 실패({e}) — 순수량 부호만 검사")
        return None


def main() -> int:
    path = Path("logs/trades.csv")
    if not path.exists():
        import urllib.request
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            urllib.request.urlretrieve(CANONICAL, path)
        except Exception as e:  # noqa: BLE001
            print(f"거래기록을 찾을 수 없음: {e}")
            return 2

    rows = load_rows(path)
    print(f"거래기록 {len(rows)}건 감사")
    res = audit(rows, holdings=_broker_holdings())
    print(format_report(res))
    if not res.ok:
        print("\n조치: 유령 기록은 매도 경로의 '체결 미확인 시 요청값 기록' 폴백에서 생긴다."
              "\n      해당 폴백은 제거됨(2026-08). 잔여 불일치는 과거 기록이다.")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
