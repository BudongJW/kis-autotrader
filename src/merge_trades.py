"""trades.csv union-merge — 여러 run/워크플로의 거래 기록을 안전하게 누적.

GitHub Actions의 download-artifact@v4는 현재 run의 아티팩트만 보므로 매 run이
빈 trades.csv로 시작해 기록이 누적되지 않는 문제가 있었다(6-04 확인). 이를
해결하기 위해 journal repo의 canonical trades.csv를 단일 진실원천으로 두고,
각 run의 logs/trades.csv를 union(중복 제거)으로 합쳐 보존한다(CLAUDE.md #5).

git이 파일을 관리하므로 KR/US 워크플로 교차·루프 핸드오프 오버랩에도
pull --rebase + union으로 안전하게 누적된다.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

HEADER = ["timestamp", "symbol", "name", "side", "qty", "price", "amount",
          "balance_after", "reason"]
# 같은 거래로 간주하는 키 (동일 초·종목·방향·수량·가격이면 중복 체결로 판단)
KEY = ("timestamp", "symbol", "side", "qty", "price")


def _read(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f) if row.get("timestamp")]


def traded_symbols(path: str | Path) -> set[str]:
    """trades.csv에서 봇이 한 번이라도 거래한 종목 집합. 캐리 포지션 흡수 판정용."""
    return {r["symbol"] for r in _read(path) if r.get("symbol")}


def net_position(trades: list[dict]) -> dict[str, int]:
    """trades.csv 기준 종목별 순포지션 수량(매수합 - 매도합)."""
    net: dict[str, int] = {}
    for t in trades:
        sym = t.get("symbol", "")
        if not sym:
            continue
        try:
            q = int(float(t.get("qty", 0)))
        except (TypeError, ValueError):
            continue
        if t.get("side") == "buy":
            net[sym] = net.get(sym, 0) + q
        elif t.get("side") == "sell":
            net[sym] = net.get(sym, 0) - q
    return net


def find_unfilled_sells(net_pos: dict[str, int], broker_qty: dict[str, int]) -> dict[str, int]:
    """체결 미확인 매도(phantom) 감지: 거래기록상 순포지션 <= 0인데 broker엔 실제 보유.

    봇이 매도(rt_cd=0)를 기록했지만 실제 체결이 안 돼(동시호가 미체결 등) 잔고에
    남아있는 경우. 반환: {symbol: 실제 보유 수량}.
    """
    out: dict[str, int] = {}
    for sym, bq in (broker_qty or {}).items():
        if bq > 0 and net_pos.get(sym, 0) <= 0:
            out[sym] = bq
    return out


def merge_rows(*row_lists: list[dict]) -> list[dict]:
    """여러 row dict 리스트를 union·dedup·timestamp 오름차순 정렬.

    같은 거래(KEY 동일)가 중복 등장하면 첫 등장을 유지하되, **비어있는 reason은
    다른 복사본의 reason으로 채운다**(근거 유실 방지 — 옛 코드가 reason 없이 기록한
    뒤 새 코드가 reason과 함께 다시 봐도 근거가 보존되도록).
    """
    by_key: dict[tuple, dict] = {}
    order: list[tuple] = []
    for rows in row_lists:
        for r in rows:
            k = tuple(str(r.get(c, "")) for c in KEY)
            if k not in by_key:
                by_key[k] = dict(r)
                order.append(k)
            else:
                existing = by_key[k]
                if not (existing.get("reason") or "").strip() and (r.get("reason") or "").strip():
                    existing["reason"] = r["reason"]
    out = [by_key[k] for k in order]
    out.sort(key=lambda r: r.get("timestamp", ""))
    return out


def merge_files(base_path: str | Path, incoming_path: str | Path,
                out_path: str | Path | None = None) -> tuple[int, int]:
    """base(canonical) + incoming(이번 run)을 union해 out_path(기본 base)에 기록.

    Returns: (병합 후 총 건수, 새로 추가된 건수)
    """
    base = _read(base_path)
    incoming = _read(incoming_path)
    merged = merge_rows(base, incoming)
    out_path = Path(out_path or base_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        for r in merged:
            w.writerow(r)
    return len(merged), len(merged) - len(base)


if __name__ == "__main__":
    # 사용: python -m src.merge_trades <canonical_csv> <incoming_csv> [out_csv]
    base = sys.argv[1] if len(sys.argv) > 1 else "journal/state/trades.csv"
    inc = sys.argv[2] if len(sys.argv) > 2 else "logs/trades.csv"
    out = sys.argv[3] if len(sys.argv) > 3 else base
    total, added = merge_files(base, inc, out)
    print(f"[merge_trades] 총 {total}건 (신규 +{added}) → {out}")
