"""VPS 셋업 검증(preflight) — 라이브 켜기 전 'live가 안전한가'를 읽기 전용으로 점검.

각 항목 PASS/FAIL + 종합 go/no-go. **주문은 절대 안 함.** VPS에 올린 뒤
systemd 타이머를 켜기 전에 `python scripts/preflight.py`로 확인.

종료코드: 0=모두 통과(live 준비됨), 1=실패 항목 있음.
"""
from __future__ import annotations

import sys
from pathlib import Path

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> bool:
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"예외: {e}"
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")
    return ok


def _cfg():
    from src.config import settings
    miss = [k for k, v in {
        "KIS_APPKEY": settings.kis_appkey, "KIS_APPSECRET": settings.kis_appsecret,
        "KIS_ACCOUNT_NO": settings.kis_account_no}.items() if not v]
    if miss:
        return False, f".env 누락: {', '.join(miss)}"
    return True, f"mode={settings.mode.value}, 계좌 {settings.kis_account_no[:4]}**** 로드됨"


def _token():
    from src.kis_auth import get_token
    t = get_token()
    return (bool(t.access_token), "KIS 토큰 발급 OK" if t.access_token else "토큰 빈값")


def _balance():
    from src.kis_client import KISClient
    r = KISClient().get_balance()
    rt = r.get("rt_cd")
    return (rt == "0", f"잔고 조회 rt_cd={rt} ({r.get('msg1','')[:40]})")


def _price():
    from src.kis_client import KISClient
    r = KISClient().get_price("069500")  # KODEX 200
    rt = r.get("rt_cd")
    return (rt == "0", f"시세 조회(069500) rt_cd={rt}")


def _journal():
    p = Path("journal/.git")
    if not p.exists():
        return False, "journal/ 클론 없음 — setup.sh 재실행 또는 git clone 필요"
    return True, "journal repo 클론 확인(영속화 가능)"


def _decision_pipeline():
    from src.bot.single_run import compute_current_day_plan, evaluate_regime
    from src.kis_client import KISClient
    dp = compute_current_day_plan()
    rr, _, _ = evaluate_regime(KISClient())
    stance = (dp or {}).get("stance", "?")
    regime = getattr(rr, "regime", "?") if rr else "?"
    return (bool(dp), f"결정 파이프라인 OK — 레짐 {regime}, 스탠스 {stance}")


def main() -> None:
    print("=" * 56)
    print("  KIS AutoTrader — VPS Preflight (읽기 전용, 주문 없음)")
    print("=" * 56)
    check("1. .env / 인증정보", _cfg)
    check("2. KIS 토큰 발급", _token)
    check("3. 잔고 조회(연결)", _balance)
    check("4. 시세 조회", _price)
    check("5. journal 영속화 경로", _journal)
    check("6. 결정 파이프라인", _decision_pipeline)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("=" * 56)
    if passed == total:
        print(f"  ✅ ALL PASS ({passed}/{total}) — live 가동 준비 완료.")
        print("     systemd 타이머 켜기: sudo systemctl enable --now kis-kr.timer kis-us.timer")
        sys.exit(0)
    else:
        fails = [n for n, ok, _ in CHECKS if not ok]
        print(f"  ⛔ {total-passed}건 실패 ({', '.join(fails)}) — live 켜지 마세요. 위 사유 해결 후 재실행.")
        sys.exit(1)


if __name__ == "__main__":
    main()
