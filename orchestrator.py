"""Orchestrator daemon: plans goals with an LLM and tracks them to completion."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from bus import consume, produce
from events import Event, GoalPlan
from llm import LLM, SYSTEM_PROMPT, get_llm

GOALS_TOPIC = "user.goals"
TASKS_TOPIC = "task.created"
ARTIFACTS_TOPIC = "artifact.created"
COMPLETED_TOPIC = "goal.completed"
APPROVAL_TOPIC = "human.approval.required"

GOALS_GROUP = "orchestrator-daemon"
ARTIFACTS_GROUP = "orchestrator-artifacts"

MAX_STEPS_PER_GOAL = 5
# Total planning attempts per goal: the first plus one retry on invalid output.
MAX_REPLANS = 2

logger = logging.getLogger("orchestrator")


@dataclass
class GoalState:
    goal_id: str
    goal_text: str
    plan: GoalPlan
    pending: Set[str]
    artifacts: List[Event] = field(default_factory=list)


class Orchestrator:
    def __init__(self, llm: Optional[LLM] = None, producer: Callable[..., None] = produce) -> None:
        self.llm = llm if llm is not None else get_llm()
        self.produce = producer
        self.goals: Dict[str, GoalState] = {}
        # Artifacts only carry task_id back (workers do not echo goal_id), so
        # step completion is correlated through this map.
        self.task_to_goal: Dict[str, str] = {}
        self.lock = threading.Lock()

    # ---------- goals ----------

    def handle_goal(self, event: Event, topic: str = GOALS_TOPIC) -> None:
        goal_id = event.task_id or f"G-{uuid.uuid4().hex[:8]}"
        goal_text = str(event.payload.get("goal", "")).strip()

        if not goal_text:
            self._escalate(goal_id, "empty_goal", {"event_id": event.event_id})
            return

        plan = self._plan(goal_id, goal_text)
        if plan is None:
            return

        if len(plan.steps) > MAX_STEPS_PER_GOAL:
            self._escalate(
                goal_id,
                "max_steps_exceeded",
                {"steps": len(plan.steps), "limit": MAX_STEPS_PER_GOAL},
            )
            return

        task_ids = [f"{goal_id}-S{index}" for index in range(1, len(plan.steps) + 1)]

        # Register before publishing, so an artifact cannot beat us to the map.
        with self.lock:
            self.goals[goal_id] = GoalState(goal_id, goal_text, plan, set(task_ids))
            for task_id in task_ids:
                self.task_to_goal[task_id] = goal_id

        for task_id, step in zip(task_ids, plan.steps):
            self.produce(
                TASKS_TOPIC,
                Event(
                    event_type="task.created",
                    task_id=task_id,
                    agent="orchestrator",
                    payload={
                        "goal_id": goal_id,
                        "depth": 0,
                        "role": step.role.value,
                        "title": step.task,
                    },
                ),
                key=task_id,
            )

        print(f"[orchestrator] {goal_id}: dispatched {len(task_ids)} step(s)")

    def _plan(self, goal_id: str, goal_text: str) -> Optional[GoalPlan]:
        last_error = ""
        for attempt in range(1, MAX_REPLANS + 1):
            raw = self.llm.complete(SYSTEM_PROMPT, f"PLAN: {goal_text}")
            try:
                return self._parse_plan(goal_id, raw)
            except (ValueError, TypeError) as exc:
                last_error = str(exc)
                logger.warning(
                    "plan attempt %d/%d invalid for %s: %s",
                    attempt,
                    MAX_REPLANS,
                    goal_id,
                    exc,
                )

        self._escalate(
            goal_id,
            "plan_validation_failed",
            {"attempts": MAX_REPLANS, "goal": goal_text, "last_error": last_error},
        )
        return None

    @staticmethod
    def _parse_plan(goal_id: str, raw: str) -> GoalPlan:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("plan must be a JSON object")
        # The model never sees the goal_id, so we own it rather than trusting an echo.
        data["goal_id"] = goal_id
        return GoalPlan.model_validate(data)

    # ---------- artifacts ----------

    def handle_artifact(self, event: Event, topic: str = ARTIFACTS_TOPIC) -> None:
        if event.task_id is None:
            return

        with self.lock:
            goal_id = self.task_to_goal.get(event.task_id)
            state = self.goals.get(goal_id) if goal_id else None
            if state is None:
                return
            state.pending.discard(event.task_id)
            state.artifacts.append(event)
            remaining = len(state.pending)

        print(f"[orchestrator] {goal_id}: {event.task_id} done, {remaining} step(s) left")
        if remaining == 0:
            self._complete(goal_id)

    def _complete(self, goal_id: str) -> None:
        state = self._forget(goal_id)
        if state is None:
            return

        summary = self.llm.complete(SYSTEM_PROMPT, self._synthesis_prompt(state)).strip()
        self.produce(
            COMPLETED_TOPIC,
            Event(
                event_type="goal.completed",
                task_id=goal_id,
                agent="orchestrator",
                payload={
                    "goal_id": goal_id,
                    "goal": state.goal_text,
                    "steps_completed": len(state.artifacts),
                    "summary": summary,
                },
            ),
            key=goal_id,
        )
        print(f"[orchestrator] {goal_id}: COMPLETE - {summary}")

    @staticmethod
    def _synthesis_prompt(state: GoalState) -> str:
        lines = [f"SUMMARIZE: goal {state.goal_id} - {state.goal_text}", "", "Artifacts:"]
        for artifact in state.artifacts:
            lines.append(f"- {artifact.task_id}: {artifact.payload.get('summary', '(no summary)')}")
        return "\n".join(lines)

    # ---------- halting ----------

    def _escalate(self, goal_id: str, reason: str, detail: Dict[str, Any]) -> None:
        self._forget(goal_id)
        self.produce(
            APPROVAL_TOPIC,
            Event(
                event_type="human.approval.required",
                task_id=goal_id,
                agent="orchestrator",
                payload={"goal_id": goal_id, "reason": reason, **detail},
            ),
            key=goal_id,
        )
        print(f"[orchestrator] {goal_id}: HALTED ({reason}) -> {APPROVAL_TOPIC}")

    def _forget(self, goal_id: str) -> Optional[GoalState]:
        """Drop all state for one goal. Returns it, or None if already gone."""
        with self.lock:
            state = self.goals.pop(goal_id, None)
            for task_id in [t for t, g in self.task_to_goal.items() if g == goal_id]:
                del self.task_to_goal[task_id]
            return state


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    orchestrator = Orchestrator()

    artifact_consumer = threading.Thread(
        target=consume,
        args=([ARTIFACTS_TOPIC], ARTIFACTS_GROUP, orchestrator.handle_artifact),
        name="artifact-consumer",
        daemon=True,
    )
    artifact_consumer.start()

    print(f"[orchestrator] daemon up - goals on '{GOALS_TOPIC}', artifacts on '{ARTIFACTS_TOPIC}'")
    consume([GOALS_TOPIC], GOALS_GROUP, orchestrator.handle_goal)


if __name__ == "__main__":
    main()
