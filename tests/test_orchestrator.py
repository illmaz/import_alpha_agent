import json

import pytest
from pydantic import ValidationError

from events import Event, GoalPlan
from llm import FakeLLM
from orchestrator import (
    APPROVAL_TOPIC,
    COMPLETED_TOPIC,
    MAX_STEPS_PER_GOAL,
    TASKS_TOPIC,
    Orchestrator,
)

GOAL_ID = "G-TEST"


class Recorder:
    """Stands in for bus.produce and keeps everything that was published."""

    def __init__(self):
        self.sent = []

    def __call__(self, topic, event, key=None):
        self.sent.append((topic, event, key))

    def topics(self):
        return [topic for topic, _, _ in self.sent]

    def on(self, topic):
        return [event for sent_topic, event, _ in self.sent if sent_topic == topic]


def goal_event(text="find home organization winners", goal_id=GOAL_ID):
    return Event(event_type="user.goal", task_id=goal_id, agent="cli", payload={"goal": text})


def artifact_for(task_id, summary="a brief"):
    return Event(
        event_type="artifact.created",
        task_id=task_id,
        agent="product_worker",
        payload={"summary": summary},
    )


def plan_json(step_count=2, role="product"):
    return json.dumps(
        {
            "reasoning": "because",
            "steps": [{"role": role, "task": f"step {i}"} for i in range(1, step_count + 1)],
        }
    )


def build(responses=None):
    recorder = Recorder()
    return Orchestrator(llm=FakeLLM(responses), producer=recorder), recorder


# ---------- GoalPlan validation ----------


def test_goal_plan_rejects_unknown_role():
    with pytest.raises(ValidationError):
        GoalPlan.model_validate(
            {"goal_id": "g", "reasoning": "r", "steps": [{"role": "marketing", "task": "t"}]}
        )


def test_goal_plan_rejects_empty_steps():
    with pytest.raises(ValidationError):
        GoalPlan.model_validate({"goal_id": "g", "reasoning": "r", "steps": []})


def test_goal_plan_accepts_known_roles():
    plan = GoalPlan.model_validate(
        {
            "goal_id": "g",
            "reasoning": "r",
            "steps": [{"role": "product", "task": "a"}, {"role": "landing", "task": "b"}],
        }
    )

    assert [step.role.value for step in plan.steps] == ["product", "landing"]


# ---------- planning ----------


def test_valid_plan_dispatches_one_task_per_step():
    orchestrator, recorder = build()

    orchestrator.handle_goal(goal_event())

    tasks = recorder.on(TASKS_TOPIC)
    assert len(tasks) == 2
    assert [task.task_id for task in tasks] == [f"{GOAL_ID}-S1", f"{GOAL_ID}-S2"]
    assert APPROVAL_TOPIC not in recorder.topics()


def test_dispatched_tasks_carry_goal_id_and_depth_zero():
    orchestrator, recorder = build()

    orchestrator.handle_goal(goal_event())

    for task in recorder.on(TASKS_TOPIC):
        assert task.payload["goal_id"] == GOAL_ID
        assert task.payload["depth"] == 0
        assert task.payload["role"] == "product"


def test_plan_goal_id_is_owned_by_us_not_the_model():
    # The model echoes a bogus goal_id; ours must win.
    forged = json.dumps(
        {"goal_id": "NOT-OURS", "reasoning": "r", "steps": [{"role": "product", "task": "t"}]}
    )
    orchestrator, recorder = build([forged])

    orchestrator.handle_goal(goal_event())

    assert recorder.on(TASKS_TOPIC)[0].payload["goal_id"] == GOAL_ID


def test_empty_goal_text_escalates():
    orchestrator, recorder = build()

    orchestrator.handle_goal(goal_event(text="   "))

    assert recorder.on(APPROVAL_TOPIC)[0].payload["reason"] == "empty_goal"
    assert TASKS_TOPIC not in recorder.topics()


# ---------- retry then escalate ----------


def test_invalid_plan_is_retried_once_and_succeeds():
    orchestrator, recorder = build(["not json at all", plan_json(step_count=1)])

    orchestrator.handle_goal(goal_event())

    assert len(orchestrator.llm.calls) == 2
    assert len(recorder.on(TASKS_TOPIC)) == 1
    assert APPROVAL_TOPIC not in recorder.topics()


def test_two_invalid_plans_escalate_and_drop_the_goal():
    orchestrator, recorder = build(["nope", "{still not a plan"])

    orchestrator.handle_goal(goal_event())

    assert len(orchestrator.llm.calls) == 2
    assert TASKS_TOPIC not in recorder.topics()

    escalation = recorder.on(APPROVAL_TOPIC)[0]
    assert escalation.payload["reason"] == "plan_validation_failed"
    assert escalation.payload["attempts"] == 2
    assert orchestrator.goals == {}


def test_plan_with_unknown_role_is_treated_as_invalid():
    orchestrator, recorder = build([plan_json(role="marketing"), plan_json(role="marketing")])

    orchestrator.handle_goal(goal_event())

    assert recorder.on(APPROVAL_TOPIC)[0].payload["reason"] == "plan_validation_failed"


# ---------- loop guard ----------


def test_too_many_steps_breaches_the_guard_and_halts():
    orchestrator, recorder = build([plan_json(step_count=MAX_STEPS_PER_GOAL + 1)])

    orchestrator.handle_goal(goal_event())

    assert TASKS_TOPIC not in recorder.topics()
    escalation = recorder.on(APPROVAL_TOPIC)[0]
    assert escalation.payload["reason"] == "max_steps_exceeded"
    assert escalation.payload["limit"] == MAX_STEPS_PER_GOAL
    # The guard is not a validation failure, so it must not burn a retry.
    assert len(orchestrator.llm.calls) == 1


def test_guard_breach_halts_only_the_offending_goal():
    orchestrator, recorder = build([plan_json(step_count=99), plan_json(step_count=2)])

    orchestrator.handle_goal(goal_event(goal_id="G-BAD"))
    orchestrator.handle_goal(goal_event(goal_id="G-GOOD"))

    assert recorder.on(APPROVAL_TOPIC)[0].task_id == "G-BAD"
    assert {task.payload["goal_id"] for task in recorder.on(TASKS_TOPIC)} == {"G-GOOD"}


# ---------- completion ----------


def test_goal_completes_once_every_step_reports_an_artifact():
    orchestrator, recorder = build()
    orchestrator.handle_goal(goal_event())

    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-S1"))
    assert COMPLETED_TOPIC not in recorder.topics()  # still one step outstanding

    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-S2"))

    completed = recorder.on(COMPLETED_TOPIC)
    assert len(completed) == 1
    assert completed[0].payload["goal_id"] == GOAL_ID
    assert completed[0].payload["steps_completed"] == 2
    assert completed[0].payload["summary"]


def test_completion_summary_comes_from_the_llm():
    orchestrator, recorder = build([plan_json(step_count=1), "the synthesized summary"])
    orchestrator.handle_goal(goal_event())

    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-S1"))

    assert recorder.on(COMPLETED_TOPIC)[0].payload["summary"] == "the synthesized summary"
    assert orchestrator.llm.calls[-1][1].startswith("SUMMARIZE:")


def test_completed_goal_state_is_released():
    orchestrator, _ = build([plan_json(step_count=1)])
    orchestrator.handle_goal(goal_event())

    orchestrator.handle_artifact(artifact_for(f"{GOAL_ID}-S1"))

    assert orchestrator.goals == {}
    assert orchestrator.task_to_goal == {}


def test_duplicate_artifact_does_not_complete_twice():
    orchestrator, recorder = build([plan_json(step_count=1)])
    orchestrator.handle_goal(goal_event())
    duplicate = artifact_for(f"{GOAL_ID}-S1")

    orchestrator.handle_artifact(duplicate)
    orchestrator.handle_artifact(duplicate)

    assert len(recorder.on(COMPLETED_TOPIC)) == 1


def test_artifact_for_unknown_task_is_ignored():
    orchestrator, recorder = build()
    orchestrator.handle_goal(goal_event())

    orchestrator.handle_artifact(artifact_for("SOMEONE-ELSES-TASK"))

    assert COMPLETED_TOPIC not in recorder.topics()


def test_two_goals_are_tracked_independently():
    orchestrator, recorder = build(
        [plan_json(step_count=1), plan_json(step_count=1), "summary a", "summary b"]
    )
    orchestrator.handle_goal(goal_event(goal_id="G-A"))
    orchestrator.handle_goal(goal_event(goal_id="G-B"))

    orchestrator.handle_artifact(artifact_for("G-A-S1"))

    assert [event.payload["goal_id"] for event in recorder.on(COMPLETED_TOPIC)] == ["G-A"]
    assert "G-B" in orchestrator.goals


# ---------- FakeLLM ----------


def test_fake_llm_is_deterministic_and_offline():
    first, second = FakeLLM().complete("s", "PLAN: x"), FakeLLM().complete("s", "PLAN: x")

    assert first == second
    assert GoalPlan.model_validate({**json.loads(first), "goal_id": "g"}).steps


def test_fake_llm_returns_prose_for_synthesis():
    reply = FakeLLM().complete("s", "SUMMARIZE: goal G - do a thing")

    with pytest.raises(json.JSONDecodeError):
        json.loads(reply)
