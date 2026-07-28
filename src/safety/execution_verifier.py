"""체결 검증 — KIS rt_cd=0이 항상 체결을 의미하진 않음.

KIS API의 order_cash 응답:
  rt_cd=0: 주문 '접수' 성공 (체결은 별개)
  rt_cd=1/2/E: 명시적 오류

KIS 백오피스에서 거부될 수 있음 (시간 외 주문·동시호가 등). 봇은
inquire-daily-ccld endpoint로 실제 체결 여부를 별도 검증해야 함.

이 모듈:
  - reconcile_trades_with_ccld(): trades.csv vs KIS ccld 비교
  - 거부된 주문은 SQLite ledger의 orders.status를 'rejected'로 갱신
  - portfolio에 잘못 기록된 trade는 별도 'rejected_trades' 필드로 분리
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.tracker import TRADE_LOG_PATH, is_kr_symbol
from src.utils.logger import log
from src.utils.clock import now_kst, today_kst


def fetch_today_ccld(client) -> dict:
    """오늘자 KIS 체결·미체결 조회 → {(symbol, ord_time): {qty, ccld_qty, status}}."""
    today = now_kst().strftime("%Y%m%d")
    try:
        resp = client.inquire_daily_ccld(today, today, ccld_type="00")
        if resp.get("rt_cd") != "0":
            return {}
        output1 = resp.get("output1", [])
        if not isinstance(output1, list):
            output1 = [output1] if output1 else []
        result = {}
        for o in output1:
            sym = o.get("pdno", "")
            ord_time = o.get("ord_tmd", "")[:6]  # HHMMSS
            ord_qty = int(o.get("ord_qty", 0) or 0)
            ccld_qty = int(o.get("tot_ccld_qty", 0) or 0)
            rjct = o.get("rjct_qty", "0")
            cncl = o.get("cncl_yn", "N")

            if ccld_qty >= ord_qty and ord_qty > 0:
                status = "executed"
            elif ccld_qty > 0:
                status = "partial"
            elif rjct and rjct != "0":
                status = "rejected"
            elif cncl == "Y":
                status = "cancelled"
            else:
                status = "pending"

            side = "buy" if o.get("sll_buy_dvsn_cd") == "02" else "sell"
            key = (sym, ord_time, side)
            result[key] = {
                "ord_qty": ord_qty,
                "ccld_qty": ccld_qty,
                "status": status,
                "rjct_qty": rjct,
                "ord_unpr": int(float(o.get("ord_unpr", 0) or 0)),
                "avg_price": int(float(o.get("avg_prvs", 0) or 0)),
            }
        return result
    except Exception as e:
        log.warning("ccld_fetch_failed", error=str(e))
        return {}


def reconcile_trades(client) -> dict:
    """trades.csv의 오늘 **국내** entry vs KIS ccld 비교. 불일치 발견 시 ledger 정정.

    **국내 전용인 이유** — 비교 대상인 fetch_today_ccld()는
    `/uapi/domestic-stock/v1/trading/inquire-daily-ccld`(TTTC8001R), 즉 국내 체결
    조회다. 해외 체결은 이 응답에 절대 들어오지 않는다. 그런데 trades.csv에는
    KR·US가 한 파일에 섞여 기록되므로, 예전엔 **모든 미국 거래가 영구적으로
    `ccld_not_found`로 잡혔다**. 실제 운영 로그(2026-07-28):

        [체결 검증] reviewed=9, executed=7, rejected=0, pending=0
          ⚠️ {'symbol': 'PSQ', 'time': '003211', 'side': 'buy',  'issue': 'ccld_not_found'}
          ⚠️ {'symbol': 'PSQ', 'time': '044507', 'side': 'sell', 'issue': 'ccld_not_found'}

    PSQ는 미국 인버스 ETF로, 정상 체결됐는데도 오탐이 난 것이다. 오탐이 일상이
    되면 진짜 불일치가 묻히고, rejected/pending이 잡히면 텔레그램 오류 알림까지
    나간다. 그래서 국내 종목만 검증 대상으로 삼고 해외분은 따로 센다.

    Returns:
        {'reviewed': N, 'executed': N, 'rejected': N, 'pending': N,
         'skipped_overseas': N, 'mismatches': [...]}
        reviewed는 **검증한 국내 건수**다 (skipped_overseas는 별도).
    """
    result = {"reviewed": 0, "executed": 0, "rejected": 0,
              "pending": 0, "skipped_overseas": 0, "mismatches": []}

    if not TRADE_LOG_PATH.exists():
        return result

    today = today_kst().isoformat()

    # trades.csv에서 오늘자 entry 추출 — 국내/해외 분리
    today_trades = []
    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("timestamp", "").startswith(today):
                continue
            if is_kr_symbol(row.get("symbol", "")):
                today_trades.append(row)
            else:
                # 해외 체결은 국내 ccld로 검증 불가 (해외 체결내역 조회 미구현)
                result["skipped_overseas"] += 1

    if not today_trades:
        return result

    ccld = fetch_today_ccld(client)
    if not ccld:
        return result

    # 각 trade를 ccld와 매칭
    for t in today_trades:
        result["reviewed"] += 1
        ts = t.get("timestamp", "")  # ISO format YYYY-MM-DDTHH:MM:SS
        try:
            hhmmss = ts[11:].replace(":", "")[:6]  # HHMMSS
        except Exception:
            continue
        sym = t.get("symbol", "")
        side = t.get("side", "")

        # ccld에서 매칭 — 시각 ±60초 허용
        matched = None
        for (csym, ctime, cside), cdata in ccld.items():
            if csym != sym or cside != side:
                continue
            try:
                if abs(int(ctime) - int(hhmmss)) <= 60:
                    matched = cdata
                    break
            except ValueError:
                continue

        if not matched:
            # ccld에 없음 → 봇이 주문 안 보냈거나 KIS가 응답 못 줌
            result["mismatches"].append({
                "symbol": sym, "time": hhmmss, "side": side,
                "issue": "ccld_not_found",
                "logged_qty": int(t.get("qty", 0)),
            })
            continue

        if matched["status"] == "executed":
            result["executed"] += 1
        elif matched["status"] == "rejected":
            result["rejected"] += 1
            result["mismatches"].append({
                "symbol": sym, "time": hhmmss, "side": side,
                "issue": "kis_rejected",
                "logged_qty": int(t.get("qty", 0)),
                "ccld_qty": matched["ccld_qty"],
                "rjct_qty": matched["rjct_qty"],
            })
            # SQLite ledger 정정
            _mark_order_rejected(sym, side, int(t.get("qty", 0)),
                                  int(t.get("price", 0)),
                                  reason=f"KIS rejected ({matched['rjct_qty']}주)")
        elif matched["status"] == "pending":
            result["pending"] += 1

    # 불일치 발견 시 ledger 이벤트 + 텔레그램 알람
    if result["rejected"] > 0 or result["mismatches"]:
        try:
            from src.safety.ledger import log_event
            log_event("trade_reconcile", "warning", result)
        except Exception:
            pass
        try:
            from src.safety.notifier import notify_error
            notify_error(
                f"체결 불일치 — 거부 {result['rejected']}건, 대기 {result['pending']}건",
                context=(f"reviewed={result['reviewed']}(국내), "
                         f"executed={result['executed']}, "
                         f"해외 {result['skipped_overseas']}건 검증제외"),
            )
        except Exception:
            pass

    return result


def _mark_order_rejected(symbol: str, side: str, qty: int, price: int,
                         reason: str = "") -> None:
    """SQLite ledger의 orders 테이블에서 매칭되는 주문을 'rejected'로 표시."""
    try:
        from src.safety.ledger import LEDGER_PATH
        import sqlite3
        if not LEDGER_PATH.exists():
            return
        today = today_kst().isoformat()
        with sqlite3.connect(LEDGER_PATH) as conn:
            # 오늘자 같은 종목·사이드의 'executed' 상태를 'rejected'로 정정
            conn.execute(
                """UPDATE orders SET status = 'rejected', gate_reason = ?
                   WHERE symbol = ? AND side = ? AND qty = ? AND price = ?
                     AND status = 'executed'
                     AND attempted_at LIKE ?""",
                (reason, symbol, side, qty, price, f"{today}%"),
            )
            conn.commit()
    except Exception as e:
        log.warning("ledger_reject_mark_failed", error=str(e))
