"""Orchestrator: publishes task.created events onto the bus."""

from bus import produce
from events import Event

TOPIC = "task.created"

TASKS = [
    {"task_id": "PROD-001", "role": "product", "title": "Score home organization opportunities"},
    {"task_id": "ENG-001", "role": "engineering", "title": "Draft the landed cost estimator"},
    {"task_id": "LAND-001", "role": "landing", "title": "Write the landing page copy"},
]


def main() -> None:
    for task in TASKS:
        event = Event(
            event_type="task.created",
            task_id=task["task_id"],
            agent="orchestrator",
            payload={"role": task["role"], "title": task["title"]},
        )
        produce(TOPIC, event, key=task["task_id"])
        print(f"[orchestrator] task.created  {task['task_id']}  role={task['role']}")

    print(f"[orchestrator] published {len(TASKS)} tasks to '{TOPIC}'")


if __name__ == "__main__":
    main()
