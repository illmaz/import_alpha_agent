"""Report store tests against a real SQLite file.

Covers the store API directly, plus the persistence property the in-memory
implementation could not offer: rows survive the engine being disposed and
rebuilt, which is what a process restart does.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.database import get_database_url, init_models, reset_engine
from app.models import Report
from app.schemas import ReportResponse, ResultStatus
from app.services.report_store import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    clear_reports,
    count_reports,
    create_report,
    get_report,
    get_report_response,
    list_reports,
    new_report_id,
    update_report,
)


TEST_ACCOUNT = "acct-store-test"


def run(coro):
    return asyncio.run(coro)


def _sample_payload(report_id: str) -> dict:
    return ReportResponse(
        report_id=report_id,
        category="home_organization",
        summary="test report",
        status=ResultStatus.CURATED,
    ).model_dump(mode="json")


# --- ids ------------------------------------------------------------------


def test_new_report_id_is_prefixed_and_unique() -> None:
    ids = {new_report_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("R-") for i in ids)


# --- create / read --------------------------------------------------------


def test_create_report_inserts_a_pending_row() -> None:
    rid = new_report_id()
    run(create_report(rid, TEST_ACCOUNT))

    row = run(get_report(rid))
    assert isinstance(row, Report)
    assert row.id == rid
    assert row.status == STATUS_PENDING
    assert json.loads(row.payload_json) == {}


def test_get_report_returns_none_for_unknown_id() -> None:
    assert run(get_report("R-nosuchid")) is None


def test_pending_report_has_no_response_body_yet() -> None:
    rid = new_report_id()
    run(create_report(rid, TEST_ACCOUNT))
    assert run(get_report_response(rid)) is None


def test_created_at_and_updated_at_are_populated() -> None:
    rid = new_report_id()
    run(create_report(rid, TEST_ACCOUNT))
    row = run(get_report(rid))
    assert row.created_at is not None
    assert row.updated_at is not None


# --- update ---------------------------------------------------------------


def test_update_report_stores_the_payload_and_status() -> None:
    rid = new_report_id()
    run(create_report(rid, TEST_ACCOUNT))
    run(update_report(rid, STATUS_READY, _sample_payload(rid)))

    row = run(get_report(rid))
    assert row.status == STATUS_READY
    assert json.loads(row.payload_json)["report_id"] == rid


def test_update_report_rehydrates_into_the_response_model() -> None:
    rid = new_report_id()
    run(create_report(rid, TEST_ACCOUNT))
    run(update_report(rid, STATUS_READY, _sample_payload(rid)))

    report = run(get_report_response(rid))
    assert isinstance(report, ReportResponse)
    assert report.report_id == rid
    assert report.summary == "test report"


def test_update_report_rejects_an_unknown_status() -> None:
    rid = new_report_id()
    run(create_report(rid, TEST_ACCOUNT))
    with pytest.raises(ValueError, match="status must be one of"):
        run(update_report(rid, "banana", {}))


def test_update_report_raises_for_a_missing_row() -> None:
    """A silent no-op would strand a report as pending with no signal."""
    with pytest.raises(ValueError, match="no report"):
        run(update_report("R-nosuchid", STATUS_READY, {}))


def test_failed_status_is_accepted() -> None:
    rid = new_report_id()
    run(create_report(rid, TEST_ACCOUNT))
    run(update_report(rid, STATUS_FAILED, {"report_id": rid}))
    assert run(get_report(rid)).status == STATUS_FAILED


# --- listing and counting -------------------------------------------------


def test_list_reports_filters_by_status() -> None:
    ready, pending = new_report_id(), new_report_id()
    run(create_report(ready, TEST_ACCOUNT))
    run(create_report(pending, TEST_ACCOUNT))
    run(update_report(ready, STATUS_READY, _sample_payload(ready)))

    ready_ids = [r.id for r in run(list_reports(status=STATUS_READY))]
    assert ready_ids == [ready]

    pending_ids = [r.id for r in run(list_reports(status=STATUS_PENDING))]
    assert pending_ids == [pending]


def test_list_reports_respects_limit() -> None:
    for _ in range(5):
        run(create_report(new_report_id(), TEST_ACCOUNT))
    assert len(run(list_reports(limit=3))) == 3


def test_count_and_clear() -> None:
    for _ in range(3):
        run(create_report(new_report_id(), TEST_ACCOUNT))
    assert run(count_reports()) == 3
    run(clear_reports())
    assert run(count_reports()) == 0


# --- persistence ----------------------------------------------------------


def test_reports_survive_an_engine_restart() -> None:
    """The property the in-memory store could not provide.

    Disposing the engine and rebuilding it is what a process restart does to
    the connection pool. The row must still be there afterwards, which also
    proves it reached the file rather than living in a session cache.
    """
    rid = new_report_id()
    run(create_report(rid, TEST_ACCOUNT))
    run(update_report(rid, STATUS_READY, _sample_payload(rid)))

    run(reset_engine())
    run(init_models())

    row = run(get_report(rid))
    assert row is not None, "report did not survive the engine restart"
    assert row.status == STATUS_READY
    assert run(get_report_response(rid)).report_id == rid


def test_tests_never_touch_the_real_database() -> None:
    """Guards the conftest redirection — a regression here would be silent."""
    url = get_database_url()
    assert "test_reports.db" in url
    assert "data/reports.db" not in url
