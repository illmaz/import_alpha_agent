"""Async SQLAlchemy engine and session factory (SQLite via aiosqlite).

No Alembic yet: `init_models()` runs `create_all` at startup. That is adequate
while the schema is one table and nothing is deployed, and it stops being
adequate the moment a column changes under existing rows — see DECISIONS.md.

The engine is created lazily rather than at import, so tests can point
`DATABASE_URL` at a temporary file and call `reset_engine()` without having to
control module import order.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/reports.db"

# SQLite serialises writers. A busy timeout makes a concurrent writer wait for
# the lock instead of failing immediately with "database is locked", which is
# the difference between a slow request and a 500 under light concurrency.
BUSY_TIMEOUT_SECONDS = 5.0

# Journal mode. DELETE (the SQLite default) rather than WAL, because the
# database lives on a bind-mounted host directory and is opened by more than
# one container. WAL coordinates readers and writers through a shared-memory
# `-shm` file, and that coordination is not reliable across a macOS bind
# mount: it worked for a single process and started returning
# "disk I/O error" as soon as report_listener opened the same file.
#
# DELETE uses ordinary POSIX locks, which the bind mount does handle. Set
# SQLITE_JOURNAL_MODE=WAL where the file is on a real local filesystem (a
# Docker named volume, or native Linux) to get WAL's better read concurrency.
JOURNAL_MODE = os.environ.get("SQLITE_JOURNAL_MODE", "DELETE")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def get_database_url() -> str:
    """Read the URL at call time so tests can redirect it."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def _ensure_parent_dir(url: str) -> None:
    """Create the directory holding the SQLite file.

    aiosqlite will not create a missing parent directory; it raises
    "unable to open database file", which reads like a permissions problem.
    """
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return
    Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_database_url()
        _ensure_parent_dir(url)
        _engine = create_async_engine(
            url,
            echo=False,
            future=True,
            connect_args={"timeout": BUSY_TIMEOUT_SECONDS},
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _sessionmaker


# Kept as the documented name from the task spec; it is a callable factory, so
# `AsyncSessionLocal()` yields a session exactly as the SQLAlchemy docs show.
def AsyncSessionLocal() -> AsyncSession:  # noqa: N802 - conventional factory name
    return get_sessionmaker()()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that always closes."""
    async with get_sessionmaker()() as session:
        yield session


async def init_models() -> None:
    """Create tables if absent. Safe to call on every startup."""
    from app import models  # noqa: F401  - registers the mappers on Base

    engine = get_engine()
    async with engine.begin() as connection:
        # Journal mode is persistent per database file, but is set on every
        # startup so a file created elsewhere still ends up in the mode this
        # deployment expects.
        await connection.exec_driver_sql(f"PRAGMA journal_mode={JOURNAL_MODE}")
        await connection.run_sync(Base.metadata.create_all)


async def reset_engine() -> None:
    """Dispose the engine and drop the cached factory. Test hook."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
