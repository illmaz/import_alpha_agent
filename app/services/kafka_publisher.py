"""Publish events to Kafka from the FastAPI request path.

`bus.produce` is synchronous and calls `flush()`, which blocks until the
broker acknowledges. Calling it directly from an async handler would stall the
event loop for every other in-flight request, so it runs in a worker thread.

Correlation, and why report_id is not enough on its own
-------------------------------------------------------
The orchestrator takes its `goal_id` from the incoming event's `task_id`
(`orchestrator.handle_goal`), and builds a *fresh* payload for
`goal.completed` — it does not echo the payload it received. A `report_id`
placed only in the payload would therefore never come back.

So the report_id is published as the event's `task_id`. The orchestrator
adopts it as the goal_id and echoes it on both `goal.completed` and
`human.approval.required`, which is what lets the listener find the row. The
report_id also travels in the payload for any consumer reading the goal event
directly.

This mirrors the existing decision that step completion is correlated by
task_id rather than goal_id, and it needs no change to the orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from bus import produce
from events import Event
from app.schemas import UserGoalPayload

logger = logging.getLogger(__name__)

GOALS_TOPIC = "user.goals"


def build_goal_event(report_id: str, goal_text: str, category: Optional[str] = None,
                     max_products: Optional[int] = None) -> Event:
    """Build the `user.goals` event for a report request.

    `task_id=report_id` is the correlation key — see the module docstring.
    """
    payload = UserGoalPayload(
        goal=goal_text,
        report_id=report_id,
        category=category,
        max_products=max_products,
    )
    return Event(
        event_type="user.goal",
        task_id=report_id,
        agent="api",
        payload=payload.model_dump(exclude_none=True),
    )


def publish_goal_sync(event: Event, topic: str = GOALS_TOPIC) -> None:
    """Blocking publish. Called on a worker thread, never on the event loop."""
    produce(topic, event, key=event.task_id)


async def publish_goal(event: Event, topic: str = GOALS_TOPIC) -> None:
    """Publish off the event loop.

    Raises whatever confluent_kafka raises; the caller decides what a failed
    publish means for the request.
    """
    await asyncio.to_thread(publish_goal_sync, event, topic)
    logger.info("published %s to %s", event.task_id, topic)
