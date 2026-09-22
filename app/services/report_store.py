"""Report persistence, backed by SQLite.

Replaces the P1 Part 2 in-memory dict. Reports now survive a restart and are
shared by every process pointed at the same file, so the single-replica
constraint recorded in DECISIONS.md is lifted for reads.

Writes are still serialised by SQLite itself: one writer at a time, with a
busy timeout (app.database.BUSY_TIMEOUT_SECONDS) so a concurrent writer waits
for the lock rather than failing. Fine at MVP volume; it is the first thing to
outgrow when write traffic is real.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select, update

from app.database import get_sessionmaker
from app.models import Report
from app.schemas import ReportResponse

# Stamped on rows that predate billing by the account_id migration. Nobody
# was charged for them, so they must never be refunded. The same literal is
# written in alembic/versions/456aca293990_*.py — see that file for why it is
# duplicated rather than imported.
LEGACY_ACCOUNT_ID = "legacy-unknown"

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
VALID_STATUSES = frozenset({STATUS_PENDING, STATUS_READY, STATUS_FAILED})


def new_report_id() -> str:
    return f"R-{uuid.uuid4().hex[:8]}"


async def create_report(report_id: str, account_id: str) -> None:
    """Insert a pending row owned by `account_id`.

    Called before the report body exists so a crash mid-generation leaves a
    visible `pending` row rather than nothing at all. The owner is recorded
    now because the daemon that later fails this report has no other way to
    know whom to refund.
    """
    async with get_sessionmaker()() as session:
        async with session.begin():
            session.add(
                Report(
                    id=report_id,
                    account_id=account_id,
                    status=STATUS_PENDING,
                    payload_json="{}",
                )
            )


async def update_report(report_id: str, status: str, payload: Dict[str, Any]) -> None:
    """Attach the generated body and move the row out of pending.

    Raises:
        ValueError: on an unknown status, or if the row does not exist. A
            silent no-op here would strand a report as pending forever with no
            signal that anything went wrong.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}")

    async with get_sessionmaker()() as session:
        async with session.begin():
            report = await session.get(Report, report_id)
            if report is None:
                raise ValueError(f"no report {report_id!r} to update")
            report.status = status
            report.payload_json = json.dumps(payload, default=str)


async def settle_if_pending(
    report_id: str, status: str, payload: Dict[str, Any]
) -> Optional[str]:
    """Move a report out of `pending`, once. Returns the owning account, or None.

    The return value is the refund authorisation: None means this call did not
    perform the transition, so this caller must not refund.

    This exists because refunds must be idempotent and two things can settle
    the same report. Kafka delivery is at-least-once, so report_listener can
    see the same `goal.completed` twice; and a slow lane can have the reaper
    fail a report moments before the listener's event arrives. A plain
    "set status, then refund" would credit the account twice. Making the
    status change conditional on the row still being pending means exactly one
    caller is told to refund.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}")

    async with get_sessionmaker()() as session:
        async with session.begin():
            result = await session.execute(
                update(Report)
                .where(Report.id == report_id, Report.status == STATUS_PENDING)
                .values(status=status, payload_json=json.dumps(payload, default=str))
            )
            if result.rowcount != 1:
                return None
            row = await session.get(Report, report_id)
            return row.account_id if row is not None else None


async def get_report(report_id: str) -> Optional[Report]:
    """Fetch one row, or None if there is no such id."""
    async with get_sessionmaker()() as session:
        return await session.get(Report, report_id)


async def get_report_response(report_id: str) -> Optional[ReportResponse]:
    """Fetch a row and rehydrate it into the API response model.

    Returns None both when the id is unknown and when the row is still
    `pending` with an empty body — from a caller's point of view there is
    nothing to serve in either case, and the endpoint distinguishes them.
    """
    report = await get_report(report_id)
    if report is None:
        return None
    payload = json.loads(report.payload_json or "{}")
    if not payload:
        return None
    return ReportResponse.model_validate(payload)


async def list_reports(status: Optional[str] = None, limit: int = 50) -> list[Report]:
    """Rows newest first, optionally filtered by status.

    The status filter is what `ix_reports_status` exists for.
    """
    statement = select(Report).order_by(Report.created_at.desc()).limit(limit)
    if status is not None:
        statement = statement.where(Report.status == status)
    async with get_sessionmaker()() as session:
        result = await session.execute(statement)
        return list(result.scalars().all())


async def list_stale_pending(cutoff: datetime) -> list[Report]:
    """Pending rows created before `cutoff`.

    This is the query `ix_reports_status` was added for. Oldest first, so a
    backlog is cleared in the order it accumulated.
    """
    statement = (
        select(Report)
        .where(Report.status == STATUS_PENDING, Report.created_at < cutoff)
        .order_by(Report.created_at.asc())
    )
    async with get_sessionmaker()() as session:
        result = await session.execute(statement)
        return list(result.scalars().all())


async def count_reports() -> int:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(Report.id))
        return len(list(result.scalars().all()))


async def clear_reports() -> None:
    """Delete every row. Test hook."""
    from sqlalchemy import delete

    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(delete(Report))
