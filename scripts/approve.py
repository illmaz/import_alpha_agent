#!/usr/bin/env python3
"""Review what the agent lane wrote, and decide.

    scripts/approve.py list
    scripts/approve.py show <goal_id>
    scripts/approve.py approve <goal_id> [--only <path>[,<path>]] [--notify]
    scripts/approve.py reject <goal_id> [--only <path>[,<path>]]

`--only` may be repeated (`--only a --only b`) or comma-separated
(`--only a,b`); both name the same two files.

`show` prints a unified diff against the file the repository serves today, or
the full contents when the target is new. A drafted message or directory
submission has no repository target to diff against, so `show` prints the exact
bytes that would leave the machine, with the channel and the recipient above
them: approving a draft means approving that text, in full, to that address.

Nothing the lane writes reaches a real path without `approve`, and every
decision is appended to data/work/decisions.jsonl.

`--only` picks a subset. It works on drafts, which are independent of each
other, and is refused on repository files, which are one change: half a merged
code step is a broken tree.

`approve` writes the decision and stops; a draft it accepts goes into
data/work/outbox/, still on this machine. Give it `--notify` to put a
`human.approval.approved` event on the bus for the publisher to notice. The
event is a notification and the decision log is the authority — the publisher
re-reads the log and refuses anything without an approved entry there, so a
hand-produced event approves nothing.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from events import HUMAN_APPROVAL_APPROVED, Event  # noqa: E402
from app.services.approval import (  # noqa: E402
    ApprovalError,
    PUBLISH,
    approve as approve_goal,
    current_target_content,
    entry_kind,
    list_manifests,
    load_manifest,
    reject as reject_goal,
    workspace_content,
)

PENDING = "pending"


def _parse_only(raw: Optional[Sequence[str]]) -> Optional[List[str]]:
    """Flatten `--only a,b --only c` into [a, b, c], or None for "everything".

    The flag repeats on purpose. With a single value, `--only a --only b` is
    argparse last-wins: the human asks to approve two drafts, the gate settles
    only the second, and the first is written to the append-only log as a
    rejection they never made. Nothing downstream can undo that line.
    """
    if raw is None:
        return None
    if isinstance(raw, str):  # callers that pass the old shape still work
        raw = [raw]
    out = [part.strip() for value in raw for part in value.split(",") if part.strip()]
    if not out:
        # A blank --only must not fall through to "everything": that would
        # approve a whole batch nobody looked at.
        raise ApprovalError("--only was given but named no path")
    return out


def _aim(entry: dict) -> str:
    """Where a draft is headed, as one column."""
    publish = entry.get("publish") or {}
    channel = str(publish.get("channel") or "?")
    recipient = str(publish.get("recipient") or "(broadcast)")
    return f"{channel} -> {recipient}"


def cmd_list(_: argparse.Namespace) -> int:
    manifests = list_manifests()
    if not manifests:
        print("No manifests. The lane has not produced any files yet.")
        return 0

    for manifest in manifests:
        marker = "*" if manifest["status"] == PENDING else " "
        print(f"{marker} {manifest['goal_id']}  [{manifest['status']}]  {manifest['created_at']}")
        print(f"    {manifest['goal'][:88]}")
        for entry in manifest["files"]:
            if entry_kind(entry) == PUBLISH:
                # No repository target exists for a draft, so the line names the
                # destination instead: "who gets this" is the decision.
                print(
                    f"    {'publish':9} {entry['workspace_path']}  "
                    f"[{_aim(entry)}]  ({entry['bytes']} bytes, sha256={entry['sha256'][:12]})"
                )
                continue
            verb = "overwrite" if entry["target_exists"] else "create"
            print(
                f"    {verb:9} {entry['target_path']}  "
                f"({entry['bytes']} bytes, sha256={entry['sha256'][:12]})"
            )
    pending = sum(1 for m in manifests if m["status"] == PENDING)
    print(f"\n{pending} pending. Review with: scripts/approve.py show <goal_id>")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.goal_id)
    if manifest is None:
        print(f"No manifest for {args.goal_id}.", file=sys.stderr)
        return 1

    print(f"goal    : {manifest['goal_id']}  [{manifest['status']}]")
    print(f"request : {manifest['goal']}")
    print(f"created : {manifest['created_at']}\n")

    drafts = [entry for entry in manifest["files"] if entry_kind(entry) == PUBLISH]
    if drafts:
        print("These are drafts, not repository changes. Approving one queues the exact")
        print("text below for the publisher; it does not merge anything.\n")

    for entry in manifest["files"]:
        workspace_path = entry["workspace_path"]
        publish_entry = entry_kind(entry) == PUBLISH
        print("=" * 76)
        if publish_entry:
            print(f"{workspace_path}   [{_aim(entry)}]")
        else:
            print(f"{entry['target_path']}   ({entry['bytes']} bytes, sha256={entry['sha256'][:12]})")
        print(f"summary: {entry['summary']}")
        if publish_entry:
            print(f"select : --only {workspace_path}")
        print("=" * 76)

        try:
            proposed = workspace_content(args.goal_id, workspace_path)
        except (FileNotFoundError, ApprovalError) as exc:
            print(f"  !! cannot read workspace file: {exc}\n", file=sys.stderr)
            continue

        if publish_entry:
            # There is nothing to diff a letter against, and the hash is the
            # thing the publisher will re-check, so it goes in the header.
            print(
                f"(a draft as it would leave the machine — {len(proposed.splitlines())} lines, "
                f"sha256={entry['sha256'][:12]})\n"
            )
            print(proposed)
            print()
            continue

        try:
            current = current_target_content(entry["target_path"])
        except ApprovalError as exc:
            print(f"  !! {exc}\n", file=sys.stderr)
            continue

        if current is None:
            print(f"(new file — full contents, {len(proposed.splitlines())} lines)\n")
            print(proposed)
        else:
            diff = list(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    proposed.splitlines(keepends=True),
                    fromfile=f"a/{entry['target_path']} (current)",
                    tofile=f"b/{entry['target_path']} (proposed)",
                )
            )
            if not diff:
                print("(identical to the current file — approving would change nothing)")
            else:
                added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
                removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
                print(f"(overwrites an existing file: +{added} / -{removed} lines)\n")
                sys.stdout.writelines(diff)
        print()

    if manifest["status"] == PENDING:
        print(f"Approve with: scripts/approve.py approve {args.goal_id}")
        print(f"Discard with: scripts/approve.py reject  {args.goal_id}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    try:
        only = _parse_only(args.only)
        manifest = approve_goal(args.goal_id, paths=only)
    except (ApprovalError, FileNotFoundError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    chosen = set(manifest.get("approved_files", [f["workspace_path"] for f in manifest["files"]]))
    merged = queued = 0
    for entry in manifest["files"]:
        if entry["workspace_path"] not in chosen:
            continue
        if entry_kind(entry) == PUBLISH:
            queued += 1
            print(f"queued  {entry['workspace_path']}  [{_aim(entry)}]  ({entry['bytes']} bytes)")
        else:
            merged += 1
            print(f"wrote   {entry['target_path']}  ({entry['bytes']} bytes)")

    print(f"\nApproved {args.goal_id}: {merged} merged, {queued} queued for publication.")
    print("Logged to data/work/decisions.jsonl. Nothing has left this machine.")

    if queued and args.notify:
        code, message = _notify(manifest, chosen)
        print(message)
        return code
    if queued:
        print("Tell the publisher with: scripts/approve.py approve ... --notify")
    return 0


def _notify(manifest: dict, chosen) -> tuple:
    """Announce an approval on the bus.

    The decision is already durable in the log before this runs, and the
    publisher reads that log rather than trusting what arrives here. So a
    broker that is down is a nuisance, not a lost approval: report it, exit
    non-zero so a script notices, and leave the decision standing.
    """
    try:
        from bus import produce

        items = [
            {
                "workspace_path": entry["workspace_path"],
                "sha256": entry["sha256"],
                "publish": entry.get("publish") or {},
            }
            for entry in manifest["files"]
            if entry["workspace_path"] in chosen and entry_kind(entry) == PUBLISH
        ]
        produce(
            HUMAN_APPROVAL_APPROVED,
            Event(
                event_type=HUMAN_APPROVAL_APPROVED,
                task_id=manifest["goal_id"],
                agent="approve.py",
                payload={
                    "goal_id": manifest["goal_id"],
                    "goal": manifest.get("goal", ""),
                    "items": items,
                    "decided_at": manifest.get("decided_at", ""),
                },
            ),
            key=manifest["goal_id"],
        )
    except Exception as exc:  # noqa: BLE001 - the decision stands regardless
        return 1, f"  !! approved, but the bus refused the notification: {exc}"
    return 0, f"Notified: {HUMAN_APPROVAL_APPROVED} ({len(items)} item(s)) for {manifest['goal_id']}."


def cmd_reject(args: argparse.Namespace) -> int:
    only = _parse_only(args.only)
    try:
        manifest = reject_goal(args.goal_id, paths=only)
    except ApprovalError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    if only is None:
        print(f"Rejected {args.goal_id}; workspace discarded. {len(manifest['files'])} file(s) dropped.")
        return 0
    print(
        f"Rejected {len(only)} file(s) of {args.goal_id}; "
        f"{len(manifest['files']) - len(only)} still pending."
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="approve.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show every manifest").set_defaults(func=cmd_list)
    p = sub.add_parser("show", help="diff a goal's files against what is served today")
    p.add_argument("goal_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="merge repository files and queue drafts")
    p.add_argument("goal_id")
    p.add_argument(
        "--only",
        action="append",
        metavar="PATH[,PATH]",
        help="approve only these paths; repeat the flag or separate them with commas",
    )
    p.add_argument(
        "--notify",
        action="store_true",
        help="produce human.approval.approved so the publisher can see it",
    )
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="discard the goal's workspace, or named drafts")
    p.add_argument("goal_id")
    p.add_argument(
        "--only",
        action="append",
        metavar="PATH[,PATH]",
        help="reject only these paths; repeat the flag or separate them with commas",
    )
    p.set_defaults(func=cmd_reject)

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
