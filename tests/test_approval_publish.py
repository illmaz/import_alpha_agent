"""The half of the gate that decides what may leave the machine.

`tests/test_approval.py` covers the merge half: a repository change goes in
whole or not at all. This file is about publish entries — outreach letters,
directory manifests — where whole-and-all-or-nothing is the wrong rule. A batch
of five prospects is five independent decisions, and "approve two, reject three"
is the normal case rather than an edge case.

The shape worth pinning down is *where an approved draft lives*. A decision
takes effect by copying the body out of the goal workspace and into
`data/work/outbox/`, because the goal directory is deleted when a batch closes.
If the outbox were a view onto the workspace, the three refused letters would
take the two approved ones with them, and the publisher would be left holding
paths that no longer resolve.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.services import approval
from app.services.payment_facts import append_payment_block
from app.services.tools import Workspace, sha256_text, workspace_root

GOAL = "G-PUBLISH"
RECIPIENTS = [
    "team@cartwave.example.com",
    "hello@tikrank.example.com",
    "ops@binford.example.com",
    "mods@dropshipdeepsix.example.com",
    "builder@ledgerlight.example.com",
]


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """A throwaway `data/work`, so a decision here never lands on the real log.

    WORKSPACE_ROOT is the single lever: Workspace() and approval() both read it,
    so one variable moves the lane, the manifest, the outbox and the decision
    log together. REPO_ROOT is separate, and is what a merge entry would target.
    """
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(approval, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("X402_NETWORK", "base-sepolia")
    monkeypatch.setenv("X402_WALLET_ADDRESS", "0x" + "3" * 40)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.importalpha.test")


def draft_entries(goal_id: str = GOAL, count: int = 5) -> List[Dict[str, Any]]:
    workspace = Workspace(goal_id)
    workspace.ensure()
    entries = []
    for index in range(count):
        body = append_payment_block(
            f"Letter {index + 1}: landed cost is the number nobody computes.\n"
        )
        path = f"outreach/{index + 1:02d}-letter.md"
        workspace.write_file(path, body)
        entries.append(
            {
                "workspace_path": path,
                "target_path": None,
                "kind": "publish",
                "summary": f"letter {index + 1}",
                "sha256": sha256_text(body),
                "bytes": len(body.encode("utf-8")),
                "publish": {
                    "channel": "email",
                    "recipient": RECIPIENTS[index],
                    "note": "dogfood",
                },
            }
        )
    return entries


def queue_batch(count: int = 5, goal_id: str = GOAL, entries=None) -> List[Dict[str, Any]]:
    entries = entries if entries is not None else draft_entries(goal_id, count)
    approval.write_manifest(goal_id, "find 5 e-commerce agent builders", entries)
    return entries


def queued(goal_id: str = GOAL) -> List[Dict[str, Any]]:
    return [item for item in approval.list_outbox() if item["goal_id"] == goal_id]


def decisions() -> List[Dict[str, Any]]:
    path = workspace_root() / "decisions.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- a batch is decided per file ------------------------------------------


def test_two_of_five_is_a_normal_decision_not_an_error():
    queue_batch()
    chosen = ["outreach/01-letter.md", "outreach/03-letter.md"]
    manifest = approval.approve(GOAL, paths=chosen)

    assert manifest["status"] == approval.APPROVED
    assert manifest["approved_files"] == chosen
    assert sorted(manifest["rejected_files"]) == [
        "outreach/02-letter.md",
        "outreach/04-letter.md",
        "outreach/05-letter.md",
    ]


def test_only_the_approved_drafts_are_queued_for_sending():
    queue_batch()
    approval.approve(GOAL, paths=["outreach/01-letter.md", "outreach/03-letter.md"])

    items = queued()
    assert len(items) == 2
    assert {item["publish"]["recipient"] for item in items} == {RECIPIENTS[0], RECIPIENTS[2]}
    assert all(item["published"] is False for item in items), (
        "queueing is not sending: nothing has left the machine yet"
    )


def test_the_three_refused_drafts_are_logged_as_refused_in_the_same_call():
    """The batch closes instead of hanging. A human who approved two of five is
    on the record about the other three, so nothing later can find them and
    read silence as consent."""
    queue_batch()
    approval.approve(GOAL, paths=["outreach/01-letter.md", "outreach/03-letter.md"])

    rows = decisions()
    assert [row["decision"] for row in rows] == [approval.APPROVED, approval.REJECTED]
    assert [f["workspace_path"] for f in rows[1]["files"]] == [
        "outreach/02-letter.md",
        "outreach/04-letter.md",
        "outreach/05-letter.md",
    ]


def test_a_queued_draft_survives_the_workspace_it_came_from():
    """The outbox is a copy, not a view.

    Deciding a batch deletes the goal workspace, so an approved letter that only
    *pointed* into that directory would vanish along with the refused ones. The
    publisher reads the outbox, so the text has to be there after the sweep.
    """
    queue_batch()
    approval.approve(GOAL, paths=["outreach/01-letter.md"])
    shutil.rmtree(Workspace(GOAL).root)

    items = queued()
    assert len(items) == 1
    assert items[0]["body"].startswith("Letter 1")
    assert sha256_text(items[0]["body"]) == items[0]["sha256"]


def test_a_draft_withdrawn_before_it_is_approved_does_not_close_the_batch():
    queue_batch()
    manifest = approval.reject(GOAL, paths=["outreach/05-letter.md"])

    assert manifest["status"] == approval.PENDING, "four drafts are still undecided"
    assert manifest["rejected_files"] == ["outreach/05-letter.md"]
    assert queued() == []


def test_a_batch_can_be_finished_in_several_sittings():
    queue_batch()
    approval.reject(GOAL, paths=["outreach/04-letter.md", "outreach/05-letter.md"])
    approval.approve(GOAL, paths=["outreach/01-letter.md", "outreach/02-letter.md"])

    assert sorted(item["workspace_path"] for item in queued()) == [
        "outreach/01-letter.md",
        "outreach/02-letter.md",
    ]
    assert sorted(approval.load_manifest(GOAL)["rejected_files"]) == [
        "outreach/03-letter.md",
        "outreach/04-letter.md",
        "outreach/05-letter.md",
    ]


def test_rejecting_the_whole_batch_leaves_nothing_behind():
    queue_batch()
    approval.reject(GOAL)

    assert approval.list_outbox() == []
    assert not Workspace(GOAL).root.exists()
    assert decisions()[-1]["decision"] == approval.REJECTED


# --- the log is the authority the publisher checks against ----------------


def test_the_decision_log_carries_the_hash_and_the_target():
    """The publisher re-reads these lines before it sends anything, so a row
    without the content hash and the recipient would not be enough to verify a
    send against. Writing them is the point of the gate, not a side effect."""
    queue_batch()
    approval.approve(GOAL, paths=["outreach/01-letter.md", "outreach/03-letter.md"])

    approved = [row for row in decisions() if row["decision"] == approval.APPROVED]
    assert len(approved) == 1
    assert approved[0]["goal_id"] == GOAL
    files = approved[0]["files"]
    assert [f["workspace_path"] for f in files] == [
        "outreach/01-letter.md",
        "outreach/03-letter.md",
    ]
    for entry in files:
        assert entry["kind"] == approval.PUBLISH
        assert entry["target_path"] is None
        assert entry["sha256"]
        assert entry["publish"]["channel"] == "email"
        assert entry["publish"]["recipient"] in RECIPIENTS


def test_the_queue_and_the_log_agree_byte_for_byte():
    """Otherwise the publisher verifies one thing and sends another."""
    queue_batch()
    approval.approve(GOAL, paths=["outreach/01-letter.md"])
    item = queued()[0]
    logged = [row for row in decisions() if row["decision"] == approval.APPROVED][0]

    assert item["sha256"] == logged["files"][0]["sha256"]
    assert item["publish"] == logged["files"][0]["publish"]
    assert sha256_text(item["body"]) == item["sha256"]


def test_a_body_that_was_never_approved_is_not_approved():
    queue_batch()
    approval.approve(GOAL, paths=["outreach/01-letter.md"])
    approved = queued()[0]
    unchosen = draft_entries(GOAL, 2)[1]

    assert approval.is_approved("outreach/01-letter.md", approved["sha256"], GOAL) is True
    assert approval.is_approved(unchosen["workspace_path"], unchosen["sha256"], GOAL) is False
    assert approval.is_approved("outreach/01-letter.md", unchosen["sha256"], GOAL) is False, (
        "the hash has to match, or approving one letter authorises every letter"
    )
    assert approval.is_approved("outreach/01-letter.md", approved["sha256"], "G-OTHER") is False


def test_an_unreadable_line_in_the_log_does_not_hide_the_lines_around_it():
    """One corrupt row must not make the publisher unable to read the approval
    it is standing on — nor, worse, read a rejection as an approval."""
    queue_batch()
    approval.approve(GOAL, paths=["outreach/01-letter.md"])
    approved = queued()[0]
    path = workspace_root() / "decisions.jsonl"
    path.write_text("{ not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    assert approval.is_approved("outreach/01-letter.md", approved["sha256"], GOAL) is True


# --- a draft never names a place in the tree ------------------------------


def test_a_publish_entry_never_names_a_place_in_the_repository():
    """`target_path` is the field that writes into the tree, so it is forced to
    None whatever a worker put there. A draft has a *recipient*; letting the same
    approved file also carry a repo path would give it two ways out, and the
    publisher only checks one of them."""
    entries = draft_entries(GOAL, 2)
    for entry in entries:
        entry["target_path"] = "README.md"
    queue_batch(entries=entries)

    assert approval.load_manifest(GOAL)["files"][0]["target_path"] is None
    approval.approve(GOAL)
    assert (approval.REPO_ROOT / "README.md").exists() is False
    assert len(queued()) == 2


def test_a_goal_holding_both_kinds_is_decided_by_their_own_rules():
    """The code change is all or nothing; the drafts beside it stay independent.
    Selecting only the drafts is refused — not because a draft cannot be chosen
    alone, but because leaving one merge file behind would close the goal with
    the change undecided."""
    workspace = Workspace(GOAL)
    workspace.ensure()
    code = "def landed_cost():\n    return 1\n"
    workspace.write_file("app/landed_cost.py", code)
    entries = draft_entries(GOAL, 2)
    entries.insert(
        0,
        {
            "workspace_path": "app/landed_cost.py",
            "target_path": "app/landed_cost.py",
            "kind": "merge",
            "summary": "the maths",
            "sha256": sha256_text(code),
            "bytes": len(code),
        },
    )
    queue_batch(entries=entries)

    with pytest.raises(approval.ApprovalError, match="cannot be partially approved"):
        approval.approve(GOAL, paths=["outreach/01-letter.md"])

    assert (approval.REPO_ROOT / "app" / "landed_cost.py").exists() is False
    assert queued() == []
    assert approval.load_manifest(GOAL)["status"] == approval.PENDING

    approval.approve(GOAL)
    assert (approval.REPO_ROOT / "app" / "landed_cost.py").read_text() == code
    assert len(queued()) == 2


def test_a_refused_draft_cannot_be_queued_by_a_later_approve():
    """The ordering the gate has to survive.

    `reject --only` leaves the goal open, and the manifest still lists every
    draft the goal produced. If "what is pending" were read off the manifest
    alone, the following whole-goal approve would see the refused draft as
    undecided: it would be queued, logged as approved, and sent. A human's no is
    not a hold.
    """
    queue_batch()
    approval.reject(GOAL, paths=["outreach/02-letter.md"])
    approval.approve(GOAL)

    assert [item["workspace_path"] for item in queued()] == [
        "outreach/01-letter.md",
        "outreach/03-letter.md",
        "outreach/04-letter.md",
        "outreach/05-letter.md",
    ]
    refused = [row for row in reversed(decisions()) if row["decision"] == approval.REJECTED][0]
    refused_sha = refused["files"][0]["sha256"]
    assert approval.is_approved("outreach/02-letter.md", refused_sha, GOAL) is False, (
        "the refusal must not read as an approval"
    )


def test_the_refused_draft_is_still_refused_when_it_is_the_only_way_out():
    queue_batch()
    approval.reject(GOAL, paths=["outreach/02-letter.md"])
    with pytest.raises(approval.ApprovalError, match="no such pending file"):
        approval.approve(GOAL, paths=["outreach/02-letter.md"])
    assert approval.is_approved("outreach/02-letter.md", "anything", GOAL) is False


# ---------- the CLI's selection flag ----------


def cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "approve.py"
    spec = importlib.util.spec_from_file_location("approve_cli_publish", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repeating_only_approves_every_path_it_names():
    """Two `--only` flags are one decision about two files, not a typo for one.

    Without `action="append"`, argparse keeps the last value and the gate sees a
    single path. The other draft is then not "left pending" — `approve()` closes
    the batch by logging what it was not shown as REJECTED, so a mistyped
    command writes a human refusal that the append-only log cannot take back.
    This happened during the dogfood run, on 1 of 5 letters.
    """
    queue_batch()
    chosen = ["outreach/01-letter.md", "outreach/03-letter.md"]
    argv = ["approve.py", "approve", GOAL]
    for path in chosen:
        argv += ["--only", path]

    assert cli().main(argv) == 0
    assert [item["workspace_path"] for item in queued()] == chosen
    approved = [row for row in decisions() if row["decision"] == approval.APPROVED]
    assert [f["workspace_path"] for f in approved[-1]["files"]] == chosen


def test_the_comma_form_selects_the_same_files_as_the_repeated_form():
    queue_batch()
    chosen = ["outreach/01-letter.md", "outreach/03-letter.md"]

    assert cli().main(["approve.py", "approve", GOAL, "--only", ",".join(chosen)]) == 0
    assert [item["workspace_path"] for item in queued()] == chosen


def test_a_blank_only_is_refused_instead_of_approving_the_batch(capsys):
    """`--only ""` reads as "no path named", and the safe answer is a refusal.

    Falling through to None would mean "everything", so an emptied shell
    variable would approve five letters nobody reviewed.
    """
    queue_batch()

    assert cli().main(["approve.py", "approve", GOAL, "--only", " "]) == 1
    assert "named no path" in capsys.readouterr().err
    assert queued() == []
    assert decisions() == []
