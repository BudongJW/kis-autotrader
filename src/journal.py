"""투자 일지 생성기 — 퍼블릭 레포에 일일 노트 + 포트폴리오 데이터 푸시.

매일 장 마감 후 실행:
  1. 당일 거래 내역 분석
  2. 포트폴리오 현황 JSON 업데이트
  3. 일일 투자 노트 마크다운 생성
  4. 퍼블릭 레포에 커밋·푸시 (GitHub Actions에서)
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.kis_client import KISClient
from src.bot.single_run import (
    load_universe, load_strategy_params,
    get_all_holdings, get_available_cash, get_price,
)
from src.tracker import get_summary, TRADE_LOG_PATH


JOURNAL_DIR = Path("journal")  # GitHub Actions에서 퍼블릭 레포를 여기에 checkout
PORTFOLIO_PATH = JOURNAL_DIR / "_data" / "portfolio.json"
POSTS_DIR = JOURNAL_DIR / "_posts"


def get_todays_trades() -> list[dict]:
    """오늘 날짜의 거래 내역을 반환."""
    today = datetime.now().strftime("%Y-%m-%d")
    trades = []
    if not TRADE_LOG_PATH.exists():
        return trades
    with TRADE_LOG_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("timestamp", "").startswith(today):
                trades.append(row)
    return trades


def build_portfolio_json(client: KISClient) -> dict:
    """현재 포트폴리오 상태를 JSON으로 생성."""
    universe = load_universe()
    universe_syms = {s["symbol"] for s in universe}
    holdings_raw = get_all_holdings(client)
    cash = get_available_cash(client)
    params = load_strategy_params()
    summary = get_summary()

    # 보유 종목 상세
    holdings = []
    holdings_value = 0
    for sym, qty in holdings_raw.items():
        cur_price = get_price(client, sym)
        value = cur_price * qty
        holdings_value += value
        name = next((s["name"] for s in universe if s["symbol"] == sym), sym)
        tag = "ETF" if sym in universe_syms else "급등주"
        holdings.append({
            "symbol": sym,
            "name": name,
            "tag": tag,
            "qty": qty,
            "current_price": cur_price,
            "value": value,
        })

    total_value = cash + holdings_value

    # 기존 포트폴리오 데이터 로드 (히스토리 유지)
    existing = {}
    if PORTFOLIO_PATH.exists():
        with PORTFOLIO_PATH.open("r", encoding="utf-8") as f:
            existing = json.load(f)

    daily_history = existing.get("daily_history", [])

    # 오늘의 히스토리 추가
    today_str = datetime.now().strftime("%Y-%m-%d")
    prev_value = daily_history[-1]["total_value"] if daily_history else 500000
    day_pnl = total_value - prev_value
    cumul_pnl = total_value - 500000

    # 중복 방지
    if not daily_history or daily_history[-1].get("date") != today_str:
        daily_history.append({
            "date": today_str,
            "total_value": total_value,
            "cash": cash,
            "holdings_value": holdings_value,
            "day_pnl": day_pnl,
            "cumul_pnl": cumul_pnl,
        })

    # 승패 계산
    todays_trades = get_todays_trades()
    sell_trades = [t for t in todays_trades if t.get("side") == "sell"]
    buy_trades = [t for t in todays_trades if t.get("side") == "buy"]

    return {
        "updated_at": datetime.now().isoformat(),
        "initial_capital": 500000,
        "cash": cash,
        "holdings": holdings,
        "holdings_value": holdings_value,
        "total_value": total_value,
        "total_pnl": summary["pnl"],
        "total_pnl_pct": round(summary["pnl_pct"], 2),
        "total_trades": summary["total_trades"],
        "winning_trades": existing.get("winning_trades", 0),
        "losing_trades": existing.get("losing_trades", 0),
        "win_rate": existing.get("win_rate", 0),
        "daily_history": daily_history,
        "strategies": {
            "etf_breakout": {
                "name": "ETF 변동성 돌파",
                "allocation": "60%",
                "params": {"k": params.get("k", 0.5), "trend_ma": params.get("trend_ma", 20)},
                "trades": existing.get("strategies", {}).get("etf_breakout", {}).get("trades", 0),
                "pnl": existing.get("strategies", {}).get("etf_breakout", {}).get("pnl", 0),
            },
            "surge_scalp": {
                "name": "급등주 단타",
                "allocation": "40%",
                "trades": existing.get("strategies", {}).get("surge_scalp", {}).get("trades", 0),
                "pnl": existing.get("strategies", {}).get("surge_scalp", {}).get("pnl", 0),
            },
        },
    }


def generate_daily_note(portfolio: dict) -> str:
    """일일 투자 노트 마크다운 생성."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    trades = get_todays_trades()
    history = portfolio.get("daily_history", [])
    today_entry = history[-1] if history else {}

    day_pnl = today_entry.get("day_pnl", 0)
    total_value = portfolio["total_value"]
    pnl_pct = round(day_pnl / (total_value - day_pnl) * 100, 2) if (total_value - day_pnl) > 0 else 0

    # Front matter
    summary_data = {
        "start_value": f"{total_value - day_pnl:,}원",
        "end_value": f"{total_value:,}원",
        "day_pnl": f"{day_pnl:+,}원",
        "trades": len(trades),
    }

    lines = [
        "---",
        f'title: "{today} 투자 일지"',
        f"date: {now.strftime('%Y-%m-%d %H:%M:%S')} +0900",
        f"pnl: {pnl_pct}",
        "summary:",
        f'  start_value: "{summary_data["start_value"]}"',
        f'  end_value: "{summary_data["end_value"]}"',
        f'  day_pnl: "{summary_data["day_pnl"]}"',
        f"  trades: {len(trades)}",
        "---",
        "",
    ]

    # 거래 내역
    lines.append("## 거래 내역")
    lines.append("")
    if trades:
        lines.append("| 시각 | 종목 | 매매 | 수량 | 가격 | 금액 |")
        lines.append("|---|---|---|---|---|---|")
        for t in trades:
            ts = t.get("timestamp", "")[-8:]  # HH:MM:SS
            side_kr = "매수" if t["side"] == "buy" else "매도"
            lines.append(
                f"| {ts} | {t.get('name', t['symbol'])} | {side_kr} "
                f"| {t['qty']}주 | {int(t['price']):,}원 | {int(t['amount']):,}원 |"
            )
    else:
        lines.append("거래 없음 (돌파/급등 신호 미발생 또는 장 휴일)")
    lines.append("")

    # 포트폴리오 현황
    lines.append("## 포트폴리오 현황")
    lines.append("")
    lines.append(f"- 총 자산: **{total_value:,}원**")
    lines.append(f"- 현금: {portfolio['cash']:,}원")
    lines.append(f"- 보유 평가: {portfolio['holdings_value']:,}원")
    lines.append(f"- 누적 수익: {portfolio['total_pnl']:+,}원 ({portfolio['total_pnl_pct']:+.2f}%)")
    lines.append("")

    if portfolio["holdings"]:
        lines.append("### 보유 종목")
        lines.append("")
        for h in portfolio["holdings"]:
            lines.append(f"- {h['name']} ({h['tag']}): {h['qty']}주 @ {h['current_price']:,}원")
        lines.append("")

    # 전략 파라미터
    lines.append("## 전략 설정")
    lines.append("")
    params = portfolio["strategies"]["etf_breakout"]["params"]
    lines.append(f"- ETF 변동성 돌파: K={params['k']}, MA={params['trend_ma']}")
    lines.append(f"- 급등주 단타: KIS 등락률 순위 + TA 복합 분석")
    lines.append(f"- 자본 배분: ETF 60% / 급등주 40%")
    lines.append("")

    # 평가 섹션 (수동 편집 가능)
    lines.append("## 좋았던 점")
    lines.append("")
    if trades:
        buy_count = sum(1 for t in trades if t["side"] == "buy")
        sell_count = sum(1 for t in trades if t["side"] == "sell")
        if sell_count > 0 and day_pnl > 0:
            lines.append(f"- 당일 수익 실현: {day_pnl:+,}원")
        if buy_count > 0:
            lines.append(f"- TA 분석 통과 종목 {buy_count}건 매수 실행")
        lines.append("- 봇 정상 작동 확인")
    else:
        lines.append("- 무리한 진입 없이 현금 보유 (신호 대기)")
    lines.append("")

    lines.append("## 문제점 / 개선사항")
    lines.append("")
    if not trades:
        lines.append("- 거래 미발생 — 돌파 기준 또는 TA 임계값 검토 필요 여부 확인")
    else:
        lines.append("- (자동 생성 — 수동으로 보완 가능)")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    print(f"[{now:%Y-%m-%d %H:%M}] 투자 일지 생성")

    client = KISClient()

    # 1. 포트폴리오 JSON 업데이트
    portfolio = build_portfolio_json(client)
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PORTFOLIO_PATH.open("w", encoding="utf-8") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)
    print(f"  portfolio.json 업데이트 완료")

    # 2. 일일 노트 생성
    note = generate_daily_note(portfolio)
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    note_path = POSTS_DIR / f"{today}-daily-note.md"
    with note_path.open("w", encoding="utf-8") as f:
        f.write(note)
    print(f"  {note_path.name} 생성 완료")

    # 3. 요약 출력
    print(f"  총 자산: {portfolio['total_value']:,}원")
    print(f"  당일 PnL: {portfolio['daily_history'][-1].get('day_pnl', 0):+,}원")
    print(f"  누적 PnL: {portfolio['total_pnl']:+,}원 ({portfolio['total_pnl_pct']:+.2f}%)")


if __name__ == "__main__":
    main()
