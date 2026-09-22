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
def clean_reports(test_database: Path) -> Iterator[None]:
    """Empty the reports table around each test so ids cannot leak between them."""
    from app.services.report_store import clear_reports

    asyncio.run(clear_reports())
    yield
    asyncio.run(clear_reports())
