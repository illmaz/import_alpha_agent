"""Alembic migrations.

Two things matter here. First, that the chain runs clean on an empty database.
Second — and this is the one that bites — that the schema the migrations
produce still matches the models. `init_models()` uses `create_all`, so a
model change works in tests and in a fresh dev database *without* a migration,
and the gap only shows up against a database that already has rows.
`test_migrations_match_the_models` is what stops that drift.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[1]


def alembic(*args: str, db_path: Path) -> subprocess.CompletedProcess:
    """Run the alembic CLI against a throwaway database."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(REPO_ROOT),
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        },
        capture_output=True,
        text=True,
    )


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "migrated.db"
    result = alembic("upgrade", "head", db_path=db_path)
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
    return db_path


def inspector_for(db_path: Path):
    # Sync driver: inspection does not need async, and this keeps the test
    # independent of the event loop.
    return inspect(create_engine(f"sqlite:///{db_path}"))


# --- the chain runs -------------------------------------------------------


def test_upgrade_head_succeeds_on_an_empty_database(migrated_db: Path) -> None:
    assert migrated_db.exists()


def test_every_table_is_created(migrated_db: Path) -> None:
    tables = set(inspector_for(migrated_db).get_table_names())
    assert {"reports", "api_keys", "credit_accounts"} <= tables


def test_alembic_version_is_stamped(migrated_db: Path) -> None:
    assert "alembic_version" in inspector_for(migrated_db).get_table_names()


def test_reports_has_account_id_not_null(migrated_db: Path) -> None:
    columns = {c["name"]: c for c in inspector_for(migrated_db).get_columns("reports")}
    assert "account_id" in columns
    assert columns["account_id"]["nullable"] is False


def test_reports_indexes_exist(migrated_db: Path) -> None:
    names = {i["name"] for i in inspector_for(migrated_db).get_indexes("reports")}
    assert {"ix_reports_status", "ix_reports_account_id"} <= names


# --- migrations vs models -------------------------------------------------


def test_migrations_match_the_models(migrated_db: Path) -> None:
    """The guard against create_all and Alembic drifting apart.

    A model changed without a migration passes every other test in the suite,
    because the test database is built by create_all. It fails here.
    """
    from app.database import Base
    from app import models  # noqa: F401  - registers the mappers

    inspector = inspector_for(migrated_db)

    for table_name, table in Base.metadata.tables.items():
        assert table_name in inspector.get_table_names(), (
            f"{table_name} is in the models but no migration creates it"
        )
        migrated_columns = {c["name"] for c in inspector.get_columns(table_name)}
        model_columns = {c.name for c in table.columns}
        missing = model_columns - migrated_columns
        assert not missing, (
            f"{table_name} is missing {sorted(missing)} in the migrations — "
            f"generate one with `alembic revision --autogenerate`"
        )


# --- downgrade ------------------------------------------------------------


def test_downgrade_removes_account_id(migrated_db: Path) -> None:
    result = alembic("downgrade", "-1", db_path=migrated_db)
    assert result.returncode == 0, f"downgrade failed:\n{result.stderr}"

    columns = {c["name"] for c in inspector_for(migrated_db).get_columns("reports")}
    assert "account_id" not in columns


def test_upgrade_is_repeatable_after_downgrade(migrated_db: Path) -> None:
    assert alembic("downgrade", "base", db_path=migrated_db).returncode == 0
    result = alembic("upgrade", "head", db_path=migrated_db)
    assert result.returncode == 0, f"re-upgrade failed:\n{result.stderr}"
