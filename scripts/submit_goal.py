#!/usr/bin/env python3
"""CLI: publish a plain-text goal onto the user.goals topic."""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import produce  # noqa: E402
from events import Event  # noqa: E402

TOPIC = "user.goals"


def main(argv: list[str]) -> int:
    goal_text = " ".join(argv[1:]).strip()
    if not goal_text:
        print('usage: python scripts/submit_goal.py "<goal text>"', file=sys.stderr)
        return 1

    goal_id = f"G-{uuid.uuid4().hex[:8]}"
    produce(
        TOPIC,
        Event(
            event_type="user.goal",
            task_id=goal_id,
            agent="cli",
            payload={"goal": goal_text},
        ),
        key=goal_id,
    )
    print(f"submitted {goal_id} -> '{TOPIC}': {goal_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
