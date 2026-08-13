"""장부 정합성 감사 — 거래기록의 순수량이 실제 보유와 맞는지 대조한다.

배경(2026-08 실측): 미장 기록에서 PSQ 순수량 -11주, QQQM -1주가 나왔다. 산 것보다
판 기록이 많다 = 유령 매도가 장부에 박혔다는 뜻이다. 원인은 체결 확인 실패 시
'요청 수량대로 체결됐다고 치고' 기록하던 폴백이었다. 그 폴백은 제거했지만,
같은 종류의 어긋남이 다시 생기면 **조용히 넘어가지 않도록** 여기서 잡는다.

순수 함수라 테스트 가능하다. 실행 진입점은 scripts/audit_ledger.py.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

TRADES_PATH = Path("logs/trades.csv")


@dataclass
class LedgerIssue:
    symbol: str
    kind: str          # "negative_net" | "mismatch" | "duplicate_sell"
    detail: str
    net_qty: int = 0
    held_qty: int = 0


@dataclass
class AuditResult:
    issues: list[LedgerIssue] = field(default_factory=list)
    net_by_symbol: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.issues


def net_quantities(rows: list[dict]) -> dict[str, int]:
    """종목별 순수량(매수-매도). 정상이면 항상 0 이상이다."""
    net: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["side"] not in ("buy", "sell"):
            continue
        net[r["symbol"]] += r["qty"] if r["side"] == "buy" else -r["qty"]
    return dict(net)


def find_duplicate_sells(rows: list[dict], window_sec: int = 300) -> list[LedgerIssue]:
    """짧은 간격에 같은 종목·같은 수량 매도가 두 번 = 중복 기록 의심.

    2026-07-30 PSQ에서 2분 간격으로 10주 매도가 두 건 찍혔다(보유는 10주뿐).
    서로 다른 청산 경로가 같은 체결을 각자 기록한 결과다.
    """
    from datetime import datetime
    issues = []
    last: dict[tuple[str, int], datetime] = {}
    for r in rows:
        if r["side"] != "sell":
            continue
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (ValueError, TypeError):
            continue
        key = (r["symbol"], r["qty"])
        prev = last.get(key)
        if prev is not None and 0 <= (ts - prev).total_seconds() <= window_sec:
            issues.append(LedgerIssue(
                symbol=r["symbol"], kind="duplicate_sell",
                detail=f"{prev:%m-%d %H:%M} 와 {ts:%H:%M} 에 같은 {r['qty']}주 매도 "
                       f"({int((ts - prev).total_seconds())}초 간격) — 중복 기록 의심",
            ))
        last[key] = ts
    return issues


def audit(rows: list[dict], holdings: dict[str, int] | None = None) -> AuditResult:
    """장부 감사. holdings를 주면 실보유와도 대조한다.

    Args:
        rows: [{"ts","symbol","side","qty"}]
        holdings: {symbol: 실제 보유수량}. None이면 순수량 부호만 검사.
    """
    res = AuditResult(net_by_symbol=net_quantities(rows))
    for sym, net in sorted(res.net_by_symbol.items()):
        if net < 0:
            res.issues.append(LedgerIssue(
                symbol=sym, kind="negative_net", net_qty=net,
                detail=f"순수량 {net}주 — 산 것보다 판 기록이 많다(유령 매도)",
            ))
        elif holdings is not None:
            held = int(holdings.get(sym, 0))
            if net != held:
                res.issues.append(LedgerIssue(
                    symbol=sym, kind="mismatch", net_qty=net, held_qty=held,
                    detail=f"장부 {net}주 vs 실보유 {held}주 — {net - held:+d}주 어긋남",
                ))
    res.issues.extend(find_duplicate_sells(rows))
    return res


def load_rows(path: Path = TRADES_PATH) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for r in csv.reader(f):
            if len(r) < 6 or r[3] not in ("buy", "sell"):
                continue
            try:
                rows.append({"ts": r[0], "symbol": r[1], "side": r[3],
                             "qty": int(float(r[4]))})
            except ValueError:
                continue
    return rows


def format_report(res: AuditResult) -> str:
    if res.ok:
        return "장부 정합성 이상 없음"
    lines = [f"장부 이상 {len(res.issues)}건:"]
    for i in res.issues:
        lines.append(f"  [{i.kind}] {i.symbol}: {i.detail}")
    return "\n".join(lines)
