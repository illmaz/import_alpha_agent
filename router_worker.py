"""Router: fans task.created events out to task.assigned.{role} topics."""

import logging

from bus import consume, produce
from events import Event

SOURCE_TOPIC = "task.created"
GROUP_ID = "router-worker"


def handle(event: Event, topic: str) -> None:
    role = event.payload.get("role")
    if not role:
        print(f"[router] {event.task_id} has no role in payload, skipping")
        return

    target_topic = f"task.assigned.{role}"
    assigned = Event(
        event_type="task.assigned",
        task_id=event.task_id,
        agent="router",
        payload={**event.payload, "assigned_to": role},
    )
    produce(target_topic, assigned, key=event.task_id)
    print(f"[router] {event.task_id}  ->  {target_topic}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(f"[router] listening on '{SOURCE_TOPIC}' (group={GROUP_ID})")
    consume([SOURCE_TOPIC], GROUP_ID, handle)


if __name__ == "__main__":
    main()
