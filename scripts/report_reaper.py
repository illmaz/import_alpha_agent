#!/usr/bin/env python3
"""Daemon: fail reports the agent lane never reported back on.

P1 Part 4 left a liveness hole. A report is published as a goal and marked
pending; if the lane dies after publication, nothing ever settles that row and
it stays pending forever. This is the same invariant Phase 2.5 established for
goals — every goal terminates — reasserted one level up for reports.

Deliberately simple, per the task brief: one query, one update, a sleep. No
state machine, no backoff, no partial recovery. A reaped report is terminal;
the caller resubmits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import install_signal_handlers, wait_for_shutdown  # noqa: E402

from app.database import init_models  # noqa: E402
from app.schemas import ReportStatus, ResultStatus  # noqa: E402
from app.services.billing import refund_report  # noqa: E402
from app.services.report_store import (  # noqa: E402
    STATUS_FAILED,
    list_stale_pending,
    settle_if_pending,
)

# How often to sweep.
INTERVAL_SECONDS = float(os.environ.get("REAP_INTERVAL_SECONDS", "60"))

# How long a report may stay pending before it is considered abandoned. Must
# comfortably exceed the orchestrator's own budget: a goal can burn
# MAX_REPLANS planning attempts plus STEP_TTL_SECONDS (default 120s) per
# stall, so a healthy-but-slow goal would otherwise be reaped mid-flight.
STALE_AFTER_SECONDS = float(os.environ.get("REPORT_STALE_AFTER_SECONDS", "300"))

ERROR_CODE = "agent_lane_timeout"

logger = logging.getLogger("report_reaper")


def _failure_payload(report_id: str, existing_json: str, age_seconds: float) -> dict:
    """Mark the stored body failed, preserving whatever is already there.

    The brief said to write `{"error": "agent_lane_timeout"}`. Writing *only*
    that would make `GET /v1/reports/{id}` return 500: the endpoint validates
    the stored payload as a ReportResponse, which forbids unknown fields and
    requires report_id. A caller would get an opaque server error instead of
    the failure we are trying to tell them about.

    So the error is recorded the way the listener records an escalation — the
    body is kept, `report_status` goes to `failed`, and the summary carries
    the error code — and the row stays readable.
    """
    try:
        payload = json.loads(existing_json or "{}")
    except ValueError:
        payload = {}

    if not isinstance(payload, dict) or not payload.get("report_id"):
        # Created but never filled in, or unparseable. Write the minimum that
        # still validates so the row can be served rather than 500.
        payload = {
            "report_id": report_id,
            "status": ResultStatus.CURATED.value,
            "opportunities": [],
        }

    payload["report_status"] = ReportStatus.FAILED.value
    payload["summary"] = (
        f"{ERROR_CODE}: the agent lane did not report back within "
        f"{STALE_AFTER_SECONDS:.0f}s (pending for {age_seconds:.0f}s). "
        f"Resubmit the request."
    )
    return payload


async def reap_once(now: datetime | None = None) -> list[str]:
    """Fail every pending report older than the threshold. Returns their ids."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=STALE_AFTER_SECONDS)

    stale = await list_stale_pending(cutoff)
    reaped: list[str] = []

    for row in stale:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds()

        try:
            # Conditional on the row still being pending, so a report the
            # listener settled between the query and now is left alone and
            # cannot be refunded twice.
            account_id = await settle_if_pending(
                row.id, STATUS_FAILED, _failure_payload(row.id, row.payload_json, age)
            )
        except Exception:
            # One bad row must not abort the sweep.
            logger.exception("could not reap %s", row.id)
            continue

        if account_id is None:
            logger.info("%s was settled before this sweep reached it", row.id)
            continue

        reaped.append(row.id)
        print(f"[report-reaper] {row.id}: FAILED ({ERROR_CODE}, pending {age:.0f}s)")

        try:
            await refund_report(row.id, account_id, ERROR_CODE)
        except Exception:
            # The report is already failed; a refund failure must not undo
            # that or stop the rest of the sweep. Loud, because it is money.
            logger.exception("REFUND FAILED for %s (account %s)", row.id, account_id)

    return reaped


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    install_signal_handlers("report-reaper")
    asyncio.run(init_models())
    print(
        f"[report-reaper] sweeping every {INTERVAL_SECONDS:.0f}s for reports "
        f"pending longer than {STALE_AFTER_SECONDS:.0f}s"
    )

    while True:
        try:
            reaped = asyncio.run(reap_once())
            if not reaped:
                logger.debug("nothing to reap")
        except Exception:
            # A crash here silently disables the guard, which is the failure
            # this daemon exists to prevent. Log and keep sweeping.
            logger.exception("sweep failed")

        # Not time.sleep: a signal arriving one second into a 60s interval
        # would otherwise wait out the remaining 59 and be SIGKILLed first.
        if wait_for_shutdown(INTERVAL_SECONDS):
            break

    print("[report-reaper] shutdown complete")


if __name__ == "__main__":
    main()
