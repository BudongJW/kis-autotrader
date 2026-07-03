"""3자 상태 대조 — API(진실원천) vs 저널(portfolio.json + trades.csv 순포지션).

AI/API/저널 싱크 불일치(2026-07-03 혼란·6/30 고아)를 근본 차단하기 위한 대조 도구.
읽기 전용(주문 없음). debug-once로 실행.

출력:
  [API]       실시간 브로커 보유·현금·당일매매 (= 진실)
  [portfolio] 대시보드가 보는 보유 + updated_at(지연)
  [trades]    trades.csv 전체 순포지션(누적 매수-매도) + 오늘 거래수
  [대조]      종목별 세 값 일치 여부, 고아(API보유인데 기록없음)·유령(기록있는데 API없음) 플래그
"""
from __future__ import annotations

import csv
import io
import json
import urllib.request
from datetime import datetime

from src.kis_client import KISClient

JOURNAL = "https://raw.githubusercontent.com/BudongJW/kis-trading-journal/main"


def _fetch(path: str) -> str:
    url = f"{JOURNAL}{path}?cb={datetime.now().strftime('%H%M%S%f')}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read().decode("utf-8")


def _minutes_ago(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
        secs = (datetime.now() - t).total_seconds()
        return f"{int(secs // 60)}분 {int(secs % 60)}초 전"
    except Exception:
        return "?"


def main() -> None:
    print("=" * 60)
    print(f"3자 상태 대조 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ── [API] 진실원천 ──
    api_hold: dict[str, int] = {}
    thdt_buy = thdt_sll = 0
    try:
        client = KISClient()
        resp = client.get_balance()
        if resp.get("rt_cd") == "0":
            for it in resp.get("output1", []):
                q = int(it.get("hldg_qty", 0) or 0)
                if q > 0:
                    api_hold[it.get("pdno", "")] = q
            o2 = (resp.get("output2") or [{}])[0]
            thdt_buy = int(o2.get("thdt_buy_amt", 0) or 0)
            thdt_sll = int(o2.get("thdt_sll_amt", 0) or 0)
            api_ok = True
        else:
            api_ok = False
            print(f"[API] 조회실패 rt_cd={resp.get('rt_cd')}")
    except Exception as e:
        api_ok = False
        print(f"[API] 예외: {e}")
    print(f"[API 진실]   보유 {api_hold or '없음'} | 당일매수 {thdt_buy:,} 매도 {thdt_sll:,}")

    # ── [저널 portfolio.json] 대시보드가 보는 것 ──
    pf_hold: dict[str, int] = {}
    try:
        pf = json.loads(_fetch("/_data/portfolio.json"))
        pf_hold = {str(h.get("symbol")): int(h.get("qty", 0) or 0)
                   for h in (pf.get("holdings") or []) if int(h.get("qty", 0) or 0) > 0}
        print(f"[portfolio]  보유 {pf_hold or '없음'} | updated {pf.get('updated_at')} "
              f"({_minutes_ago(pf.get('updated_at', ''))})")
    except Exception as e:
        print(f"[portfolio]  조회실패: {e}")

    # ── [저널 trades.csv] 전체 순포지션 ──
    net: dict[str, int] = {}
    today_cnt = 0
    try:
        rows = list(csv.DictReader(io.StringIO(_fetch("/state/trades.csv"))))
        today = datetime.now().strftime("%Y-%m-%d")
        for row in rows:
            sym = str(row.get("symbol", ""))
            qty = int(row.get("qty", 0) or 0)
            side = row.get("side", "")
            if side == "buy":
                net[sym] = net.get(sym, 0) + qty
            elif side == "sell":
                net[sym] = net.get(sym, 0) - qty
            if row.get("timestamp", "").startswith(today):
                today_cnt += 1
        net = {k: v for k, v in net.items() if v != 0}
        print(f"[trades]     순포지션(누적) {net or '없음'} | 오늘거래 {today_cnt}건")
    except Exception as e:
        print(f"[trades]     조회실패: {e}")

    # ── [대조] ──
    print("-" * 60)
    print("종목별 대조 (API | portfolio | trades순):")
    all_syms = set(api_hold) | set(pf_hold) | set(net)
    synced = True
    for s in sorted(all_syms):
        a, p, n = api_hold.get(s, 0), pf_hold.get(s, 0), net.get(s, 0)
        mark = "OK" if a == p == n else "불일치"
        if not (a == p == n):
            synced = False
        print(f"  {s}: {a} | {p} | {n}   [{mark}]")

    orphan = set(api_hold) - set(net)           # API 보유인데 trades에 기록 없음
    phantom = set(net) - set(api_hold)          # trades엔 있는데 API 보유 아님
    if orphan:
        print(f"  [경고] 고아(API보유 미기록) — 봇 미관리 위험: {orphan}")
    if phantom:
        print(f"  [경고] 유령(기록만 있고 API 없음): {phantom}")
    if not all_syms:
        print("  (전 소스 보유 없음 — flat)")

    print("=" * 60)
    verdict = "동기화 OK" if (synced and api_ok and not orphan and not phantom) else "불일치 — 확인 필요"
    print(f"판정: {verdict}")


if __name__ == "__main__":
    main()
