"""환경변수 + 설정 로드.

.env 파일을 읽어 타입 검증된 Settings 객체로 노출한다.
실전·모의투자 분기는 MODE 환경변수로 결정.

이 모듈은 사실상 모든 코드가 import하므로, 프로세스 타임존을 KST로 고정하는
안전장치도 여기에 둔다 (아래 _ensure_kst_timezone 참고).
"""

from __future__ import annotations

import os
import time as _time
from enum import Enum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _ensure_kst_timezone() -> None:
    """프로세스 타임존을 Asia/Seoul로 고정 (이미 지정돼 있으면 존중).

    코드 곳곳에 timezone-naive `datetime.now()`가 남아 있고, 이들이 KST를
    반환하는 유일한 근거는 워크플로의 `env: TZ=Asia/Seoul` 한 줄이었다.
    새 워크플로에서 그 줄을 빠뜨리면 예외 하나 없이 전 시스템이 9시간 밀린다
    (일일 손실 한도·쿨다운·리포트 날짜가 전부 어긋남).

    신규 코드는 `src.utils.clock`의 now_kst()/today_kst()를 쓰는 게 원칙이고,
    이건 남아 있는 naive 호출부에 대한 2차 방어선이다.
    """
    os.environ.setdefault("TZ", "Asia/Seoul")
    if hasattr(_time, "tzset"):      # POSIX 전용 (Windows에는 없음)
        _time.tzset()


_ensure_kst_timezone()


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mode: Mode = Mode.PAPER

    kis_appkey: str = ""
    kis_appsecret: str = ""
    kis_virtual_appkey: str = ""
    kis_virtual_appsecret: str = ""

    kis_htsid: str = ""
    kis_account_no: str = ""
    kis_account_prod_code: str = "01"

    kis_live_url: str = "https://openapi.koreainvestment.com:9443"
    kis_paper_url: str = "https://openapivts.koreainvestment.com:29443"

    kis_rate_limit_live: int = 18
    kis_rate_limit_paper: int = 2

    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    log_level: str = "INFO"
    log_dir: Path = Field(default=Path("./logs"))

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE

    @property
    def base_url(self) -> str:
        return self.kis_live_url if self.is_live else self.kis_paper_url

    @property
    def appkey(self) -> str:
        return self.kis_appkey if self.is_live else self.kis_virtual_appkey

    @property
    def appsecret(self) -> str:
        return self.kis_appsecret if self.is_live else self.kis_virtual_appsecret

    @property
    def rate_limit(self) -> int:
        return self.kis_rate_limit_live if self.is_live else self.kis_rate_limit_paper

    @property
    def account_full(self) -> str:
        return f"{self.kis_account_no}-{self.kis_account_prod_code}"

    def validate_runtime(self) -> None:
        missing: list[str] = []
        if not self.appkey:
            missing.append("KIS_APPKEY" if self.is_live else "KIS_VIRTUAL_APPKEY")
        if not self.appsecret:
            missing.append("KIS_APPSECRET" if self.is_live else "KIS_VIRTUAL_APPSECRET")
        if not self.kis_account_no:
            missing.append("KIS_ACCOUNT_NO")
        if missing:
            raise RuntimeError(
                f"환경변수 누락: {', '.join(missing)}. .env 파일을 확인하세요."
            )


settings = Settings()
