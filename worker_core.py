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

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable, List, Optional

from app.services.payment_facts import (
    PaymentNotConfigured,
    append_payment_block,
    human_only_violations,
)
from app.services.tools import SandboxViolation, Workspace
from bus import produce
from events import Event
from llm import LLM, get_llm
from search import (
    SearchAPI,
    SearchError,
    get_search,
    validate_target_url,
)

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


# ==========================================================================
# Tool-using workers
# ==========================================================================
# The three workers above make one LLM call and write one file. Outreach and
# marketing need to look around first, so they run a bounded loop instead.
#
# The loop is capped twice over — by `max_tool_calls` and by the workspace
# size limit — because the liveness invariant from P2.5 says every goal
# terminates. An agent that can call tools is an agent that can loop, and a
# loop with no ceiling is a goal that never completes.
#
# What is deliberately absent: any tool that sends, posts, submits or spends.
# `TOOLS` below is the entire surface a model can reach, and publishing lives
# in a different process that no worker has a handle on. A prompt cannot
# conjure a function that was never passed in — the same reasoning that left
# network and shell out of app/services/tools.py.

ALLOWED_TOOLS = ("search_web", "read_url")

# Where a draft may be aimed. Anything else is refused rather than passed
# through, so a made-up channel cannot reach the publisher and be silently
# routed to whatever default it has.
PUBLISH_CHANNELS = ("email", "directory", "social")

MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", "18"))
MAX_OBSERVATION_CHARS = 4000

# A step may name a directory ("write drafts under outreach/") or an exact
# file. Both are parsed from the step text, never from model output.
_OUTPUT_DIR_RE = re.compile(r"(?<![\w./-])([a-z0-9][a-z0-9_-]{1,31})/")


def extract_output_dir(task: str, role: str) -> str:
    """The directory a step's files land in. Falls back to '<role>_out'."""
    match = _OUTPUT_DIR_RE.search(task or "")
    candidate = match.group(1) if match else f"{role}_out"
    # Defence in depth: the regex above already excludes `.` and `/`, but this
    # value becomes a path segment, so it is checked again where it is used.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", candidate):
        return f"{role}_out"
    return candidate


def output_path_for(task: str, role: str, index: int) -> str:
    """Path of the index-th (1-based) file this step produces.

    The model never names a path. If the step text asks for `outreach/note.md`
    that is the first file; further files are numbered beside it. So a worker
    that produces five drafts writes five distinct paths it had no say in,
    which is what keeps a prompt-injected "../.env" structurally impossible
    here as well as inside the sandbox.
    """
    directory = extract_output_dir(task, role)
    named = extract_deliverable(task)
    if named:
        stem, dot, ext = Path(named).name.rpartition(".")
        if index == 1:
            return named
        return f"{directory}/{stem}-{index}.{ext}" if dot else f"{directory}/{named}-{index}"
    return f"{directory}/draft-{index}.md"


def parse_tool_action(raw: str) -> tuple[Optional[dict], str]:
    """One action object from model output, or why it was not readable.

    Tolerant of the ``` fences models add regardless of instructions, and of
    a JSON object wrapped in a sentence. What it will not do is guess: an
    unparseable turn costs the worker one iteration of its budget and tells
    the model so.
    """
    text = strip_code_fences(raw).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None, "reply was not a JSON action object"
        text = text[start : end + 1]
    try:
        action = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"unparseable JSON action: {exc}"
    if not isinstance(action, dict):
        return None, "action must be a JSON object"
    return action, ""


def tool_user_prompt(task: str, transcript: List[str], turn: int, budget: int, written: int) -> str:
    """The one-turn-ahead view a worker gives its model.

    The whole transcript is re-sent because the LLM interface here is
    single-turn by design (one static system prompt, cache-friendly). It is
    capped by the tool budget, so this cannot grow without bound.
    """
    head = [
        f"TASK: {task}",
        f"TURN: {turn} of at most {budget}. Files written so far: {written}.",
        "",
        "Reply with exactly ONE JSON object and nothing else. One of:",
        '  {"call": {"tool": "search_web", "args": {"query": "...", "max_results": 5}}}',
        '  {"call": {"tool": "read_url", "args": {"url": "https://..."}}}',
        '  {"write": {"content": "<full file text>", "channel": "email|directory|social",'
        ' "recipient": "<where it is aimed>", "summary": "<one line>"}}',
        '  {"done": "<2-3 sentences describing what you produced>"}',
        "",
        "You may only read: search_web and read_url. There is no tool to send,"
        " post or submit anything — write a draft and stop. A human reviews every",
        "draft before it leaves this system, so write it as though they will.",
        "Write one file per target, and only what the lookups actually returned.",
    ]
    if transcript:
        head += ["", "Transcript:"] + transcript
    return "\n".join(head)


def _observe(label: str, payload: object) -> str:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    if len(body) > MAX_OBSERVATION_CHARS:
        body = body[:MAX_OBSERVATION_CHARS] + ' ...truncated}'
    return f"OBSERVATION {label} -> {body}"


def _publish_target(channel: object, recipient: object) -> dict:
    """Validate the aim of a draft. Unknown channels are refused.

    A draft with nowhere to go is not a draft, it is a note: the publisher
    cannot aim at it, and a human approving it would be approving "send this
    somewhere". So each channel declares what it needs and the check is done
    here, once, rather than at send time when the transcript is gone.
    """
    clean_channel = str(channel or "").strip().lower()
    clean_recipient = str(recipient or "").strip()
    if clean_channel not in PUBLISH_CHANNELS:
        return {
            "channel": "none",
            "recipient": "",
            "note": f"refused unknown publish channel {clean_channel!r}",
        }
    is_url = clean_recipient.startswith(("http://", "https://"))
    if is_url:
        try:
            clean_recipient = validate_target_url(clean_recipient)
        except SearchError as exc:
            return {"channel": "none", "recipient": "", "note": f"refused target: {exc}"}
    if clean_channel == "email":
        # An email destination is the one target that is not a URL, and it is
        # checked loosely on purpose: this is a shape test, not an SMTP handsh-
        # ake. Nothing is sent from here, so a false negative costs a human
        # noticing a bad address at the gate, not a bounce.
        #
        # A page URL is refused here rather than carried through, because an
        # item that reaches the publisher with `to: https://…` is a send that
        # cannot succeed and a queue the human has to read through to discover
        # that. A prospect with no published address gets no draft; the lookup
        # that failed to find a route is the finding.
        if is_url or "@" not in clean_recipient or "." not in clean_recipient.split("@")[-1]:
            return {
                "channel": "none",
                "recipient": "",
                "note": "refused email draft with no address in the recipient",
            }
    elif is_url:
        pass
    elif clean_recipient:
        return {
            "channel": "none",
            "recipient": "",
            "note": f"refused {clean_channel} target {clean_recipient[:60]!r}: not a URL",
        }
    if clean_channel in ("email", "directory") and not clean_recipient:
        return {"channel": "none", "recipient": "", "note": f"refused {clean_channel} draft with no target"}
    return {"channel": clean_channel, "recipient": clean_recipient[:300], "note": ""}


def handle_tool_step(
    *,
    role: str,
    system_prompt: str,
    artifact_type: str,
    event: Event,
    llm: Optional[LLM] = None,
    search: Optional[SearchAPI] = None,
    producer: Optional[Callable[..., None]] = None,
    output_topic: str = "artifact.created",
    max_tool_calls: int = MAX_TOOL_CALLS,
    base_url: Optional[str] = None,
) -> Event:
    """Run one research-then-draft step and publish its artifacts.

    Emits one artifact carrying one file entry per draft, each bound to its
    content by sha256. Nothing is written outside the goal's workspace and
    nothing is sent anywhere at all.
    """
    emit = producer if producer is not None else produce
    task = str(event.payload.get("title", "(untitled)"))
    goal_id = str(event.payload.get("goal_id") or "").strip()
    client = llm if llm is not None else get_llm()
    engine = search if search is not None else get_search()

    files: List[dict] = []
    transcript: List[str] = []
    summary = ""
    status = "draft"
    lookups = 0
    refused: List[str] = []

    if not goal_id:
        # No workspace means nowhere to put a draft, so the loop would run and
        # discard. Terminate honestly instead of pretending to work.
        artifact = Event(
            event_type="artifact.created",
            task_id=event.task_id,
            agent=f"{role}_worker",
            payload={
                "artifact_type": artifact_type,
                "summary": f"Refused: step {event.task_id} carries no goal_id, so no workspace exists.",
                "status": "failed",
                "goal_id": "",
                "datapoints": [],
                "files": [],
            },
        )
        emit(output_topic, artifact, key=event.task_id)
        return artifact

    workspace = Workspace(goal_id)
    workspace.ensure()
    tools: dict = {"search_web": engine.search, "read_url": engine.read_url}

    for turn in range(1, max_tool_calls + 1):
        user = tool_user_prompt(task, transcript, turn, max_tool_calls, len(files))
        raw = client.complete(system_prompt, user)
        action, error = parse_tool_action(raw)

        if error:
            transcript.append(f"OBSERVATION turn {turn} rejected -> {error}")
            continue

        if "call" in action or "write" in action:
            # The model's own action goes into the transcript before its result.
            # Without it a loop has no memory of what it already asked, and a
            # model that cannot see its own history repeats one query until the
            # budget runs out — which looks like a broken tool and is not.
            # A write is echoed without its content: the draft is already in the
            # file, and pasting it back into the prompt would spend the context
            # on a copy of what the model just wrote.
            if "call" in action:
                echoed = action.get("call") or {}
            else:
                write = action.get("write") or {}
                echoed = {key: value for key, value in write.items() if key != "content"}
            transcript.append(f"ACTION {json.dumps(echoed, ensure_ascii=False, default=str)[:600]}")

        if "call" in action:
            call = action.get("call") or {}
            name = str(call.get("tool") or "")
            args = call.get("args") or {}
            if name not in tools:
                # The interesting rejection: a model asking for a tool that
                # does not exist is told it does not exist.
                refused.append(name)
                transcript.append(
                    _observe(
                        f"{name} unavailable",
                        {"error": f"no such tool. allowed: {', '.join(ALLOWED_TOOLS)}"},
                    )
                )
                continue
            lookups += 1
            try:
                result = _run_tool(name, tools[name], args)
            except SearchError as exc:
                transcript.append(_observe(name, {"error": str(exc)}))
                continue
            transcript.append(_observe(name, result))
            continue

        if "write" in action:
            write = action.get("write") or {}
            content = write.get("content")
            if not isinstance(content, str) or not content.strip():
                transcript.append(_observe("write", {"error": "content missing or empty"}))
                continue
            channel = _publish_target(write.get("channel"), write.get("recipient"))
            if channel["channel"] == "none":
                refused.append(channel["note"])
                transcript.append(_observe("write", {"error": channel["note"]}))
                continue
            # The ban list is applied to the model's own words, before the
            # terms are attached, and a violation is sent back as a refused
            # write rather than dropped: the transcript shows the model what it
            # has to fix, and a human never has to read a draft that fails a
            # rule the code could have enforced.
            banned = human_only_violations(content)
            if banned:
                refused.extend(banned)
                transcript.append(
                    _observe(
                        "write refused",
                        {"error": f"human-gated ask: {', '.join(banned)}. Rewrite without it."},
                    )
                )
                continue
            # The terms go in now, before the bytes are hashed, so the thing a
            # human approves is character-for-character the thing the publisher
            # would send. Appending at send time instead would mean approving
            # one text and shipping another.
            try:
                content = append_payment_block(content, base_url)
            except PaymentNotConfigured as exc:
                refused.append(f"payment terms unavailable: {exc}")
                transcript.append(_observe("write", {"error": f"refused: {exc}"}))
                continue
            path = output_path_for(task, role, len(files) + 1)
            try:
                record = workspace.write_file(path, content)
            except SandboxViolation as exc:
                logger.warning("step %s refused: %s", event.task_id, exc)
                summary = f"Refused: {exc}"
                status = "failed"
                break
            files.append(
                {
                    "workspace_path": record.path,
                    # None, not a repo path: a draft is aimed at an outside
                    # destination, not merged into the tree. The gate that
                    # clears this to leave the system is the same human
                    # approval, but via the publisher, never via a merge.
                    "target_path": None,
                    "kind": "publish",
                    "publish": channel,
                    "sha256": record.sha256,
                    "bytes": record.bytes,
                    "summary": str(write.get("summary") or f"{role}: {task}")[:160],
                }
            )
            # The recipient is echoed back deliberately. Without it a model has
            # no way to know which prospect a path already covers, and its only
            # alternative is to guess — which is how one target gets written to
            # five times while four others are never contacted at all.
            transcript.append(
                _observe(
                    "write",
                    {
                        "path": record.path,
                        "sha256": record.sha256[:12],
                        "channel": channel["channel"],
                        "recipient": channel["recipient"],
                    },
                )
            )
            continue

        if "done" in action:
            summary = str(action.get("done") or "").strip()
            break

        transcript.append(_observe("action", {"error": "expected one of call/write/done"}))

    if not summary:
        # Reached when the budget is spent without a `done`. The step still
        # emits an artifact, so the goal completes rather than stalling.
        summary = (
            f"Stopped after {lookups} lookup(s) and {len(files)} draft(s): "
            f"tool budget of {max_tool_calls} turn(s) exhausted."
        )
        status = "partial" if files else "failed"
    elif not files:
        # A loop that declares itself finished having written nothing has
        # failed, whatever it says. Called a draft it would complete the goal
        # and raise an approval request over an empty list of files.
        status = "failed"
        if refused:
            summary += " Nothing was written. Refused: " + "; ".join(sorted(set(refused))[:3])

    artifact = Event(
        event_type="artifact.created",
        task_id=event.task_id,
        agent=f"{role}_worker",
        payload={
            "artifact_type": artifact_type,
            "summary": summary,
            "status": status,
            "goal_id": goal_id,
            "datapoints": [],
            "files": files,
            "lookups": lookups,
            "refused_tools": sorted(set(refused)),
        },
    )
    emit(output_topic, artifact, key=event.task_id)
    print(
        f"[{role}] artifact.created  {event.task_id}  {len(files)} draft(s)  "
        f"{lookups} lookup(s)  ->  '{output_topic}'"
    )
    return artifact


def _run_tool(name: str, fn: Callable[..., object], args: dict) -> object:
    """Invoke a lookup with validated arguments, and normalise the result."""
    args = args if isinstance(args, dict) else {}
    if name == "search_web":
        query = str(args.get("query") or "").strip()
        if not query:
            raise SearchError("search_web needs a non-empty 'query'")
        limit = args.get("max_results", 5)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        results = fn(query, max_results=limit)
        return {"query": query, "results": [r.to_dict() for r in results]}
    url = str(args.get("url") or "").strip()
    if not url:
        raise SearchError("read_url needs a non-empty 'url'")
    return fn(url).to_dict()
