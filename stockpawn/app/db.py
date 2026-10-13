"""DB 엔진·세션. MVP는 SQLite 단일 파일, DATABASE_URL만 바꾸면 PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import config


def make_engine(url: str = config.DATABASE_URL) -> Engine:
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}  # 스케줄러 스레드 + API 스레드 공용
        if url in ("sqlite://", "sqlite:///:memory:"):
            engine = create_engine(url, connect_args=connect_args, poolclass=StaticPool)
        else:
            path = url.removeprefix("sqlite:///")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(url, connect_args=connect_args)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    from app import models  # noqa: F401 — 테이블 등록

    SQLModel.metadata.create_all(engine)


def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session
