"""The dogfood run, offline: the lane rewrites the landing page over v0.

This drives the real Orchestrator and the real worker step handler with
FakeLLM, so it exercises planning, the sandbox, the artifact contract, the
manifest and the approval gate together. No broker, no network, no key.
"""

from __future__ import annotations

import pytest

import landing_worker
from app.services import approval
from app.services.approval import PENDING, approve, load_manifest, reject
from app.services.tools import Workspace
from llm import FakeLLM
from orchestrator import APPROVAL_TOPIC, COMPLETED_TOPIC, TASKS_TOPIC, Orchestrator
from tests.helpers import Recorder, goal_event
from worker_core import handle_step

GOAL_ID = "G-DOGFOOD"
DOGFOOD_GOAL = (
    "Rewrite the ImportAlpha landing page: produce landing/index.html with a hero, "
    "value proposition, pricing ($99 / $299 / $999), and a sample-report viewer "
    "that fetches /v1/public/sample-reports. Single self-contained HTML file, "
    "inline CSS/JS."
)
V0 = "<!doctype html><title>v0</title><h1>hand-written</h1>\n"


@pytest.fixture(autouse=True)
def sandboxed_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "work"))
    repo = tmp_path / "repo"
    (repo / "landing").mkdir(parents=True)
    (repo / "landing" / "index.html").write_text(V0)
    monkeypatch.setattr(approval, "REPO_ROOT", repo)
    return repo


def run_lane(goal_id: str = GOAL_ID):
    """Plan the goal, run the landing step, settle the artifact. Returns the recorder."""
    recorder = Recorder()
    orchestrator = Orchestrator(llm=FakeLLM(), producer=recorder)
    orchestrator.handle_goal(goal_event(text=DOGFOOD_GOAL, goal_id=goal_id))

    for task in recorder.on(TASKS_TOPIC):
        artifacts = []
        handle_step(
            role="landing",
            system_prompt=landing_worker.SYSTEM_PROMPT,
            artifact_type=landing_worker.ARTIFACT_TYPE,
            event=task,
            llm=FakeLLM(),
            producer=lambda topic, event, key=None: artifacts.append(event),
        )
        for artifact in artifacts:
            orchestrator.handle_artifact(artifact)

    return recorder


# ---------- planning ----------


def test_the_goal_plans_a_single_landing_deliverable():
    recorder = run_lane()
    tasks = recorder.on(TASKS_TOPIC)

    assert len(tasks) == 1
    assert tasks[0].payload["role"] == "landing"
    assert "landing/index.html" in tasks[0].payload["title"]
    assert tasks[0].payload["goal_id"] == GOAL_ID


# ---------- the worker's hands ----------


def test_the_worker_writes_into_its_workspace_not_the_repo(sandboxed_repo):
    run_lane()

    assert Workspace(GOAL_ID).read_file("landing/index.html")
    # The repository is untouched until a human approves.
    assert (sandboxed_repo / "landing" / "index.html").read_text() == V0


def test_the_written_page_is_a_real_self_contained_page():
    run_lane()
    page = Workspace(GOAL_ID).read_file("landing/index.html")

    assert page.lstrip().startswith("<!doctype html")
    # Both P3.1 endpoints, reused rather than reimplemented. Prices in
    # particular are fetched, never hardcoded, so the page cannot quote a
    # figure that checkout would not honour.
    assert "/v1/public/sample-reports" in page
    assert "/v1/public/pricing" in page
    assert "synthetic" in page.lower()  # the disclaimer survives the rewrite


def test_the_artifact_addresses_the_file_by_hash():
    recorder = run_lane()
    completed = recorder.on(COMPLETED_TOPIC)

    assert len(completed) == 1
    manifest = load_manifest(GOAL_ID)
    entry = manifest["files"][0]
    assert entry["sha256"]
    assert entry["bytes"] > 0


# ---------- the gate ----------


def test_completion_raises_a_review_request_naming_the_overwrite():
    recorder = run_lane()

    reviews = recorder.on(APPROVAL_TOPIC)
    assert len(reviews) == 1
    payload = reviews[0].payload
    assert payload["reason"] == "artifact_review"
    assert payload["goal_id"] == GOAL_ID
    assert payload["files"][0]["target_path"] == "landing/index.html"
    assert payload["files"][0]["target_exists"] is True  # this is the v0 overwrite


def test_the_manifest_is_pending_and_describes_the_overwrite():
    run_lane()
    manifest = load_manifest(GOAL_ID)

    assert manifest["status"] == PENDING
    assert manifest["goal"] == DOGFOOD_GOAL
    assert manifest["files"][0]["target_exists"] is True


def test_approve_lands_the_lane_page_over_v0(sandboxed_repo):
    run_lane()
    proposed = Workspace(GOAL_ID).read_file("landing/index.html")

    approve(GOAL_ID)

    served = (sandboxed_repo / "landing" / "index.html").read_text()
    assert served == proposed
    assert served != V0


def test_reject_leaves_v0_serving_and_discards_the_workspace(sandboxed_repo):
    run_lane()

    reject(GOAL_ID)

    assert (sandboxed_repo / "landing" / "index.html").read_text() == V0
    assert not Workspace(GOAL_ID).root.exists()


# ---------- the planner must name paths ----------


def test_planner_prompt_requires_a_step_to_name_its_file():
    """Found the hard way on the first live run.

    A real model decomposed this same goal into five steps like "Design the
    layout and structure of the landing page", none of which named a path, so
    every step produced prose and the goal delivered no file at all. The
    planner prompt now has to say it.
    """
    from llm import SYSTEM_PROMPT

    assert "MUST name the exact relative path" in SYSTEM_PROMPT
    assert "landing/index.html" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "step_text, expected",
    [
        ("write landing/index.html into the workspace", "landing/index.html"),
        ("Write landing/index.html: hero, pricing, sample viewer", "landing/index.html"),
        ("produce styles/site.css for the page", "styles/site.css"),
        # The shape the live model actually emitted: no path, so no file.
        ("Design the layout and structure of the landing page", None),
        ("Review and test the landing page before finalizing", None),
        # Data stays human-curated: a JSON deliverable is never extracted.
        ("write data/fixtures/report.json into the workspace", None),
    ],
)
def test_deliverable_extraction_matches_real_planner_output(step_text, expected):
    from worker_core import extract_deliverable

    assert extract_deliverable(step_text) == expected


# ---------- a goal with no files asks for nothing ----------


def test_an_analysis_only_goal_raises_no_review_request():
    recorder = Recorder()
    orchestrator = Orchestrator(llm=FakeLLM(), producer=recorder)
    orchestrator.handle_goal(goal_event(text="Assess demand for drawer organizers", goal_id="G-TALK"))

    for task in recorder.on(TASKS_TOPIC):
        artifacts = []
        handle_step(
            role="product",
            system_prompt="you are a sourcing analyst",
            artifact_type="product_brief",
            event=task,
            llm=FakeLLM(),
            producer=lambda topic, event, key=None: artifacts.append(event),
        )
        for artifact in artifacts:
            orchestrator.handle_artifact(artifact)

    assert recorder.on(COMPLETED_TOPIC)
    assert recorder.on(APPROVAL_TOPIC) == []
    assert load_manifest("G-TALK") is None
