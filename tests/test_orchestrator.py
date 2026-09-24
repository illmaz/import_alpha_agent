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
)

from tests.helpers import GOAL_ID, artifact_for, build, goal_event, plan_json


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


def test_destination_names_a_draft_by_where_it_leaves_not_by_a_repo_path():
    from orchestrator import destination

    assert destination({"kind": "merge", "target_path": "landing/index.html"}) == "landing/index.html"
    draft = {
        "kind": "publish",
        "target_path": None,
        "publish": {"channel": "directory", "recipient": "https://skills.example.com/submit"},
    }
    assert destination(draft) == "directory->https://skills.example.com/submit"
    assert destination({"kind": "publish", "target_path": None}) == "?->(broadcast)"


def test_a_goal_whose_files_are_drafts_still_reaches_review(capsys):
    """The shape that killed the live run: five letters, no repository targets.

    `_request_review` joined `f["target_path"]` for its log line. Publish
    entries carry None there on purpose — an approved draft leaves through a
    recipient, not a file location — so the join raised TypeError, which took
    the orchestrator's artifact thread with it. The manifest for the goal in
    flight was written and the human was never told; every goal after it got no
    manifest at all, so a worker kept drafting into a queue nobody could see.
    """
    orchestrator, recorder = build([plan_json(step_count=1)])
    orchestrator.handle_goal(goal_event())
    draft_artifact = Event(
        event_type="artifact.created",
        task_id=f"{GOAL_ID}-S1",
        agent="outreach_worker",
        payload={
            "summary": "1 draft(s)",
            "files": [
                {
                    "workspace_path": "outreach_out/draft-1.md",
                    "kind": "publish",
                    "sha256": "0" * 64,
                    "bytes": 2026,
                    "summary": "letter to Cartwave AI",
                    "publish": {"channel": "email", "recipient": "team@cartwave.example.com"},
                }
            ],
        },
    )

    orchestrator.handle_artifact(draft_artifact)

    required = recorder.on(APPROVAL_TOPIC)
    assert len(required) == 1, "a goal of drafts never asked for review"
    assert required[0].payload["files"][0]["destination"] == "email->team@cartwave.example.com"
    assert "REVIEW REQUIRED for email->team@cartwave.example.com" in capsys.readouterr().out


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
