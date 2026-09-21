#!/usr/bin/env python3
"""Submit a goal to the orchestrator via Kafka."""

import asyncio
import sys

from bus import Bus
from events import Event, RouterRequest, Topic


async def submit_goal(query: str) -> None:
    """Publish a router request event with the given query."""
    bus = Bus()
    request = RouterRequest(query=query)
    
    await bus.publish(
        Event(
            topic=Topic.ROUTER_REQUEST,
            correlation_id=request.task_id,
            payload=request.model_dump()
        )
    )
    print(f"Submitted goal: task_id={request.task_id} query={query!r}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/submit_goal.py \"<your goal/query>\"")
        sys.exit(1)
    
    query = " ".join(sys.argv[1:])
    asyncio.run(submit_goal(query))
