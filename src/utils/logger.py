"""구조화 로깅 — JSON 포맷, 거래 기록·디버깅 양쪽 용도."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

from src.config import settings


def setup_logging() -> structlog.stdlib.BoundLogger:
    """structlog 초기화 + 콘솔/파일 핸들러 설정."""
    log_dir: Path = settings.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=settings.log_level.upper(),
    )

    file_handler = logging.FileHandler(log_dir / "kis.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    return structlog.get_logger()


log = setup_logging()
