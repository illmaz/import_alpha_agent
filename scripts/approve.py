#!/usr/bin/env python3
"""Review and merge what the agent lane wrote.

    scripts/approve.py list
    scripts/approve.py show <goal_id>
    scripts/approve.py approve <goal_id>
    scripts/approve.py reject <goal_id>

`show` prints a unified diff against the file the repository serves today, or
the full contents when the target is new. Nothing the lane writes reaches a
real path without `approve`, and every decision is appended to
data/work/decisions.jsonl.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.approval import (  # noqa: E402
    ApprovalError,
    approve as approve_goal,
    current_target_content,
    list_manifests,
    load_manifest,
    reject as reject_goal,
    workspace_content,
)

PENDING = "pending"


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

    for entry in manifest["files"]:
        target, workspace_path = entry["target_path"], entry["workspace_path"]
        print("=" * 76)
        print(f"{target}   ({entry['bytes']} bytes, sha256={entry['sha256'][:12]})")
        print(f"summary: {entry['summary']}")
        print("=" * 76)

        try:
            proposed = workspace_content(args.goal_id, workspace_path)
        except (FileNotFoundError, ApprovalError) as exc:
            print(f"  !! cannot read workspace file: {exc}\n", file=sys.stderr)
            continue

        try:
            current = current_target_content(target)
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
                    fromfile=f"a/{target} (current)",
                    tofile=f"b/{target} (proposed)",
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
        manifest = approve_goal(args.goal_id)
    except (ApprovalError, FileNotFoundError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    for entry in manifest["files"]:
        print(f"wrote {entry['target_path']}  ({entry['bytes']} bytes)")
    print(f"\nApproved {args.goal_id}. Logged to data/work/decisions.jsonl.")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    try:
        manifest = reject_goal(args.goal_id)
    except ApprovalError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    print(f"Rejected {args.goal_id}; workspace discarded. {len(manifest['files'])} file(s) dropped.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="approve.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show every manifest").set_defaults(func=cmd_list)
    for name, func, helptext in (
        ("show", cmd_show, "diff a goal's files against what is served today"),
        ("approve", cmd_approve, "copy the goal's files onto their targets"),
        ("reject", cmd_reject, "discard the goal's workspace"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("goal_id")
        p.set_defaults(func=func)

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
