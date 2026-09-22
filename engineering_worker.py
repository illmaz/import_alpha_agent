"""Engineering agent: consumes its assigned tasks and emits artifact.created."""

import logging
import time

from bus import consume, install_signal_handlers, produce
from events import Event

SOURCE_TOPIC = "task.assigned.engineering"
OUTPUT_TOPIC = "artifact.created"
GROUP_ID = "engineering-worker"

WORK_SECONDS = 2


def handle(event: Event, topic: str) -> None:
    title = event.payload.get("title", "(untitled)")
    print(f"[engineering] Engineering Agent is working on {event.task_id}: {title}")
    time.sleep(WORK_SECONDS)

    artifact = Event(
        event_type="artifact.created",
        task_id=event.task_id,
        agent="engineering_worker",
        payload={
            "artifact_type": "engineering_brief",
            "summary": f"Placeholder engineering brief for {event.task_id} ({title}).",
            "status": "stub",
            # Empty until real sources are wired: AGENTS.md forbids inventing
            # datapoints, which each need source_url, observed_at, confidence.
            "datapoints": [],
        },
    )
    produce(OUTPUT_TOPIC, artifact, key=event.task_id)
    print(f"[engineering] artifact.created  {event.task_id}  ->  '{OUTPUT_TOPIC}'")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    install_signal_handlers("engineering")
    print(f"[engineering] listening on '{SOURCE_TOPIC}' (group={GROUP_ID})")
    consume([SOURCE_TOPIC], GROUP_ID, handle)


if __name__ == "__main__":
    main()
