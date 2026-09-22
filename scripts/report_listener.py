#!/usr/bin/env python3
"""Daemon: settle report rows when the agent lane finishes a goal.

Consumes `goal.completed` and `human.approval.required`. The orchestrator
adopts the API's `task_id` as its `goal_id` and echoes it on both topics, so
the goal_id *is* the report_id for goals the API raised. Anything else on
those topics (a CLI-submitted goal, for instance) has no matching row and is
skipped.

Deliberately simple, per the task brief: no retries, no dead-letter queue. A
malformed event is logged and dropped rather than crashing the daemon, because
one bad event must not stop every later report from settling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import consume, install_signal_handlers  # noqa: E402
from events import Event  # noqa: E402

from app.database import init_models  # noqa: E402
from app.schemas import ReportStatus  # noqa: E402
from app.services.billing import refund_report  # noqa: E402
from app.services.report_store import (  # noqa: E402
    STATUS_FAILED,
    STATUS_READY,
    get_report,
    settle_if_pending,
)

COMPLETED_TOPIC = "goal.completed"
APPROVAL_TOPIC = "human.approval.required"
GROUP_ID = "report-listener"

logger = logging.getLogger("report_listener")


def _report_id(event: Event) -> str | None:
    """The report id this event settles, if any.

    `goal_id` is the correlation key (see module docstring); `report_id` is
    accepted too so a future producer that does echo the payload still works.
    """
    payload = event.payload or {}
    return payload.get("report_id") or payload.get("goal_id") or event.task_id


async def _settle(report_id: str, status: str, summary: str, reason: str) -> bool:
    """Write the outcome onto the stored report body. Returns False if no row.

    A failure refunds the customer's credit — but only if *this* call won the
    pending -> failed transition. Kafka is at-least-once, so the same
    `goal.completed` can arrive twice, and the reaper may have already failed
    a slow report; without that guard the account would be credited twice.
    """
    row = await get_report(report_id)
    if row is None:
        return False

    payload = json.loads(row.payload_json or "{}")
    if not payload:
        # Nothing to merge into. Record the status so the row does not sit
        # pending forever, even though the body is missing.
        payload = {"report_id": report_id, "status": "curated"}

    payload["report_status"] = (
        ReportStatus.READY.value if status == STATUS_READY else ReportStatus.FAILED.value
    )
    payload["summary"] = summary

    account_id = await settle_if_pending(report_id, status, payload)
    if account_id is None:
        # Already settled by the reaper or a duplicate delivery.
        logger.info("%s was already settled; skipping", report_id)
        return False

    if status == STATUS_FAILED:
        await refund_report(report_id, account_id, reason)
    return True


def handle(event: Event, topic: str) -> None:
    """Consumer callback. Runs on the bus thread, so it owns its event loop."""
    report_id = _report_id(event)
    if not report_id:
        logger.warning("event on %s carries no correlation id; skipping", topic)
        return

    if topic == COMPLETED_TOPIC:
        status = STATUS_READY
        reason = "goal.completed"
        summary = str(event.payload.get("summary", "")).strip() or "Goal completed."
    else:
        status = STATUS_FAILED
        reason = str(event.payload.get("reason", "unknown"))
        summary = f"Generation halted by the agent lane: {reason}."

    try:
        settled = asyncio.run(_settle(report_id, status, summary, reason))
    except Exception:
        # One bad event must not stop every later report from settling.
        logger.exception("failed to settle %s from %s", report_id, topic)
        return

    if settled:
        print(f"[report-listener] {report_id}: {status} <- '{topic}'")
    else:
        # Expected for CLI-submitted goals, which have no report row.
        print(f"[report-listener] {report_id}: no report row (not an API goal), skipped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    install_signal_handlers("report-listener")
    asyncio.run(init_models())
    print(
        f"[report-listener] listening on '{COMPLETED_TOPIC}' and "
        f"'{APPROVAL_TOPIC}' (group={GROUP_ID})"
    )
    consume([COMPLETED_TOPIC, APPROVAL_TOPIC], GROUP_ID, handle)
    print("[report-listener] shutdown complete")


if __name__ == "__main__":
    main()
