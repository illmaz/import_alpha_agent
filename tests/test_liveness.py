"""Phase 2.5: every goal must terminate. Stall detection and role gating."""

import importlib

import pytest
from pydantic import ValidationError

import events
from events import ACTIVE_ROLES, WORKER_ROLES, Event, GoalPlan
from orchestrator import (
    APPROVAL_TOPIC,
    COMPLETED_TOPIC,
    STEP_TTL_SECONDS,
    TASKS_TOPIC,
)
from tests.helpers import GOAL_ID, FakeClock, Recorder, artifact_for, build, goal_event, plan_json

PAST_TTL = STEP_TTL_SECONDS + 1


# ---------- stall detection ----------


def test_no_stall_while_steps_are_within_ttl():
    clock = FakeClock()
    orchestrator, recorder = build(clock=clock)
    orchestrator.handle_goal(goal_event())

    clock.advance(STEP_TTL_SECONDS - 1)
    orchestrator.check_stalls()

    assert APPROVAL_TOPIC not in recorder.topics()
    assert len(recorder.on(TASKS_TOPIC)) == 2  # no replan
    assert len(orchestrator.llm.calls) == 1


def test_stalled_step_replans_once_then_escalates_on_second_stall():
    clock = FakeClock()
    orchestrator, recorder = build(clock=clock)
    orchestrator.handle_goal(goal_event())

    # First stall -> one replan, tagged as a new generation.
    clock.advance(PAST_TTL)
    orchestrator.check_stalls()

    tasks = recorder.on(TASKS_TOPIC)
    assert len(tasks) == 4
    assert [task.task_id for task in tasks[2:]] == [f"{GOAL_ID}-R1-S1", f"{GOAL_ID}-R1-S2"]
    assert APPROVAL_TOPIC not in recorder.topics()

    # Second stall -> halt.
    clock.advance(PAST_TTL)
    orchestrator.check_stalls()

    escalations = recorder.on(APPROVAL_TOPIC)
    assert len(escalations) == 1
    assert escalations[0].payload["reason"] == "step_stalled"
    assert escalations[0].payload["ttl_seconds"] == STEP_TTL_SECONDS
    assert escalations[0].payload["stalled_steps"] == [f"{GOAL_ID}-R1-S1", f"{GOAL_ID}-R1-S2"]
    assert orchestrator.goals == {}
    assert orchestrator.task_to_goal == {}


def test_stall_escalates_without_replan_when_retries_already_spent():
    # A validation retry consumes the same budget a stall replan would need.
    clock = FakeClock()
    orchestrator, recorder = build(["not json", plan_json(step_count=1)], clock=clock)
    orchestrator.handle_goal(goal_event())
    assert len(recorder.on(TASKS_TOPIC)) == 1

    clock.advance(PAST_TTL)
    orchestrator.check_stalls()

    assert len(recorder.on(TASKS_TOPIC)) == 1  # nothing re-dispatched
    assert recorder.on(APPROVAL_TOPIC)[0].payload["reason"] == "step_stalled"


def test_ttl_does_not_fire_on_steps_that_complete_in_time():
    clock = FakeClock()
    orchestrator, recorder = build(clock=clock)
    orchestrator.handle_goal(goal_event())

    clock.advance(STEP_TTL_SECONDS - 1)
    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-S1"))
    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-S2"))

    clock.advance(10_000)
    orchestrator.check_stalls()

    assert len(recorder.on(COMPLETED_TOPIC)) == 1
    assert APPROVAL_TOPIC not in recorder.topics()


def test_stall_affects_only_the_stalled_goal():
    clock = FakeClock()
    orchestrator, recorder = build(clock=clock)
    orchestrator.handle_goal(goal_event(goal_id="G-SLOW"))
    orchestrator.handle_goal(goal_event(goal_id="G-FAST"))

    orchestrator.handle_artifact(artifact_for("G-FAST-S1"))
    orchestrator.handle_artifact(artifact_for("G-FAST-S2"))

    clock.advance(PAST_TTL)
    orchestrator.check_stalls()  # replan
    clock.advance(PAST_TTL)
    orchestrator.check_stalls()  # escalate

    assert [event.task_id for event in recorder.on(COMPLETED_TOPIC)] == ["G-FAST"]
    assert [event.task_id for event in recorder.on(APPROVAL_TOPIC)] == ["G-SLOW"]


def test_late_artifact_from_the_stalled_generation_is_ignored():
    clock = FakeClock()
    orchestrator, recorder = build(clock=clock)
    orchestrator.handle_goal(goal_event())

    clock.advance(PAST_TTL)
    orchestrator.check_stalls()

    # The original slow worker finally answers; its step no longer exists.
    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-S1"))
    assert COMPLETED_TOPIC not in recorder.topics()

    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-R1-S1"))
    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-R1-S2"))

    assert len(recorder.on(COMPLETED_TOPIC)) == 1


def test_replan_after_stall_counts_against_the_attempt_budget():
    clock = FakeClock()
    orchestrator, _ = build(clock=clock)
    orchestrator.handle_goal(goal_event())
    assert orchestrator.goals[GOAL_ID].attempts == 1

    clock.advance(PAST_TTL)
    orchestrator.check_stalls()

    assert orchestrator.goals[GOAL_ID].attempts == 2
    assert orchestrator.goals[GOAL_ID].stalls == 1


# ---------- active role registry ----------


def test_default_active_roles_cover_every_worker_role():
    assert ACTIVE_ROLES == frozenset(WORKER_ROLES)


def test_active_roles_can_be_narrowed_by_env(monkeypatch):
    monkeypatch.setenv("ACTIVE_ROLES", "product, landing")

    assert events._parse_active_roles() == frozenset({"product", "landing"})


def test_active_roles_env_rejects_unknown_role(monkeypatch):
    monkeypatch.setenv("ACTIVE_ROLES", "product,marketing")

    with pytest.raises(ValueError, match="unknown roles"):
        events._parse_active_roles()


def test_plan_targeting_an_inactive_role_fails_validation(monkeypatch):
    monkeypatch.setattr(events, "ACTIVE_ROLES", frozenset({"product"}))

    with pytest.raises(ValidationError, match="no active worker"):
        GoalPlan.model_validate(
            {"goal_id": "g", "reasoning": "r", "steps": [{"role": "landing", "task": "t"}]}
        )


def test_orchestrator_never_dispatches_to_an_inactive_role(monkeypatch):
    monkeypatch.setattr(events, "ACTIVE_ROLES", frozenset({"product"}))
    orchestrator, recorder = build([plan_json(role="landing"), plan_json(role="landing")])

    orchestrator.handle_goal(goal_event())

    assert TASKS_TOPIC not in recorder.topics()
    assert recorder.on(APPROVAL_TOPIC)[0].payload["reason"] == "plan_validation_failed"


def test_all_active_roles_are_accepted_by_default():
    plan = GoalPlan.model_validate(
        {
            "goal_id": "g",
            "reasoning": "r",
            "steps": [{"role": role, "task": "t"} for role in sorted(WORKER_ROLES)],
        }
    )

    assert len(plan.steps) == len(WORKER_ROLES)


# ---------- worker stubs ----------

WORKERS = [
    ("product_worker", "task.assigned.product", "product_brief"),
    ("engineering_worker", "task.assigned.engineering", "engineering_brief"),
    ("landing_worker", "task.assigned.landing", "landing_brief"),
]


@pytest.mark.parametrize("module_name, source_topic, artifact_type", WORKERS)
def test_worker_emits_a_wellformed_stub_artifact(
    monkeypatch, module_name, source_topic, artifact_type
):
    module = importlib.import_module(module_name)
    recorder = Recorder()
    monkeypatch.setattr(module, "produce", recorder)
    monkeypatch.setattr(module, "WORK_SECONDS", 0)

    assigned = Event(
        event_type="task.assigned",
        task_id="T-1",
        agent="router",
        payload={"title": "do the thing", "goal_id": "G-1"},
    )
    module.handle(assigned, source_topic)

    artifacts = recorder.on("artifact.created")
    assert len(artifacts) == 1

    artifact = artifacts[0]
    assert artifact.event_type == "artifact.created"
    assert artifact.task_id == "T-1"  # preserved so the orchestrator can correlate
    assert artifact.agent == module_name
    assert artifact.payload["artifact_type"] == artifact_type
    assert artifact.payload["status"] == "stub"
    assert artifact.payload["datapoints"] == []  # never invent data
    assert "do the thing" in artifact.payload["summary"]


@pytest.mark.parametrize("module_name, source_topic, _artifact_type", WORKERS)
def test_worker_listens_on_its_own_role_topic(module_name, source_topic, _artifact_type):
    module = importlib.import_module(module_name)

    assert module.SOURCE_TOPIC == source_topic
    assert module.OUTPUT_TOPIC == "artifact.created"
    assert module.GROUP_ID == module_name.replace("_", "-")
