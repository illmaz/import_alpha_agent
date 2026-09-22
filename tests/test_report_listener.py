"""Tests for the report_listener daemon.

The listener is what closes the loop: without it every report raised through
the API stays pending forever. These exercise `handle()` directly with real
Event objects and a real SQLite row — no broker involved.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import report_listener  # noqa: E402

from app.schemas import ReportResponse, ResultStatus  # noqa: E402
from app.services.report_store import (  # noqa: E402
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    create_report,
    get_report,
    get_report_response,
    new_report_id,
    update_report,
)
from events import Event  # noqa: E402


TEST_ACCOUNT = "acct-listener-test"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def pending_report() -> str:
    """A stored, pending report exactly as POST /v1/reports leaves it."""
    report_id = new_report_id()
    run(create_report(report_id, TEST_ACCOUNT))
    body = ReportResponse(
        report_id=report_id,
        category="home_organization",
        summary="Awaiting the agent lane's synthesis.",
        status=ResultStatus.CURATED,
    ).model_dump(mode="json")
    run(update_report(report_id, STATUS_PENDING, body))
    return report_id


def completed_event(goal_id: str, summary: str = "All steps reported an artifact.") -> Event:
    """Shaped exactly as orchestrator._complete publishes it."""
    return Event(
        event_type="goal.completed",
        task_id=goal_id,
        agent="orchestrator",
        payload={
            "goal_id": goal_id,
            "goal": "Produce a report",
            "steps_completed": 2,
            "summary": summary,
        },
    )


def approval_event(goal_id: str, reason: str = "step_stalled") -> Event:
    """Shaped exactly as orchestrator._escalate publishes it."""
    return Event(
        event_type="human.approval.required",
        task_id=goal_id,
        agent="orchestrator",
        payload={"goal_id": goal_id, "reason": reason},
    )


# --- correlation ----------------------------------------------------------


def test_report_id_is_read_from_goal_id() -> None:
    """goal_id is the correlation key, because the orchestrator echoes it."""
    assert report_listener._report_id(completed_event("R-abc12345")) == "R-abc12345"


def test_explicit_report_id_in_payload_wins() -> None:
    event = completed_event("G-goalonly")
    event.payload["report_id"] = "R-explicit"
    assert report_listener._report_id(event) == "R-explicit"


def test_falls_back_to_task_id_when_payload_is_bare() -> None:
    event = Event(event_type="goal.completed", task_id="R-fromtask", payload={})
    assert report_listener._report_id(event) == "R-fromtask"


# --- goal.completed -------------------------------------------------------


def test_completed_event_marks_the_report_ready(pending_report: str) -> None:
    report_listener.handle(completed_event(pending_report), report_listener.COMPLETED_TOPIC)

    row = run(get_report(pending_report))
    assert row.status == STATUS_READY


def test_completed_event_writes_the_synthesis_into_the_body(pending_report: str) -> None:
    report_listener.handle(
        completed_event(pending_report, summary="Two briefs, no sourced datapoints."),
        report_listener.COMPLETED_TOPIC,
    )

    report = run(get_report_response(pending_report))
    assert report.summary == "Two briefs, no sourced datapoints."
    assert report.report_status.value == "ready"


def test_completed_event_preserves_the_rest_of_the_body(pending_report: str) -> None:
    before = run(get_report_response(pending_report))
    report_listener.handle(completed_event(pending_report), report_listener.COMPLETED_TOPIC)
    after = run(get_report_response(pending_report))

    assert after.report_id == before.report_id
    assert after.category == before.category
    assert after.status is ResultStatus.CURATED


def test_empty_summary_falls_back_to_a_default(pending_report: str) -> None:
    report_listener.handle(
        completed_event(pending_report, summary="   "), report_listener.COMPLETED_TOPIC
    )
    assert run(get_report_response(pending_report)).summary == "Goal completed."


# --- human.approval.required ---------------------------------------------


def test_approval_event_marks_the_report_failed(pending_report: str) -> None:
    report_listener.handle(
        approval_event(pending_report, reason="step_stalled"), report_listener.APPROVAL_TOPIC
    )

    row = run(get_report(pending_report))
    assert row.status == STATUS_FAILED

    report = run(get_report_response(pending_report))
    assert report.report_status.value == "failed"
    assert "step_stalled" in report.summary


@pytest.mark.parametrize(
    "reason", ["plan_validation_failed", "max_steps_exceeded", "empty_goal", "step_stalled"]
)
def test_every_escalation_reason_fails_the_report(pending_report: str, reason: str) -> None:
    report_listener.handle(approval_event(pending_report, reason), report_listener.APPROVAL_TOPIC)
    assert run(get_report(pending_report)).status == STATUS_FAILED


# --- events that are not ours --------------------------------------------


def test_goal_with_no_report_row_is_skipped_quietly() -> None:
    """CLI-submitted goals share these topics and must not raise."""
    report_listener.handle(completed_event("G-clionly"), report_listener.COMPLETED_TOPIC)
    assert run(get_report("G-clionly")) is None


def test_event_with_no_correlation_id_is_skipped() -> None:
    event = Event(event_type="goal.completed", payload={})
    report_listener.handle(event, report_listener.COMPLETED_TOPIC)  # must not raise


def test_a_failing_settle_does_not_crash_the_daemon(
    pending_report: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad event must not stop every later report from settling."""

    async def boom(*args, **kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(report_listener, "_settle", boom)
    report_listener.handle(completed_event(pending_report), report_listener.COMPLETED_TOPIC)

    # Still pending, but the daemon survived to process the next event.
    assert run(get_report(pending_report)).status == STATUS_PENDING


def test_settling_a_row_with_an_empty_body_still_records_the_status() -> None:
    """A row must never be left pending just because its body is missing."""
    report_id = new_report_id()
    run(create_report(report_id, TEST_ACCOUNT))

    report_listener.handle(completed_event(report_id), report_listener.COMPLETED_TOPIC)

    row = run(get_report(report_id))
    assert row.status == STATUS_READY
    assert json.loads(row.payload_json)["report_status"] == "ready"
