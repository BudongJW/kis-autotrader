"""청산 품질 진단 (상시 계측용) — '비싸게 사고 싸게 판다'가 실제로 데이터에 있는지 검증.

trades.csv를 FIFO로 페어링해 라운드트립 손익, 승률, 평균이익/평균손실, 보유시간,
청산사유별 분포, 진입 시점의 '이미 오른 정도'를 집계한다.
"""
from __future__ import annotations

import csv
import re
import statistics
from collections import defaultdict, deque
from pathlib import Path

SRC = Path(r"C:\Users\wodnj\kis-trading-journal\state\trades.csv")


def load():
    rows = []
    with SRC.open(encoding="utf-8") as f:
        for r in csv.reader(f):
            if len(r) < 8:
                continue
            ts, sym, name, side, qty, price, amount, fee = r[:8]
            reason = r[8] if len(r) > 8 else ""
            if side not in ("buy", "sell"):
                continue
            try:
                rows.append({
                    "ts": ts, "sym": sym, "name": name, "side": side,
                    "qty": int(float(qty)), "price": float(price),
                    "amount": float(amount), "reason": reason,
                })
            except ValueError:
                continue
    return rows


def pair_fifo(rows):
    """종목별 FIFO 매칭으로 라운드트립 생성."""
    books = defaultdict(deque)
    trips = []
    for r in rows:
        if r["side"] == "buy":
            books[r["sym"]].append(dict(r))
        else:
            remain = r["qty"]
            while remain > 0 and books[r["sym"]]:
                b = books[r["sym"]][0]
                take = min(remain, b["qty"])
                pnl_pct = (r["price"] - b["price"]) / b["price"] * 100
                trips.append({
                    "sym": r["sym"], "name": r["name"],
                    "buy_ts": b["ts"], "sell_ts": r["ts"],
                    "buy_px": b["price"], "sell_px": r["price"],
                    "qty": take, "pnl_pct": pnl_pct,
                    "pnl_krw": (r["price"] - b["price"]) * take,
                    "buy_reason": b["reason"], "sell_reason": r["reason"],
                })
                b["qty"] -= take
                remain -= take
                if b["qty"] <= 0:
                    books[r["sym"]].popleft()
    return trips


def hold_min(a, b):
    from datetime import datetime
    try:
        fa = datetime.fromisoformat(a)
        fb = datetime.fromisoformat(b)
        return (fb - fa).total_seconds() / 60
    except Exception:
        return -1


def main():
    rows = load()
    trips = pair_fifo(rows)
    print(f"전체 거래 {len(rows)}건 -> 라운드트립 {len(trips)}건\n")

    wins = [t for t in trips if t["pnl_pct"] > 0]
    losses = [t for t in trips if t["pnl_pct"] <= 0]
    print("=== 전체 성적 ===")
    print(f"  승 {len(wins)} / 패 {len(losses)} | 승률 {len(wins)/max(1,len(trips))*100:.1f}%")
    if wins:
        print(f"  평균이익 {statistics.mean(t['pnl_pct'] for t in wins):+.2f}%")
    if losses:
        print(f"  평균손실 {statistics.mean(t['pnl_pct'] for t in losses):+.2f}%")
    print(f"  합계손익(원) {sum(t['pnl_krw'] for t in trips):+,.0f}")
    print(f"  평균 보유 {statistics.mean([hold_min(t['buy_ts'], t['sell_ts']) for t in trips]):.0f}분")

    # 손익비(R:R) — 이게 1 미만이면 '작게 벌고 크게 잃는' 구조
    if wins and losses:
        aw = statistics.mean(t["pnl_pct"] for t in wins)
        al = abs(statistics.mean(t["pnl_pct"] for t in losses))
        wr = len(wins) / len(trips)
        print(f"  손익비 {aw/al:.2f} | 기대값 {wr*aw - (1-wr)*al:+.3f}%/거래")

    print("\n=== 청산 사유별 ===")
    by_reason = defaultdict(list)
    for t in trips:
        key = re.split(r"[(:]", t["sell_reason"])[0].strip()[:24] or "(없음)"
        by_reason[key].append(t)
    for k, v in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        m = statistics.mean(t["pnl_pct"] for t in v)
        tot = sum(t["pnl_krw"] for t in v)
        print(f"  {k:<26} {len(v):>3}건 | 평균 {m:+.2f}% | 합계 {tot:+9,.0f}원")

    print("\n=== 진입 시 '이미 움직인 정도' vs 결과 ===")
    # buy_reason에 '전일대비 +X%' 가 있는 모멘텀 진입만
    buckets = defaultdict(list)
    for t in trips:
        m = re.search(r"전일대비\s*([+-]?\d+\.?\d*)%", t["buy_reason"])
        if not m:
            continue
        mv = abs(float(m.group(1)))
        b = "0~1%" if mv < 1 else "1~2%" if mv < 2 else "2~3%" if mv < 3 else "3%+"
        buckets[b].append(t["pnl_pct"])
    for b in ["0~1%", "1~2%", "2~3%", "3%+"]:
        v = buckets.get(b, [])
        if v:
            wr = sum(1 for x in v if x > 0) / len(v) * 100
            print(f"  진입시 변동 {b:<6} {len(v):>3}건 | 평균 {statistics.mean(v):+.2f}% | 승률 {wr:.0f}%")

    print("\n=== 최근 20 라운드트립 ===")
    for t in trips[-20:]:
        print(f"  {t['buy_ts'][5:16]} -> {t['sell_ts'][11:16]} {t['name'][:10]:<10} "
              f"{t['buy_px']:>8,.0f} -> {t['sell_px']:>8,.0f} {t['pnl_pct']:+6.2f}% "
              f"({hold_min(t['buy_ts'], t['sell_ts']):>4.0f}분) {t['sell_reason'][:34]}")


if __name__ == "__main__":
    main()
