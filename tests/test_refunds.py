"""Refunds when a report fails.

The billing gap P2.1 left open: the customer paid, the lane never delivered,
and the credit was gone. These cover both failure paths (listener escalation
and reaper timeout) and — most importantly — that the two cannot both refund
the same report.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import report_listener  # noqa: E402
import report_reaper  # noqa: E402

from app.database import get_sessionmaker  # noqa: E402
from app.models import Report  # noqa: E402
from app.schemas import ReportResponse, ResultStatus  # noqa: E402
from app.services.billing import (  # noqa: E402
    REPORT_COST_CREDITS,
    create_account,
    deduct_credits,
    get_balance,
    refund_report,
)
from app.services.report_store import (  # noqa: E402
    LEGACY_ACCOUNT_ID,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    get_report,
    new_report_id,
    settle_if_pending,
)

ACCOUNT = "acct-refund"
START = 10


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def funded_account() -> str:
    run(create_account(ACCOUNT, START))
    return ACCOUNT


def _body(report_id: str) -> dict:
    return ReportResponse(
        report_id=report_id, status=ResultStatus.CURATED, summary="pending"
    ).model_dump(mode="json")


def paid_report(account_id: str = ACCOUNT, age_seconds: float = 0.0) -> str:
    """A report that has been paid for, exactly as POST /v1/reports leaves it."""
    report_id = new_report_id()
    run(deduct_credits(account_id, REPORT_COST_CREDITS))
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)

    async def _insert() -> None:
        async with get_sessionmaker()() as session:
            async with session.begin():
                session.add(
                    Report(
                        id=report_id,
                        account_id=account_id,
                        status=STATUS_PENDING,
                        payload_json=json.dumps(_body(report_id)),
                        created_at=created,
                        updated_at=created,
                    )
                )

    run(_insert())
    return report_id


# --- the report row now knows who paid -----------------------------------


def test_report_row_records_the_paying_account() -> None:
    report_id = paid_report()
    assert run(get_report(report_id)).account_id == ACCOUNT


# --- refund_report --------------------------------------------------------


def test_refund_report_credits_the_account() -> None:
    report_id = paid_report()
    assert run(get_balance(ACCOUNT)) == START - 1

    assert run(refund_report(report_id, ACCOUNT, "test")) is True
    assert run(get_balance(ACCOUNT)) == START


def test_refund_report_skips_legacy_rows() -> None:
    """Pre-billing reports were never charged, so refunding would mint credits."""
    assert run(refund_report("R-old", LEGACY_ACCOUNT_ID, "test")) is False


def test_refund_report_skips_a_missing_account() -> None:
    assert run(refund_report("R-x", "no-such-account", "test")) is False


def test_refund_report_skips_an_empty_account_id() -> None:
    assert run(refund_report("R-x", "", "test")) is False


# --- the reaper path ------------------------------------------------------


STALE = report_reaper.STALE_AFTER_SECONDS + 60


def test_reaped_report_refunds_the_customer() -> None:
    """The acceptance case: pay, lane times out, balance restored."""
    report_id = paid_report(age_seconds=STALE)
    assert run(get_balance(ACCOUNT)) == START - 1

    assert run(report_reaper.reap_once()) == [report_id]

    assert run(get_report(report_id)).status == STATUS_FAILED
    assert run(get_balance(ACCOUNT)) == START, "the timed-out report was not refunded"


def test_reaping_several_refunds_each() -> None:
    ids = [paid_report(age_seconds=STALE) for _ in range(3)]
    assert run(get_balance(ACCOUNT)) == START - 3

    assert sorted(run(report_reaper.reap_once())) == sorted(ids)
    assert run(get_balance(ACCOUNT)) == START


def test_a_fresh_pending_report_is_not_refunded() -> None:
    paid_report(age_seconds=0)
    run(report_reaper.reap_once())
    assert run(get_balance(ACCOUNT)) == START - 1


# --- the listener path ----------------------------------------------------


def test_escalated_report_refunds_the_customer() -> None:
    report_id = paid_report()

    report_listener.handle(
        report_listener.approval_event(report_id)
        if hasattr(report_listener, "approval_event")
        else _approval(report_id),
        report_listener.APPROVAL_TOPIC,
    )

    assert run(get_report(report_id)).status == STATUS_FAILED
    assert run(get_balance(ACCOUNT)) == START


def _approval(report_id: str, reason: str = "step_stalled"):
    from events import Event

    return Event(
        event_type="human.approval.required",
        task_id=report_id,
        agent="orchestrator",
        payload={"goal_id": report_id, "reason": reason},
    )


def _completed(report_id: str, summary: str = "done"):
    from events import Event

    return Event(
        event_type="goal.completed",
        task_id=report_id,
        agent="orchestrator",
        payload={"goal_id": report_id, "summary": summary},
    )


def test_a_successful_report_is_not_refunded() -> None:
    """The customer got what they paid for."""
    report_id = paid_report()

    report_listener.handle(_completed(report_id), report_listener.COMPLETED_TOPIC)

    assert run(get_report(report_id)).status == STATUS_READY
    assert run(get_balance(ACCOUNT)) == START - 1


# --- idempotency: the part that would quietly mint money ------------------


def test_duplicate_failure_events_refund_only_once() -> None:
    """Kafka delivery is at-least-once, so the same event can arrive twice."""
    report_id = paid_report()

    report_listener.handle(_approval(report_id), report_listener.APPROVAL_TOPIC)
    report_listener.handle(_approval(report_id), report_listener.APPROVAL_TOPIC)
    report_listener.handle(_approval(report_id), report_listener.APPROVAL_TOPIC)

    assert run(get_balance(ACCOUNT)) == START, "refunded more than once"


def test_reaper_and_listener_cannot_both_refund() -> None:
    """A slow report can be reaped moments before its event arrives."""
    report_id = paid_report(age_seconds=STALE)

    run(report_reaper.reap_once())
    report_listener.handle(_approval(report_id), report_listener.APPROVAL_TOPIC)

    assert run(get_balance(ACCOUNT)) == START, "double refund across daemons"


def test_a_reaped_report_is_not_reaped_again() -> None:
    paid_report(age_seconds=STALE)

    run(report_reaper.reap_once())
    assert run(report_reaper.reap_once()) == []
    assert run(get_balance(ACCOUNT)) == START


def test_listener_cannot_mark_a_ready_report_failed() -> None:
    """Once settled, a late escalation must not flip it back or refund."""
    report_id = paid_report()
    report_listener.handle(_completed(report_id), report_listener.COMPLETED_TOPIC)

    report_listener.handle(_approval(report_id), report_listener.APPROVAL_TOPIC)

    assert run(get_report(report_id)).status == STATUS_READY
    assert run(get_balance(ACCOUNT)) == START - 1


# --- settle_if_pending ----------------------------------------------------


def test_settle_if_pending_returns_the_account_once() -> None:
    report_id = paid_report()

    assert run(settle_if_pending(report_id, STATUS_FAILED, _body(report_id))) == ACCOUNT
    assert run(settle_if_pending(report_id, STATUS_FAILED, _body(report_id))) is None


def test_settle_if_pending_returns_none_for_unknown_report() -> None:
    assert run(settle_if_pending("R-nope", STATUS_FAILED, {})) is None


def test_settle_if_pending_rejects_a_bad_status() -> None:
    report_id = paid_report()
    with pytest.raises(ValueError):
        run(settle_if_pending(report_id, "banana", {}))
