"""The sandbox is the whole security story for worker hands, so it is tested
for what it refuses at least as hard as for what it allows."""

from __future__ import annotations

import logging

import pytest

from app.services.tools import (
    MAX_FILE_BYTES,
    FileRecord,
    SandboxViolation,
    Workspace,
    sha256_text,
)


@pytest.fixture(autouse=True)
def workspace_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "work"))
    yield tmp_path


@pytest.fixture
def workspace():
    ws = Workspace("G-TEST")
    ws.ensure()
    return ws


# ---------- what it allows ----------


def test_write_then_read_round_trips(workspace):
    record = workspace.write_file("landing/index.html", "<h1>hi</h1>")

    assert isinstance(record, FileRecord)
    assert record.path == "landing/index.html"
    assert record.sha256 == sha256_text("<h1>hi</h1>")
    assert workspace.read_file("landing/index.html") == "<h1>hi</h1>"


def test_write_creates_intermediate_directories(workspace):
    workspace.write_file("a/b/c/deep.txt", "x")

    assert (workspace.root / "a" / "b" / "c" / "deep.txt").is_file()


def test_list_dir_reports_paths_relative_to_the_workspace(workspace):
    workspace.write_file("one.txt", "1")
    workspace.write_file("sub/two.txt", "2")

    assert workspace.list_dir() == ["one.txt", "sub"]
    assert workspace.list_dir("sub") == ["sub/two.txt"]


def test_two_goals_get_separate_workspaces():
    a, b = Workspace("G-A"), Workspace("G-B")
    a.ensure()
    b.ensure()
    a.write_file("note.txt", "from a")

    assert a.root != b.root
    with pytest.raises(FileNotFoundError):
        b.read_file("note.txt")


def test_reading_a_missing_file_raises_not_found(workspace):
    with pytest.raises(FileNotFoundError):
        workspace.read_file("nope.txt")


# ---------- what it refuses ----------


ESCAPES = [
    "../escaped.txt",
    "../../escaped.txt",
    "sub/../../escaped.txt",
    "/etc/passwd",
    "/tmp/escaped.txt",
    "../../../../../../etc/hosts",
    "../fixtures/home_organizers_report.json",
    "data/fixtures/report.json",
    "fixtures/report.json",
    "",
    "   ",
]


@pytest.mark.parametrize("bad_path", ESCAPES)
def test_escape_attempts_are_refused(workspace, bad_path):
    with pytest.raises(SandboxViolation):
        workspace.write_file(bad_path, "payload")


@pytest.mark.parametrize("bad_path", ESCAPES)
def test_escape_attempts_are_refused_for_reads_too(workspace, bad_path):
    with pytest.raises(SandboxViolation):
        workspace.read_file(bad_path)


def test_escape_attempt_writes_nothing_outside(workspace, tmp_path):
    sentinel = tmp_path / "work" / "escaped.txt"

    with pytest.raises(SandboxViolation):
        workspace.write_file("../escaped.txt", "payload")

    assert not sentinel.exists()


def test_escape_attempt_is_logged(workspace, caplog):
    with caplog.at_level(logging.WARNING, logger="tools"):
        with pytest.raises(SandboxViolation):
            workspace.write_file("../escaped.txt", "payload")

    assert any("sandbox violation" in record.message for record in caplog.records)


def test_symlink_out_of_the_workspace_is_not_followed(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SandboxViolation):
        workspace.write_file("link/planted.txt", "payload")

    assert not (outside / "planted.txt").exists()


def test_a_goal_id_with_a_separator_is_refused():
    for bad in ("../other", "a/b", "/abs", "", "x" * 65):
        with pytest.raises(SandboxViolation):
            Workspace(bad)


def test_oversized_writes_are_refused(workspace):
    with pytest.raises(SandboxViolation):
        workspace.write_file("big.txt", "x" * (MAX_FILE_BYTES + 1))

    assert not (workspace.root / "big.txt").exists()
