"""Shared test fixtures.

Every test that touches the database runs against a throwaway SQLite file, not
`./data/reports.db`. The URL is set before any engine is built and the engine
is reset afterwards, so a test run can never read or write the real database.

A file is used rather than `:memory:` on purpose: an in-memory SQLite database
is scoped to a single connection, which would make the persistence tests pass
for the wrong reason.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def test_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point DATABASE_URL at a temp file for the whole session."""
    db_path = tmp_path_factory.mktemp("db") / "test_reports.db"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    from app.database import init_models, reset_engine

    asyncio.run(reset_engine())
    asyncio.run(init_models())

    yield db_path

    asyncio.run(reset_engine())
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous


@pytest.fixture(autouse=True)
def clean_tables(test_database: Path) -> Iterator[None]:
    """Empty every table around each test.

    Accounts and keys leak between tests as readily as reports do — a test
    that creates "acct-1" would collide with the next one that does, and a
    listing test would see rows it never made.
    """
    from app.services.auth import clear_api_keys
    from app.services.billing import clear_accounts
    from app.services.report_store import clear_reports

    async def _wipe() -> None:
        await clear_reports()
        await clear_api_keys()
        await clear_accounts()

    asyncio.run(_wipe())
    yield
    asyncio.run(_wipe())
