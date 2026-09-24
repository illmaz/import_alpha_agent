"""The human gate. Nothing the lane writes reaches a real path without it."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.services import approval
from app.services.approval import (
    APPROVED,
    PENDING,
    REJECTED,
    ApprovalError,
    approve,
    list_manifests,
    load_manifest,
    reject,
    validate_target,
    write_manifest,
)
from app.services.tools import Workspace, sha256_text

GOAL = "G-REVIEW"
CONTENT = "<h1>written by the lane</h1>\n"


@pytest.fixture(autouse=True)
def sandboxed_repo(tmp_path, monkeypatch):
    """Point both the workspace and the 'repository' at tmp_path.

    Without the REPO_ROOT patch these tests would approve files onto the real
    working tree.
    """
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "work"))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(approval, "REPO_ROOT", repo)
    return repo


def stage(content: str = CONTENT, workspace_path: str = "landing/index.html", goal: str = GOAL):
    """Write a file into a workspace and record a manifest for it."""
    ws = Workspace(goal)
    ws.ensure()
    record = ws.write_file(workspace_path, content)
    return write_manifest(
        goal,
        "Rewrite the landing page",
        [
            {
                "workspace_path": record.path,
                "target_path": record.path,
                "sha256": record.sha256,
                "bytes": record.bytes,
                "summary": "landing: rewrite",
            }
        ],
    )


# ---------- manifests ----------


def test_manifest_records_a_new_file_as_a_create():
    manifest = stage()

    assert manifest["status"] == PENDING
    assert manifest["files"][0]["target_path"] == "landing/index.html"
    assert manifest["files"][0]["target_exists"] is False


def test_manifest_records_an_existing_target_as_an_overwrite(sandboxed_repo):
    (sandboxed_repo / "landing").mkdir()
    (sandboxed_repo / "landing" / "index.html").write_text("<h1>v0</h1>")

    manifest = stage()

    assert manifest["files"][0]["target_exists"] is True


def test_list_manifests_filters_by_status():
    stage()

    assert [m["goal_id"] for m in list_manifests(PENDING)] == [GOAL]
    assert list_manifests(APPROVED) == []


def test_list_manifests_is_empty_before_the_lane_runs():
    assert list_manifests() == []


# ---------- approve ----------


def test_approve_writes_the_file_onto_its_target(sandboxed_repo):
    stage()

    approve(GOAL)

    assert (sandboxed_repo / "landing" / "index.html").read_text() == CONTENT


def test_approve_overwrites_v0(sandboxed_repo):
    (sandboxed_repo / "landing").mkdir()
    target = sandboxed_repo / "landing" / "index.html"
    target.write_text("<h1>v0</h1>")
    stage()

    approve(GOAL)

    assert target.read_text() == CONTENT


def test_approve_marks_the_manifest_and_logs_the_decision():
    stage()

    approve(GOAL)

    assert load_manifest(GOAL)["status"] == APPROVED
    entries = [
        json.loads(line)
        for line in approval.decisions_log_path().read_text().splitlines()
    ]
    assert entries[-1]["goal_id"] == GOAL
    assert entries[-1]["decision"] == APPROVED
    assert entries[-1]["files"][0]["target_path"] == "landing/index.html"
    assert entries[-1]["at"]


def test_approving_twice_is_refused():
    stage()
    approve(GOAL)

    with pytest.raises(ApprovalError, match="already approved"):
        approve(GOAL)


def test_approve_refuses_a_goal_with_no_manifest():
    with pytest.raises(ApprovalError, match="no manifest"):
        approve("G-NOTHING")


def test_approve_refuses_when_the_workspace_file_changed_after_the_manifest():
    stage()
    Workspace(GOAL).write_file("landing/index.html", "<h1>swapped</h1>")

    with pytest.raises(ApprovalError, match="changed since the manifest"):
        approve(GOAL)


def test_approve_is_all_or_nothing(sandboxed_repo):
    """A bad second entry must not leave the first one applied."""
    ws = Workspace(GOAL)
    ws.ensure()
    good = ws.write_file("landing/index.html", CONTENT)
    write_manifest(
        GOAL,
        "two files, one poisoned",
        [
            {
                "workspace_path": good.path,
                "target_path": good.path,
                "sha256": good.sha256,
                "bytes": good.bytes,
                "summary": "ok",
            },
            {
                "workspace_path": good.path,
                "target_path": "data/fixtures/home_organizers_report.json",
                "sha256": good.sha256,
                "bytes": good.bytes,
                "summary": "poisoned",
            },
        ],
    )

    with pytest.raises(ApprovalError, match="protected"):
        approve(GOAL)

    assert not (sandboxed_repo / "landing" / "index.html").exists()


# ---------- protected targets ----------


@pytest.mark.parametrize(
    "target",
    [
        "data/fixtures/home_organizers_report.json",
        "data/fixtures/anything.json",
        "data/work/G-OTHER/index.html",
        ".git/config",
        ".env",
    ],
)
def test_protected_targets_are_refused(target):
    with pytest.raises(ApprovalError, match="protected"):
        validate_target(target)


@pytest.mark.parametrize("target", ["../outside.txt", "/etc/passwd", "a/../../b.txt"])
def test_targets_outside_the_repository_are_refused(target):
    with pytest.raises(ApprovalError):
        validate_target(target)


def test_an_ordinary_target_is_allowed():
    assert validate_target("landing/index.html").name == "index.html"


# ---------- reject ----------


def test_reject_discards_the_workspace_and_writes_nothing(sandboxed_repo):
    stage()
    workspace_root_dir = Workspace(GOAL).root

    reject(GOAL)

    assert not workspace_root_dir.exists()
    assert not (sandboxed_repo / "landing" / "index.html").exists()


def test_reject_logs_before_deleting():
    stage()

    reject(GOAL)

    entries = [
        json.loads(line)
        for line in approval.decisions_log_path().read_text().splitlines()
    ]
    assert entries[-1]["decision"] == REJECTED
    assert entries[-1]["files"][0]["target_path"] == "landing/index.html"


def test_rejecting_twice_is_refused():
    stage()
    reject(GOAL)

    with pytest.raises(ApprovalError, match="no manifest"):
        reject(GOAL)


# ---------- the CLI ----------


def load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "approve.py"
    spec = importlib.util.spec_from_file_location("approve_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_show_renders_a_unified_diff_over_an_existing_target(sandboxed_repo, capsys):
    (sandboxed_repo / "landing").mkdir()
    (sandboxed_repo / "landing" / "index.html").write_text("<h1>v0</h1>\n")
    stage()

    load_cli().main(["approve.py", "show", GOAL])
    out = capsys.readouterr().out

    assert "overwrites an existing file" in out
    assert "--- a/landing/index.html (current)" in out
    assert "+++ b/landing/index.html (proposed)" in out
    assert "-<h1>v0</h1>" in out
    assert "+<h1>written by the lane</h1>" in out


def test_cli_show_prints_full_contents_for_a_new_file(capsys):
    stage()

    load_cli().main(["approve.py", "show", GOAL])
    out = capsys.readouterr().out

    assert "new file" in out
    assert CONTENT.strip() in out


def test_cli_list_marks_pending_goals(capsys):
    stage()

    load_cli().main(["approve.py", "list"])
    out = capsys.readouterr().out

    assert GOAL in out
    assert "1 pending" in out


def test_cli_approve_then_list_shows_it_approved(sandboxed_repo, capsys):
    stage()
    cli = load_cli()

    assert cli.main(["approve.py", "approve", GOAL]) == 0
    capsys.readouterr()
    cli.main(["approve.py", "list"])

    assert "[approved]" in capsys.readouterr().out
    assert (sandboxed_repo / "landing" / "index.html").read_text() == CONTENT


def test_cli_reject_returns_zero_and_discards(capsys):
    stage()
    cli = load_cli()

    assert cli.main(["approve.py", "reject", GOAL]) == 0
    assert not Workspace(GOAL).root.exists()


def test_cli_reports_failure_for_an_unknown_goal(capsys):
    assert load_cli().main(["approve.py", "approve", "G-NOPE"]) == 1
    assert "Refused" in capsys.readouterr().err
