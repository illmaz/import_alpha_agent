"""The gate between what the lane wrote and what the repository serves.

Workers write into `data/work/<goal_id>/` and stop. Nothing they produce
reaches a real path until a person runs `scripts/approve.py approve <goal_id>`.
This module owns that crossing: the manifest that describes a pending change,
the target paths a change may not touch, and the append-only decision log.

Targets are relative to the repository root and are re-validated at approval
time, not only when the manifest is written — the manifest is a file on disk
and could have been edited between the two moments.

Two kinds of manifest entry cross here, and they cross differently:

  merge    a file that becomes part of the repository. All of them, or none:
           half a change merged is a broken tree, so selection is refused.
  publish  a draft aimed at somewhere outside the repository — an email, a
           directory listing, a post. Never written into the tree. Each one is
           independent, so a human may approve two of five and reject three,
           which is exactly what an outreach batch needs.

Publish items are copied into `data/outbox/` when approved. Not for the
publisher's convenience: because `reject()` deletes the workspace, and a batch
where one draft is approved and another rejected would otherwise destroy the
approved text along with the rejected one.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.services.tools import Workspace, sha256_text, workspace_root

logger = logging.getLogger("approval")

REPO_ROOT = Path(__file__).resolve().parents[2]

MANIFEST_NAME = "MANIFEST.json"
DECISIONS_LOG = "decisions.jsonl"
OUTBOX_DIR = "outbox"

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"

# How an approved file reaches its destination.
MERGE, PUBLISH = "merge", "publish"

# Paths the lane may never write, whatever a manifest claims.
#   data/fixtures — curated data is authored by people (docs/DECISIONS.md)
#   data/work     — a goal must not rewrite another goal's workspace
#   .git, .env    — history and secrets
PROTECTED_TARGET_PREFIXES = ("data/fixtures", "data/work", ".git", ".env")


class ApprovalError(RuntimeError):
    """A manifest named a target that may not be written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_path(goal_id: str) -> Path:
    return Workspace(goal_id).root / MANIFEST_NAME


def decisions_log_path() -> Path:
    return workspace_root() / DECISIONS_LOG


def validate_target(target_path: str) -> Path:
    """Resolve a manifest target against the repo root, or refuse it."""
    if not target_path or target_path.startswith("/") or ".." in Path(target_path).parts:
        raise ApprovalError(f"unsafe target path: {target_path!r}")

    # Strip a leading "./" as a prefix. str.lstrip("./") would strip the
    # characters, turning ".git/config" into "git/config" and ".env" into
    # "env" — silently unprotecting exactly the paths that matter most.
    normalised = target_path.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]

    for prefix in PROTECTED_TARGET_PREFIXES:
        if normalised == prefix or normalised.startswith(prefix + "/"):
            raise ApprovalError(
                f"target {target_path!r} is protected; the lane may not write it"
            )

    resolved = (REPO_ROOT / target_path).resolve()
    if REPO_ROOT not in resolved.parents:
        raise ApprovalError(f"target {target_path!r} resolves outside the repository")
    return resolved


def entry_kind(entry: Dict[str, Any]) -> str:
    """Merge unless the entry says it is a draft aimed outside the tree."""
    return PUBLISH if entry.get("kind") == PUBLISH else MERGE


def write_manifest(goal_id: str, goal_text: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Record a pending change. `entries` come from worker file artifacts."""
    files = []
    for entry in entries:
        kind = entry_kind(entry)
        if kind == PUBLISH:
            # No target path at all. A draft has a *recipient*, and letting it
            # also carry a repo path would give one approved file two ways to
            # leave the system — one of which the publisher never checks.
            target = None
            target_exists = False
        else:
            target = entry.get("target_path") or entry["workspace_path"]
            target_exists = (REPO_ROOT / target).is_file()
        record = {
            "workspace_path": entry["workspace_path"],
            "target_path": target,
            "kind": kind,
            "sha256": entry["sha256"],
            "bytes": entry["bytes"],
            "summary": entry.get("summary", ""),
            "target_exists": target_exists,
        }
        if kind == PUBLISH:
            record["publish"] = entry.get("publish") or {}
        files.append(record)

    manifest = {
        "goal_id": goal_id,
        "goal": goal_text,
        "status": PENDING,
        "created_at": _now(),
        "files": files,
    }

    path = manifest_path(goal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info("manifest written for %s (%d file(s))", goal_id, len(files))
    return manifest


def load_manifest(goal_id: str) -> Optional[Dict[str, Any]]:
    path = manifest_path(goal_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_manifests(status: Optional[str] = None) -> List[Dict[str, Any]]:
    root = workspace_root()
    if not root.is_dir():
        return []

    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_file = child / MANIFEST_NAME
        if not manifest_file.is_file():
            continue
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("unreadable manifest in %s", child.name)
            continue
        if status is None or manifest.get("status") == status:
            found.append(manifest)
    return found


def workspace_content(goal_id: str, workspace_path: str) -> str:
    return Workspace(goal_id).read_file(workspace_path)


def current_target_content(target_path: str) -> Optional[str]:
    """What the repository serves today, or None if the target is new."""
    resolved = validate_target(target_path)
    if not resolved.is_file():
        return None
    return resolved.read_text(encoding="utf-8")


def log_decision(goal_id: str, decision: str, files: List[Dict[str, Any]]) -> None:
    """Append-only record of every human call on lane-authored work.

    This is the authority the publisher checks itself against. An event saying
    "approved" is a notification; this log is the evidence, written by the
    process a human ran, with the content hash of what they were shown.
    """
    entry = {
        "goal_id": goal_id,
        "decision": decision,
        "at": _now(),
        "files": [
            {
                "target_path": f.get("target_path"),
                "workspace_path": f.get("workspace_path"),
                "kind": f.get("kind", MERGE),
                "sha256": f["sha256"],
                "bytes": f["bytes"],
                **({"publish": f["publish"]} if f.get("publish") else {}),
            }
            for f in files
        ],
    }
    path = decisions_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    logger.info("%s %s (%d file(s))", decision, goal_id, len(files))


def approved_decisions() -> List[Dict[str, Any]]:
    """Every approved entry in the decision log, oldest first.

    A malformed line is skipped rather than raising: one corrupt row must not
    make the publisher unable to read the rows around it, and the rows it can
    read are the ones that gate a send.
    """
    path = decisions_log_path()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("ignoring unreadable line in %s", path)
            continue
        if isinstance(row, dict) and row.get("decision") == APPROVED:
            out.append(row)
    return out


def is_approved(workspace_path: str, sha256: str, goal_id: Optional[str] = None) -> bool:
    """Did a human approve *these bytes* at this path?

    The hash is the point. A path alone proves nothing, because a file can be
    edited after it was approved, and the publisher is about to send whatever
    it is holding.
    """
    for entry in approved_decisions():
        if goal_id is not None and entry.get("goal_id") != goal_id:
            continue
        for f in entry.get("files", []):
            if f.get("workspace_path") == workspace_path and f.get("sha256") == sha256:
                return True
    return False


def outbox_dir() -> Path:
    return workspace_root() / OUTBOX_DIR


def _outbox_id(goal_id: str, workspace_path: str, sha256: str) -> str:
    return f"{goal_id}::{workspace_path}::{sha256[:16]}"


def outbox_item_name(item_id: str) -> str:
    """Outbox ids contain path separators by design; the filename may not."""
    return item_id.replace("/", "-").replace(":", "_").replace("\\", "-")


def outbox_path(item_id: str) -> Path:
    """Where an item's approved copy lives. One function, because the id is
    derived in one place and a second spelling of it would silently orphan
    files that exist but can no longer be found."""
    return outbox_dir() / f"{outbox_item_name(item_id)}.json"


def read_outbox(item_id: str) -> Optional[Dict[str, Any]]:
    path = outbox_path(item_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_outbox() -> List[Dict[str, Any]]:
    root = outbox_dir()
    if not root.is_dir():
        return []
    items = []
    for child in sorted(root.glob("*.json")):
        try:
            items.append(json.loads(child.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            logger.warning("unreadable outbox item %s", child.name)
    return items


def pending_files(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The entries a human has not decided yet.

    The manifest keeps every file the goal produced, including the ones an
    earlier partial decision already settled, so "pending" has to subtract them.
    It matters: `reject <goal> --only one-draft` leaves the goal open, and
    without this a following `approve <goal>` would treat the refused draft as
    undecided — queue it, log it as approved, and hand the publisher a message a
    human had already said no to.
    """
    decided = set(manifest.get("approved_files", [])) | set(manifest.get("rejected_files", []))
    return [f for f in manifest.get("files", []) if f["workspace_path"] not in decided]


def _select(manifest: Dict[str, Any], paths: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    """Resolve a human's subset, or refuse it.

    Selection is allowed over publish items only. Merge items are one change:
    approving two of three files from a code step would write half a feature
    into the tree, which is the failure mode the all-or-nothing rule exists to
    prevent. A human who wants to merge a subset gets told to split the goal.
    """
    files = pending_files(manifest)
    if paths is None:
        return files

    chosen = [f for f in files if f["workspace_path"] in set(paths)]
    missing = set(paths) - {f["workspace_path"] for f in chosen}
    if missing:
        raise ApprovalError(f"no such pending file: {', '.join(sorted(missing))}")

    merged = [f for f in files if entry_kind(f) == MERGE]
    picked_merged = [f for f in chosen if entry_kind(f) == MERGE]
    if merged and len(picked_merged) != len(merged):
        raise ApprovalError(
            f"these {len(merged)} file(s) are one repository change and cannot be "
            f"partially approved: {', '.join(f['workspace_path'] for f in merged)}"
        )
    return chosen


def approve(goal_id: str, paths: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Let a decision take effect: merge files into the tree, queue drafts.

    Merge is all or nothing. Drafts are per-file, and the ones not chosen here
    are recorded as rejected so the batch closes instead of hanging.
    """
    manifest = load_manifest(goal_id)
    if manifest is None:
        raise ApprovalError(f"no manifest for goal {goal_id}")
    if manifest["status"] != PENDING:
        raise ApprovalError(f"goal {goal_id} is already {manifest['status']}")

    workspace = Workspace(goal_id)
    chosen = _select(manifest, paths)
    not_chosen = [f for f in pending_files(manifest) if f not in chosen]

    # Validate and read everything before writing anything, so a bad entry
    # cannot leave half of a merge applied.
    staged = []
    queued = []
    for entry in chosen:
        content = workspace.read_file(entry["workspace_path"])
        actual = sha256_text(content)
        if actual != entry["sha256"]:
            raise ApprovalError(
                f"{entry['workspace_path']} changed since the manifest was written "
                f"(expected {entry['sha256'][:12]}, found {actual[:12]})"
            )
        if entry_kind(entry) == PUBLISH:
            queued.append((entry, content))
        else:
            staged.append((validate_target(entry["target_path"]), content))

    for target, content in staged:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    for entry, content in queued:
        item = {
            "item_id": _outbox_id(goal_id, entry["workspace_path"], entry["sha256"]),
            "goal_id": goal_id,
            "workspace_path": entry["workspace_path"],
            "sha256": entry["sha256"],
            "bytes": len(content.encode("utf-8")),
            "publish": entry.get("publish") or {},
            "summary": entry.get("summary", ""),
            "body": content,
            "approved_at": _now(),
            # Nothing has left the machine yet, and this says so. The publisher
            # sets it to the channel it used, or leaves it false in a dry run —
            # a dry-run item must stay replayable once real keys exist.
            "published": False,
        }
        target = outbox_path(item["item_id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(item, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    manifest["status"] = APPROVED
    manifest["decided_at"] = _now()
    manifest["approved_files"] = [f["workspace_path"] for f in chosen]
    if not_chosen:
        # Merged, not replaced. A goal can be decided more than once — one draft
        # withdrawn first, the batch approved after — and a rewrite would drop
        # the earlier refusals from the record the CLI shows a human.
        manifest["rejected_files"] = sorted(
            set(manifest.get("rejected_files", [])) | {f["workspace_path"] for f in not_chosen}
        )
    manifest_path(goal_id).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    log_decision(goal_id, APPROVED, chosen)
    if not_chosen:
        # A second line, not a rewrite: the log is append-only, and the earlier
        # record of what was chosen has to stay exactly as it was written.
        log_decision(goal_id, REJECTED, not_chosen)
    return manifest


def reject(goal_id: str, paths: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Discard pending work. The decision is logged before anything is deleted.

    With `paths` it rejects a subset, which is how "reject 3 of 5" is recorded
    while the other two stay queued for the publisher. Without it the whole goal
    goes, and the outbox copies go with it — a rejection is not a hold.
    """
    manifest = load_manifest(goal_id)
    if manifest is None:
        raise ApprovalError(f"no manifest for goal {goal_id}")
    if manifest["status"] != PENDING:
        raise ApprovalError(f"goal {goal_id} is already {manifest['status']}")

    files = pending_files(manifest)
    if paths is None:
        chosen, remainder = files, []
    else:
        chosen = [f for f in files if f["workspace_path"] in set(paths)]
        missing = set(paths) - {f["workspace_path"] for f in chosen}
        if missing:
            raise ApprovalError(f"no such pending file: {', '.join(sorted(missing))}")
        remainder = [f for f in files if f not in chosen]

    log_decision(goal_id, REJECTED, chosen)
    for entry in chosen:
        if entry_kind(entry) != PUBLISH:
            continue
        outbox_path(_outbox_id(goal_id, entry["workspace_path"], entry["sha256"])).unlink(missing_ok=True)

    if not remainder:
        # A whole-goal rejection is the one decision with no record left behind
        # in the workspace, because the workspace is deleted. The append-only
        # log line above is the durable copy; writing a manifest into a
        # directory that has just been removed is how this used to fail.
        shutil.rmtree(Workspace(goal_id).root, ignore_errors=True)
        manifest["status"] = REJECTED
        manifest["decided_at"] = _now()
        return manifest

    # Something is still pending, so the goal stays open and the manifest says
    # which part of it died. Marking it rejected here would let the publisher
    # find an approved outbox item under a "rejected" goal.
    manifest["status"] = PENDING
    manifest["decided_at"] = _now()
    manifest["rejected_files"] = sorted(
        set(manifest.get("rejected_files", [])) | {f["workspace_path"] for f in chosen}
    )
    manifest_path(goal_id).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
