"""환경변수 + 설정 로드.

.env 파일을 읽어 타입 검증된 Settings 객체로 노출한다.
실전·모의투자 분기는 MODE 환경변수로 결정.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
