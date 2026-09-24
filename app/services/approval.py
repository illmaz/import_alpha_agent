"""The gate between what the lane wrote and what the repository serves.

Workers write into `data/work/<goal_id>/` and stop. Nothing they produce
reaches a real path until a person runs `scripts/approve.py approve <goal_id>`.
This module owns that crossing: the manifest that describes a pending change,
the target paths a change may not touch, and the append-only decision log.

Targets are relative to the repository root and are re-validated at approval
time, not only when the manifest is written — the manifest is a file on disk
and could have been edited between the two moments.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.tools import Workspace, sha256_text, workspace_root

logger = logging.getLogger("approval")

REPO_ROOT = Path(__file__).resolve().parents[2]

MANIFEST_NAME = "MANIFEST.json"
DECISIONS_LOG = "decisions.jsonl"

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"

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


def write_manifest(goal_id: str, goal_text: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Record a pending change. `entries` come from worker file artifacts."""
    files = []
    for entry in entries:
        target = entry.get("target_path") or entry["workspace_path"]
        files.append(
            {
                "workspace_path": entry["workspace_path"],
                "target_path": target,
                "sha256": entry["sha256"],
                "bytes": entry["bytes"],
                "summary": entry.get("summary", ""),
                "target_exists": (REPO_ROOT / target).is_file(),
            }
        )

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
    """Append-only record of every human call on lane-authored code."""
    entry = {
        "goal_id": goal_id,
        "decision": decision,
        "at": _now(),
        "files": [
            {"target_path": f["target_path"], "sha256": f["sha256"], "bytes": f["bytes"]}
            for f in files
        ],
    }
    path = decisions_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    logger.info("%s %s (%d file(s))", decision, goal_id, len(files))


def approve(goal_id: str) -> Dict[str, Any]:
    """Copy every manifest file onto its target. All or nothing."""
    manifest = load_manifest(goal_id)
    if manifest is None:
        raise ApprovalError(f"no manifest for goal {goal_id}")
    if manifest["status"] != PENDING:
        raise ApprovalError(f"goal {goal_id} is already {manifest['status']}")

    workspace = Workspace(goal_id)

    # Validate and read everything before writing anything, so a bad entry
    # cannot leave half the change applied.
    staged = []
    for entry in manifest["files"]:
        target = validate_target(entry["target_path"])
        content = workspace.read_file(entry["workspace_path"])
        actual = sha256_text(content)
        if actual != entry["sha256"]:
            raise ApprovalError(
                f"{entry['workspace_path']} changed since the manifest was written "
                f"(expected {entry['sha256'][:12]}, found {actual[:12]})"
            )
        staged.append((target, content))

    for target, content in staged:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    manifest["status"] = APPROVED
    manifest["decided_at"] = _now()
    manifest_path(goal_id).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log_decision(goal_id, APPROVED, manifest["files"])
    return manifest


def reject(goal_id: str) -> Dict[str, Any]:
    """Discard the workspace. The decision is logged before anything is deleted."""
    manifest = load_manifest(goal_id)
    if manifest is None:
        raise ApprovalError(f"no manifest for goal {goal_id}")
    if manifest["status"] != PENDING:
        raise ApprovalError(f"goal {goal_id} is already {manifest['status']}")

    log_decision(goal_id, REJECTED, manifest["files"])
    shutil.rmtree(Workspace(goal_id).root, ignore_errors=True)

    manifest["status"] = REJECTED
    manifest["decided_at"] = _now()
    return manifest
