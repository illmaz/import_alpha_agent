"""Sandboxed file tools. The only hands a worker has.

A worker may read, write and list inside `data/work/<goal_id>/` and nowhere
else. There is no network tool and no shell tool, by omission rather than by
policy: capabilities that do not exist cannot be talked into existing by a
prompt. What an LLM writes is untrusted input, so the path it lands on is
chosen by this module, never by the model.

Escapes fail loudly. `..`, absolute paths and symlinks pointing outside the
workspace all raise `SandboxViolation` and log a warning — a silent clamp
would let a confused worker believe it had written something it had not.

Nothing here reaches the repository tree. Getting a file out of a workspace
and onto a real path is a separate, human-gated step: scripts/approve.py.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List

logger = logging.getLogger("tools")

# A goal id becomes a directory name, so it may not contain a separator.
GOAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DEFAULT_WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / "data" / "work"

# Refused as deliverables even inside the workspace. Curated data is written by
# people; a worker that can emit a fixture can quietly become the source of the
# numbers we promise are hand-checked. See docs/DECISIONS.md.
FORBIDDEN_SEGMENTS = frozenset({"fixtures"})

MAX_FILE_BYTES = 512 * 1024


class SandboxViolation(RuntimeError):
    """A path escaped the workspace, or named something a worker may not touch."""


@dataclass(frozen=True)
class FileRecord:
    """What a worker produced, addressed by content."""

    path: str
    sha256: str
    bytes: int


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def workspace_root() -> Path:
    """Read the env each call so tests can point it at a tmp_path."""
    return Path(os.environ.get("WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_ROOT))).resolve()


class Workspace:
    """A single goal's scratch directory."""

    def __init__(self, goal_id: str) -> None:
        if not GOAL_ID_RE.match(goal_id or ""):
            raise SandboxViolation(f"unusable goal_id for a workspace: {goal_id!r}")
        self.goal_id = goal_id
        self.root = workspace_root() / goal_id

    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    # ---------- the boundary ----------

    def resolve(self, relpath: str) -> Path:
        """Map a worker-supplied relative path to a real one inside the root.

        Every read, write and list goes through here. Raises rather than
        returning a clamped path.
        """
        if not isinstance(relpath, str) or not relpath.strip():
            self._refuse(relpath, "empty path")

        candidate = PurePosixPath(relpath)
        if candidate.is_absolute():
            self._refuse(relpath, "absolute path")
        if ".." in candidate.parts:
            self._refuse(relpath, "parent traversal")
        if FORBIDDEN_SEGMENTS.intersection(part.lower() for part in candidate.parts):
            self._refuse(relpath, "names a protected directory")

        root = self.root.resolve()
        # resolve() follows symlinks, so a link planted inside the workspace
        # that points outside it is caught here rather than followed.
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            self._refuse(relpath, "resolves outside the workspace")

        return resolved

    def _refuse(self, relpath: object, reason: str) -> None:
        message = f"sandbox violation in {self.goal_id}: {reason} ({relpath!r})"
        logger.warning(message)
        raise SandboxViolation(message)

    # ---------- the tools ----------

    def write_file(self, relpath: str, content: str) -> FileRecord:
        target = self.resolve(relpath)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            self._refuse(relpath, f"exceeds {MAX_FILE_BYTES} bytes")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        record = FileRecord(path=relpath, sha256=sha256_text(content), bytes=len(encoded))
        logger.info(
            "wrote %s (%d bytes, sha256=%s) in %s",
            relpath,
            record.bytes,
            record.sha256[:12],
            self.goal_id,
        )
        return record

    def read_file(self, relpath: str) -> str:
        target = self.resolve(relpath)
        if not target.is_file():
            raise FileNotFoundError(f"{relpath} does not exist in workspace {self.goal_id}")
        return target.read_text(encoding="utf-8")

    def list_dir(self, relpath: str = ".") -> List[str]:
        target = self.resolve(relpath)
        if not target.is_dir():
            return []
        root = self.root.resolve()
        return sorted(
            str(child.relative_to(root)) for child in target.iterdir()
        )
