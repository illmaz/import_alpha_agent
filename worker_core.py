"""Shared worker behaviour: one LLM call, one sandboxed deliverable.

The three role workers differ only in their system prompt and artifact type,
so the loop lives here. Two properties are deliberate:

*The model supplies content; this module supplies the path.* A step's target
file is parsed from the step text by `extract_deliverable`, not chosen by the
LLM, so a prompt-injected "write ../../.env" never becomes a path at all.

*A sandbox violation ends the step, not the process.* It emits a failed
artifact instead of raising, so the goal still terminates — a worker that died
mid-step would leave the goal to stall out on its TTL instead.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, List, Optional

from app.services.tools import SandboxViolation, Workspace
from bus import produce
from events import Event
from llm import LLM, get_llm

logger = logging.getLogger("worker")

# Deliverable extensions the lane may author. `.json` is absent on purpose:
# curated data is written by people, and a worker that can emit JSON can
# quietly become the source of numbers we promise are hand-checked.
DELIVERABLE_RE = re.compile(
    r"(?<![\w./-])([A-Za-z0-9_][A-Za-z0-9_/-]*\.(?:html|css|js|md|txt))(?![\w./-])"
)

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\n(.*?)\n?```\s*$", re.DOTALL)


def extract_deliverable(task_text: str) -> Optional[str]:
    """The file a step asks for, or None if the step is analysis only."""
    match = DELIVERABLE_RE.search(task_text or "")
    return match.group(1) if match else None


def strip_code_fences(text: str) -> str:
    """Models wrap file contents in ``` blocks however firmly you ask them not to."""
    match = _FENCE_RE.match(text or "")
    return match.group(1) if match else (text or "").strip()


def deliverable_prompt(task: str, path: str) -> str:
    return (
        f"DELIVERABLE: {path}\n"
        f"TASK: {task}\n\n"
        "Reply with the complete contents of that one file and nothing else: "
        "no explanation before or after, no markdown code fences."
    )


def analysis_prompt(task: str) -> str:
    return f"TASK: {task}\n\nReply with 2-3 plain sentences describing what you concluded."


def handle_step(
    *,
    role: str,
    system_prompt: str,
    artifact_type: str,
    event: Event,
    llm: Optional[LLM] = None,
    # Late-bound rather than `= produce`: a default argument captures the
    # function at definition time, which would make the real Kafka producer
    # unpatchable and hang any test that reached this line.
    producer: Optional[Callable[..., None]] = None,
    output_topic: str = "artifact.created",
) -> Event:
    """Run one assigned step and publish its artifact. Returns the artifact."""
    emit = producer if producer is not None else produce
    task = str(event.payload.get("title", "(untitled)"))
    goal_id = str(event.payload.get("goal_id") or "").strip()
    deliverable = extract_deliverable(task)
    client = llm if llm is not None else get_llm()

    files: List[dict] = []
    status = "draft"

    if deliverable and goal_id:
        print(f"[{role}] {event.task_id}: writing {deliverable}")
        content = strip_code_fences(client.complete(system_prompt, deliverable_prompt(task, deliverable)))
        try:
            workspace = Workspace(goal_id)
            workspace.ensure()
            record = workspace.write_file(deliverable, content)
        except SandboxViolation as exc:
            logger.warning("step %s refused: %s", event.task_id, exc)
            summary = f"Refused: {exc}"
            status = "failed"
        else:
            files.append(
                {
                    "workspace_path": record.path,
                    "target_path": record.path,
                    "sha256": record.sha256,
                    "bytes": record.bytes,
                    "summary": f"{role}: {task}"[:160],
                }
            )
            summary = (
                f"Wrote {record.path} ({record.bytes} bytes, "
                f"sha256={record.sha256[:12]}) into workspace {goal_id}."
            )
    else:
        if deliverable and not goal_id:
            logger.warning("step %s names %s but carries no goal_id", event.task_id, deliverable)
        print(f"[{role}] {event.task_id}: {task}")
        summary = client.complete(system_prompt, analysis_prompt(task)).strip()

    artifact = Event(
        event_type="artifact.created",
        task_id=event.task_id,
        agent=f"{role}_worker",
        payload={
            "artifact_type": artifact_type,
            "summary": summary,
            "status": status,
            "goal_id": goal_id,
            # Still empty, still for the same reason: a datapoint needs
            # source_url, observed_at and confidence, and the lane has no
            # sources wired. Prose and code are not datapoints.
            "datapoints": [],
            "files": files,
        },
    )
    emit(output_topic, artifact, key=event.task_id)
    print(f"[{role}] artifact.created  {event.task_id}  ->  '{output_topic}'")
    return artifact
