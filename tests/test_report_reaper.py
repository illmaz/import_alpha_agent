"""Tests for the report reaper.

The reaper is the backstop for the liveness hole P1 Part 4 opened: a report
published as a goal that the agent lane never reports back on. These use
artificially old `created_at` timestamps rather than sleeping.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import report_reaper  # noqa: E402

from app.database import get_sessionmaker  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Report  # noqa: E402
from app.schemas import ReportResponse, ResultStatus  # noqa: E402
from app.services.report_store import (  # noqa: E402
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    get_report,
    get_report_response,
    new_report_id,
)


def run(coro):
    return asyncio.run(coro)


def _body(report_id: str) -> dict:
    return ReportResponse(
        report_id=report_id,
        category="home_organization",
        summary="Awaiting the agent lane's synthesis.",
        status=ResultStatus.CURATED,
    ).model_dump(mode="json")


TEST_ACCOUNT = "acct-reaper-test"


def make_report(
    status: str,
    age_seconds: float,
    with_body: bool = True,
    account_id: str = TEST_ACCOUNT,
) -> str:
    """Insert a row with an artificially old created_at."""
    report_id = new_report_id()
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    payload = json.dumps(_body(report_id)) if with_body else "{}"

    async def _insert() -> None:
        async with get_sessionmaker()() as session:
            async with session.begin():
                session.add(
                    Report(
                        id=report_id,
                        account_id=account_id,
                        status=status,
                        payload_json=payload,
                        created_at=created,
                        updated_at=created,
                    )
                )

    run(_insert())
    return report_id


STALE = report_reaper.STALE_AFTER_SECONDS + 60
FRESH = report_reaper.STALE_AFTER_SECONDS - 60


# --- reaps what it should ------------------------------------------------


def test_reaps_a_stale_pending_report() -> None:
    report_id = make_report(STATUS_PENDING, age_seconds=STALE)

    reaped = run(report_reaper.reap_once())

    assert reaped == [report_id]
    assert run(get_report(report_id)).status == STATUS_FAILED


def test_reaped_report_records_the_error_code() -> None:
    report_id = make_report(STATUS_PENDING, age_seconds=STALE)
    run(report_reaper.reap_once())

    report = run(get_report_response(report_id))
    assert report.report_status.value == "failed"
    assert report_reaper.ERROR_CODE in report.summary


def test_reaps_several_at_once_oldest_first() -> None:
    older = make_report(STATUS_PENDING, age_seconds=STALE + 600)
    newer = make_report(STATUS_PENDING, age_seconds=STALE + 10)

    assert run(report_reaper.reap_once()) == [older, newer]


def test_reaping_is_idempotent() -> None:
    """A second sweep finds nothing, because the row is no longer pending."""
    make_report(STATUS_PENDING, age_seconds=STALE)

    assert len(run(report_reaper.reap_once())) == 1
    assert run(report_reaper.reap_once()) == []


# --- leaves alone what it should -----------------------------------------


def test_leaves_a_fresh_pending_report_alone() -> None:
    report_id = make_report(STATUS_PENDING, age_seconds=FRESH)

    assert run(report_reaper.reap_once()) == []
    assert run(get_report(report_id)).status == STATUS_PENDING


def test_leaves_a_ready_report_alone() -> None:
    report_id = make_report(STATUS_READY, age_seconds=STALE)

    assert run(report_reaper.reap_once()) == []
    assert run(get_report(report_id)).status == STATUS_READY


def test_leaves_an_already_failed_report_alone() -> None:
    report_id = make_report(STATUS_FAILED, age_seconds=STALE)

    assert run(report_reaper.reap_once()) == []
    assert run(get_report(report_id)).status == STATUS_FAILED


def test_ready_report_keeps_its_summary() -> None:
    report_id = make_report(STATUS_READY, age_seconds=STALE)
    before = run(get_report_response(report_id)).summary

    run(report_reaper.reap_once())

    assert run(get_report_response(report_id)).summary == before


def test_boundary_report_exactly_at_the_threshold_is_not_reaped() -> None:
    """Strictly older than the cutoff, so one right on it survives."""
    now = datetime.now(timezone.utc)
    report_id = new_report_id()
    created = now - timedelta(seconds=report_reaper.STALE_AFTER_SECONDS)

    async def _insert() -> None:
        async with get_sessionmaker()() as session:
            async with session.begin():
                session.add(
                    Report(
                        id=report_id,
                        account_id=TEST_ACCOUNT,
                        status=STATUS_PENDING,
                        payload_json=json.dumps(_body(report_id)),
                        created_at=created,
                        updated_at=created,
                    )
                )

    run(_insert())
    assert run(report_reaper.reap_once(now=now)) == []


def test_empty_database_sweeps_cleanly() -> None:
    assert run(report_reaper.reap_once()) == []


# --- the reaped row must stay servable -----------------------------------


def test_reaped_report_is_still_readable_over_http() -> None:
    """The reason the payload is merged rather than replaced.

    Writing a bare {"error": ...} body would fail ReportResponse validation on
    read, and GET would answer 500 instead of reporting the failure.
    """
    report_id = make_report(STATUS_PENDING, age_seconds=STALE)
    run(report_reaper.reap_once())

    # /v1 requires a key since P2.1; reading a report is not metered.
    from app.services.auth import issue_api_key
    from app.services.billing import create_account

    run(create_account("acct-reaper", 0))
    key, _ = run(issue_api_key("acct-reaper"))

    with TestClient(app) as client:
        response = client.get(
            f"/v1/reports/{report_id}", headers={"Authorization": f"Bearer {key}"}
        )

    assert response.status_code == 200
    assert response.json()["report_status"] == "failed"
    assert report_reaper.ERROR_CODE in response.json()["summary"]


def test_reaped_report_preserves_the_rest_of_the_body() -> None:
    report_id = make_report(STATUS_PENDING, age_seconds=STALE)
    before = run(get_report_response(report_id))

    run(report_reaper.reap_once())
    after = run(get_report_response(report_id))

    assert after.report_id == before.report_id
    assert after.category == before.category
    assert after.status is ResultStatus.CURATED


def test_row_with_no_body_is_still_reaped_and_servable() -> None:
    """A row created but never filled in must not become an unreadable 500."""
    report_id = make_report(STATUS_PENDING, age_seconds=STALE, with_body=False)

    assert run(report_reaper.reap_once()) == [report_id]

    report = run(get_report_response(report_id))
    assert report.report_id == report_id
    assert report.report_status.value == "failed"


def test_one_bad_row_does_not_abort_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_report(STATUS_PENDING, age_seconds=STALE + 600)
    second = make_report(STATUS_PENDING, age_seconds=STALE + 10)

    real_settle = report_reaper.settle_if_pending
    calls = {"n": 0}

    async def flaky(report_id, status, payload):
        calls["n"] += 1
        if report_id == first:
            raise RuntimeError("row exploded")
        return await real_settle(report_id, status, payload)

    monkeypatch.setattr(report_reaper, "settle_if_pending", flaky)

    reaped = run(report_reaper.reap_once())

    assert reaped == [second], "the sweep must continue past a failing row"
    assert calls["n"] == 2


def test_threshold_exceeds_the_orchestrator_step_ttl() -> None:
    """A healthy-but-slow goal must not be reaped mid-flight.

    The orchestrator allows STEP_TTL_SECONDS (default 120) per stall before it
    replans, so a reaper threshold below that would fail reports the lane was
    still legitimately working on.
    """
    assert report_reaper.STALE_AFTER_SECONDS > 120
