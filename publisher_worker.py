"""Publisher daemon — P3.6. Ears on the approval topic, hands on the senders.

    python publisher_worker.py            # consume human.approval.approved
    python publisher_worker.py --drain    # publish everything already queued
    python publisher_worker.py --check    # print limits and dry-run state, send nothing

`--drain` exists because an approval is durable in the decision log and the
Kafka event is only a notification. If the broker was down, or this consumer
was not running, or a human approved from another process, the queued drafts are
still queued and still approved; the notification is not the permission and so
it is not the only way to act on one.

Everything about the send decision lives in app/services/publisher.py. This file
is signal handling and a subscription.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.services.publisher import (
    build_publisher,
    configured_limits,
    dry_run_enabled,
)
from bus import consume, install_signal_handlers
from events import Event

APPROVED_TOPIC = "human.approval.approved"
GROUP_ID = "publisher"


def handle(event: Event, topic: str) -> None:
    """Act on one approval notification.

    Only the goal is taken from the event, never the content: the payloads a
    caller could put in a message are exactly the payloads that must not be
    trusted. What gets sent is read back from the outbox and re-verified against
    the decision log by the publisher.
    """
    goal_id = str(event.payload.get("goal_id") or event.task_id or "")
    publisher = build_publisher()
    if goal_id:
        attempts = publisher.publish_goal(goal_id)
    else:
        attempts = publisher.publish_pending()
    for attempt in attempts:
        logging.info("publisher %s %s", attempt.status, attempt.item_id)


def _check() -> int:
    from app.services import approval

    limits = configured_limits()
    publisher = build_publisher()
    print(f"PUBLISHER_DRY_RUN     : {dry_run_enabled()}")
    print(f"daily limits          : {json.dumps(limits)}")
    print(f"used today            : "
          f"{json.dumps({bucket: publisher.used(bucket) for bucket in limits})}")
    pending = [item for item in approval.list_outbox() if not item.get("published")]
    print(f"queued in outbox      : {len(pending)}")
    for item in pending[:20]:
        publish = item.get("publish") or {}
        print(
            f"  - {item['item_id'][:64]}  {publish.get('channel')} -> "
            f"{publish.get('recipient') or '(broadcast)'}"
        )
    if dry_run_enabled():
        print("\nDry-run: a real send needs PUBLISHER_DRY_RUN=false and the channel's key.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="publisher_worker.py", description=__doc__)
    parser.add_argument("--drain", action="store_true", help="publish everything queued, no Kafka")
    parser.add_argument("--check", action="store_true", help="print state and send nothing")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.check:
        return _check()

    if args.drain:
        publisher = build_publisher()
        for attempt in publisher.publish_pending():
            print(attempt.to_line())
        return 0

    install_signal_handlers("publisher")
    print(f"[publisher] listening on '{APPROVED_TOPIC}' (group={GROUP_ID}, dry_run={dry_run_enabled()})")
    consume([APPROVED_TOPIC], GROUP_ID, handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
