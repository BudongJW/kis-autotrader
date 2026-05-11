"""인증 + 첫 시세 조회 점검 — 집에서 셋업 직후 가장 먼저 실행할 것.

성공 시: "✅ 토큰 발급 성공, 삼성전자 현재가: XX,XXX원"
실패 시: 친절한 에러 메시지로 어디가 문제인지 알려준다.
"""

from __future__ import annotations

import sys

from src.config import settings
from src.kis_auth import get_token
from src.kis_client import KISClient


def main() -> int:
    print(f"📡 KIS Developers 인증 점검 (mode={settings.mode.value})")
    print(f"   Base URL: {settings.base_url}")
    print(f"   계좌:     {settings.account_full}")

    try:
        settings.validate_runtime()
    except RuntimeError as e:
        print(f"\n❌ {e}")
        print("   → .env 파일을 다시 확인하세요.")
        return 1

    try:
        token = get_token()
        print(f"\n✅ 토큰 발급 성공 (만료: {token.expires_at:%Y-%m-%d %H:%M})")
    except Exception as e:
        print(f"\n❌ 토큰 발급 실패: {e}")
        print("   → 앱키/앱시크릿이 모드(paper/live)와 맞는지 확인.")
        return 1

    client = KISClient()
    try:
        resp = client.get_price("005930")  # 삼성전자
        if resp.get("rt_cd") != "0":
            print(f"\n❌ 시세 조회 실패: {resp.get('msg1')}")
            return 1
        price = resp["output"]["stck_prpr"]
        print(f"✅ 삼성전자 현재가: {int(price):,}원")
    except Exception as e:
        print(f"\n❌ 시세 조회 예외: {e}")
        return 1

    print("\n🎉 모든 점검 통과. 이제 백테스트·봇을 실행해도 됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
