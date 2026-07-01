"""읽기전용: 주요 종목의 일중 변동폭 통계 — 5~10% 익절이 현실적인지 판단용.

각 종목 최근 일봉에서:
  - 일중 레인지 (고가-저가)/시가 %
  - 시가 대비 최대 상방 (고가-시가)/시가 %  ← 완벽 타이밍 롱이 잡을 수 있는 상한
  - 시가 대비 최대 하방 (시가-저가)/시가 %  ← 완벽 타이밍 숏(인버스 원지수) 상한
  - 5%/10% 이상 레인지가 며칠에 한 번 나오는지
주문 없음.
"""
from src.kis_client import KISClient
from src.bot.runner import fetch_recent_history

SYMS = [
    ("069500", "KODEX 200(지수 1x)"),
    ("114800", "KODEX 인버스(1x)"),
    ("091160", "KODEX 반도체"),
    ("005930", "삼성전자"),
    ("122630", "KODEX 레버리지(2x)"),
]


def pct(a, b):
    return (a / b * 100.0) if b else 0.0


def main():
    client = KISClient()
    for sym, name in SYMS:
        try:
            h = fetch_recent_history(client, sym, days=40)
            if h is None or len(h) < 10:
                print(f"[{sym}] {name}: 데이터 부족")
                continue
            h = h.tail(30)
            rng = ((h["high"] - h["low"]) / h["open"] * 100.0)
            up = ((h["high"] - h["open"]) / h["open"] * 100.0)   # 시가->고가
            dn = ((h["open"] - h["low"]) / h["open"] * 100.0)    # 시가->저가
            n = len(h)
            ge5 = int((rng >= 5).sum())
            ge10 = int((rng >= 10).sum())
            up_ge5 = int((up >= 5).sum())
            print(f"[{sym}] {name}  (최근 {n}일)")
            print(f"   일중레인지 평균 {rng.mean():.2f}% / 최대 {rng.max():.2f}%")
            print(f"   시가->고가 평균 {up.mean():.2f}% / 최대 {up.max():.2f}%  "
                  f"(롱이 완벽타이밍 시 잡는 상한)")
            print(f"   시가->저가 평균 {dn.mean():.2f}% / 최대 {dn.max():.2f}%")
            print(f"   레인지 5%이상 {ge5}/{n}일, 10%이상 {ge10}/{n}일, "
                  f"시가대비 +5%이상 도달 {up_ge5}/{n}일")
        except Exception as e:
            print(f"[{sym}] {name}: 오류 {e}")


if __name__ == "__main__":
    main()
