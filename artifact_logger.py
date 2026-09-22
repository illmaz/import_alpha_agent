"""Logger: records artifact.created events to the console."""

import logging

from bus import consume, install_signal_handlers
from events import Event

SOURCE_TOPIC = "artifact.created"
GROUP_ID = "artifact-logger"


def handle(event: Event, topic: str) -> None:
    print("=" * 68)
    print(f"ARTIFACT  {event.task_id}   from {event.agent}")
    print("-" * 68)
    print(f"  event_id   : {event.event_id}")
    print(f"  event_type : {event.event_type}")
    print(f"  created_at : {event.created_at}")
    for key, value in event.payload.items():
        print(f"  {key:<11}: {value}")
    print("=" * 68)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    install_signal_handlers("logger")
    print(f"[logger] listening on '{SOURCE_TOPIC}' (group={GROUP_ID})")
    consume([SOURCE_TOPIC], GROUP_ID, handle)


if __name__ == "__main__":
    main()
