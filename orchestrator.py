"""Orchestrator daemon: plans goals with an LLM and drives them to termination.

Liveness invariant: every goal ends in exactly one of completed, escalated, or
guard-breached. Stall detection is what makes that true when a worker dies.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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
# Total planning attempts per goal, spanning both the initial retry and any
# stall-triggered replan.
MAX_REPLANS = 2

# A step with no artifact after this long is considered stalled.
STEP_TTL_SECONDS = float(os.environ.get("STEP_TTL_SECONDS", "120"))
TICK_SECONDS = float(os.environ.get("STALL_TICK_SECONDS", "10"))

logger = logging.getLogger("orchestrator")


@dataclass
class GoalState:
    goal_id: str
    goal_text: str
    plan: GoalPlan
    pending: Set[str]
    dispatched_at: Dict[str, float]
    attempts: int
    generation: int = 0
    stalls: int = 0
    artifacts: List[Event] = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        llm: Optional[LLM] = None,
        producer: Callable[..., None] = produce,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.llm = llm if llm is not None else get_llm()
        self.produce = producer
        self.clock = clock
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

        self._plan_and_dispatch(goal_id, goal_text, attempts_used=0, generation=0, stalls=0)

    def _plan_and_dispatch(
        self, goal_id: str, goal_text: str, attempts_used: int, generation: int, stalls: int
    ) -> None:
        plan, used, last_error = self._attempt_plan(
            goal_id, goal_text, budget=MAX_REPLANS - attempts_used
        )
        attempts = attempts_used + used

        if plan is None:
            self._escalate(
                goal_id,
                "plan_validation_failed",
                {"attempts": attempts, "goal": goal_text, "last_error": last_error},
            )
            return

        if len(plan.steps) > MAX_STEPS_PER_GOAL:
            self._escalate(
                goal_id,
                "max_steps_exceeded",
                {"steps": len(plan.steps), "limit": MAX_STEPS_PER_GOAL},
            )
            return

        task_ids = [
            self._task_id(goal_id, generation, index)
            for index in range(1, len(plan.steps) + 1)
        ]
        now = self.clock()

        # Register before publishing, so an artifact cannot beat us to the map.
        with self.lock:
            self._forget_locked(goal_id)
            self.goals[goal_id] = GoalState(
                goal_id=goal_id,
                goal_text=goal_text,
                plan=plan,
                pending=set(task_ids),
                dispatched_at={task_id: now for task_id in task_ids},
                attempts=attempts,
                generation=generation,
                stalls=stalls,
            )
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

    def _attempt_plan(
        self, goal_id: str, goal_text: str, budget: int
    ) -> Tuple[Optional[GoalPlan], int, str]:
        """Plan within `budget` attempts. Returns (plan, attempts_used, last_error)."""
        used = 0
        last_error = ""
        while used < budget:
            used += 1
            raw = self.llm.complete(SYSTEM_PROMPT, f"PLAN: {goal_text}")
            try:
                return self._parse_plan(goal_id, raw), used, ""
            except (ValueError, TypeError) as exc:
                last_error = str(exc)
                logger.warning("plan attempt %d invalid for %s: %s", used, goal_id, exc)
        return None, used, last_error

    @staticmethod
    def _parse_plan(goal_id: str, raw: str) -> GoalPlan:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("plan must be a JSON object")
        # The model never sees the goal_id, so we own it rather than trusting an echo.
        data["goal_id"] = goal_id
        return GoalPlan.model_validate(data)

    @staticmethod
    def _task_id(goal_id: str, generation: int, index: int) -> str:
        """Step ids are generation-tagged so a late artifact from a stalled
        dispatch cannot be mistaken for the replanned step finishing."""
        if generation == 0:
            return f"{goal_id}-S{index}"
        return f"{goal_id}-R{generation}-S{index}"

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

    # ---------- stall detection ----------

    def check_stalls(self) -> None:
        """Replan or escalate goals whose steps have gone quiet past the TTL."""
        now = self.clock()
        stalled: List[Tuple[str, List[str]]] = []

        with self.lock:
            for goal_id, state in self.goals.items():
                overdue = [
                    task_id
                    for task_id in state.pending
                    if now - state.dispatched_at.get(task_id, now) >= STEP_TTL_SECONDS
                ]
                if overdue:
                    stalled.append((goal_id, sorted(overdue)))

        for goal_id, overdue in stalled:
            self._handle_stall(goal_id, overdue)

    def _handle_stall(self, goal_id: str, overdue: List[str]) -> None:
        with self.lock:
            state = self.goals.get(goal_id)
            if state is None:
                return
            # Out of replans, either because this goal already stalled once or
            # because validation retries used the budget up.
            exhausted = state.stalls >= 1 or state.attempts >= MAX_REPLANS
            goal_text, attempts, generation = state.goal_text, state.attempts, state.generation
            if not exhausted:
                state.stalls += 1

        if exhausted:
            self._escalate(
                goal_id,
                "step_stalled",
                {"stalled_steps": overdue, "ttl_seconds": STEP_TTL_SECONDS},
            )
            return

        print(f"[orchestrator] {goal_id}: stalled on {', '.join(overdue)} - replanning")
        self._plan_and_dispatch(
            goal_id, goal_text, attempts_used=attempts, generation=generation + 1, stalls=1
        )

    def _ticker(self) -> None:
        while True:
            time.sleep(TICK_SECONDS)
            try:
                self.check_stalls()
            except Exception:  # a crash here would silently disable liveness
                logger.exception("stall check failed")

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
        with self.lock:
            return self._forget_locked(goal_id)

    def _forget_locked(self, goal_id: str) -> Optional[GoalState]:
        """Drop all state for one goal. Caller must hold the lock."""
        state = self.goals.pop(goal_id, None)
        for task_id in [t for t, g in self.task_to_goal.items() if g == goal_id]:
            del self.task_to_goal[task_id]
        return state


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    orchestrator = Orchestrator()

    threading.Thread(
        target=consume,
        args=([ARTIFACTS_TOPIC], ARTIFACTS_GROUP, orchestrator.handle_artifact),
        name="artifact-consumer",
        daemon=True,
    ).start()

    threading.Thread(target=orchestrator._ticker, name="stall-ticker", daemon=True).start()

    print(
        f"[orchestrator] daemon up - goals on '{GOALS_TOPIC}', artifacts on "
        f"'{ARTIFACTS_TOPIC}', step TTL {STEP_TTL_SECONDS:.0f}s"
    )
    consume([GOALS_TOPIC], GOALS_GROUP, orchestrator.handle_goal)


if __name__ == "__main__":
    main()
