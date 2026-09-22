#!/usr/bin/env python3
"""CLI for accounts, API keys and credits.

    python scripts/manage_accounts.py create-account "test-customer" 100
    python scripts/manage_accounts.py issue-key <account_id> [label]
    python scripts/manage_accounts.py add-credits <account_id> 50
    python scripts/manage_accounts.py balance <account_id>
    python scripts/manage_accounts.py history <account_id> [limit]
    python scripts/manage_accounts.py reconcile [account_id]
    python scripts/manage_accounts.py list-accounts
    python scripts/manage_accounts.py list-keys <account_id>
    python scripts/manage_accounts.py revoke-key <key_hash>

A plaintext key is printed exactly once, at issuance. It is stored only as a
SHA-256 hash, so it cannot be recovered afterwards — issue a new one instead.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import init_models  # noqa: E402
from app.services import auth, billing  # noqa: E402

_SLUG = re.compile(r"[^a-z0-9]+")


def _account_id(label: str) -> str:
    """Readable, collision-resistant id: slug of the label plus random suffix."""
    slug = _SLUG.sub("-", label.strip().lower()).strip("-") or "account"
    return f"{slug[:32]}-{uuid.uuid4().hex[:6]}"


def _print_key_once(key: str) -> None:
    print()
    print("=" * 68)
    print("  API KEY — shown once, not recoverable. Store it now.")
    print("=" * 68)
    print(f"  {key}")
    print("=" * 68)
    print()
    print("  Use it as:")
    print(f'    curl -H "Authorization: Bearer {key}" \\')
    print("      http://localhost:8000/v1/opportunities")
    print()


async def cmd_create_account(args: argparse.Namespace) -> int:
    account_id = args.account_id or _account_id(args.label)
    try:
        await billing.create_account(account_id, args.credits, label=args.label)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    key, key_hash = await auth.issue_api_key(account_id, label=args.label)
    print(f"created account  : {account_id}")
    print(f"label            : {args.label}")
    print(f"initial credits  : {args.credits}")
    print(f"key hash         : {key_hash}")
    _print_key_once(key)
    return 0


async def cmd_issue_key(args: argparse.Namespace) -> int:
    if await billing.get_account(args.account_id) is None:
        print(f"error: no account {args.account_id!r}", file=sys.stderr)
        return 1
    key, key_hash = await auth.issue_api_key(args.account_id, label=args.label)
    print(f"account   : {args.account_id}")
    print(f"key hash  : {key_hash}")
    _print_key_once(key)
    return 0


async def cmd_add_credits(args: argparse.Namespace) -> int:
    try:
        balance = await billing.add_credits(args.account_id, args.credits)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.account_id}: +{args.credits} -> balance {balance}")
    return 0


async def cmd_balance(args: argparse.Namespace) -> int:
    if await billing.get_account(args.account_id) is None:
        print(f"error: no account {args.account_id!r}", file=sys.stderr)
        return 1
    print(f"{args.account_id}: {await billing.get_balance(args.account_id)} credits")
    return 0


async def cmd_history(args: argparse.Namespace) -> int:
    """Print the ledger: the answer to "why is my balance N"."""
    if await billing.get_account(args.account_id) is None:
        print(f"error: no account {args.account_id!r}", file=sys.stderr)
        return 1

    rows = await billing.list_transactions(args.account_id, limit=args.limit)
    total = await billing.count_transactions(args.account_id)
    balance = await billing.get_balance(args.account_id)

    print(f"account  : {args.account_id}")
    print(f"balance  : {balance} credits   ({total} ledger entries)")
    if not rows:
        print("no transactions yet")
        return 0

    print()
    print(f"{'WHEN':<20} {'DELTA':>6}  {'REASON':<11} {'BALANCE':>7}  REFERENCE")
    for row in rows:
        when = row.created_at.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{when:<20} {row.delta:>+6}  {row.reason:<11} "
            f"{row.balance_after:>7}  {row.reference or ''}"
        )

    if total > len(rows):
        print(f"\n... {total - len(rows)} older entries (raise the limit to see them)")
    return 0


async def cmd_reconcile(args: argparse.Namespace) -> int:
    """Check cached balances against the ledger sum."""
    if args.account_id:
        accounts = [await billing.get_account(args.account_id)]
        if accounts[0] is None:
            print(f"error: no account {args.account_id!r}", file=sys.stderr)
            return 1
    else:
        accounts = await billing.list_accounts()

    failures = 0
    for account in accounts:
        cached, derived = await billing.reconcile_detail(account.account_id)
        if cached == derived:
            print(f"  OK       {account.account_id}: {cached}")
        else:
            failures += 1
            print(
                f"  MISMATCH {account.account_id}: cached {cached} != ledger {derived}",
                file=sys.stderr,
            )

    if failures:
        print(f"\n{failures} account(s) do not reconcile.", file=sys.stderr)
        return 1
    print(f"\nall {len(accounts)} account(s) reconcile")
    return 0


async def cmd_list_accounts(_: argparse.Namespace) -> int:
    accounts = await billing.list_accounts()
    if not accounts:
        print("no accounts yet")
        return 0
    print(f"{'ACCOUNT_ID':<42} {'CREDITS':>8}  LABEL")
    for account in accounts:
        print(f"{account.account_id:<42} {account.balance:>8}  {account.label}")
    return 0


async def cmd_list_keys(args: argparse.Namespace) -> int:
    keys = await auth.list_api_keys(args.account_id)
    if not keys:
        print(f"no keys for {args.account_id}")
        return 0
    print(f"{'KEY_HASH':<66} {'ACTIVE':<7} LABEL")
    for row in keys:
        print(f"{row.key_hash:<66} {str(row.active):<7} {row.label}")
    return 0


async def cmd_revoke_key(args: argparse.Namespace) -> int:
    if not await auth.revoke_api_key(args.key_hash):
        print(f"error: no key with hash {args.key_hash!r}", file=sys.stderr)
        return 1
    print(f"revoked {args.key_hash}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_accounts.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-account", help="create an account and issue its first key")
    create.add_argument("label", help='human name, e.g. "test-customer"')
    create.add_argument("credits", nargs="?", type=int, default=0, help="initial credits")
    create.add_argument("--account-id", default=None, help="override the generated id")
    create.set_defaults(func=cmd_create_account)

    issue = sub.add_parser("issue-key", help="issue an additional key for an account")
    issue.add_argument("account_id")
    issue.add_argument("label", nargs="?", default="")
    issue.set_defaults(func=cmd_issue_key)

    add = sub.add_parser("add-credits", help="top up an account")
    add.add_argument("account_id")
    add.add_argument("credits", type=int)
    add.set_defaults(func=cmd_add_credits)

    bal = sub.add_parser("balance", help="show an account balance")
    bal.add_argument("account_id")
    bal.set_defaults(func=cmd_balance)

    history = sub.add_parser("history", help="print the credit ledger")
    history.add_argument("account_id")
    history.add_argument("limit", nargs="?", type=int, default=50)
    history.set_defaults(func=cmd_history)

    rec = sub.add_parser("reconcile", help="check cached balances against the ledger")
    rec.add_argument("account_id", nargs="?", default=None)
    rec.set_defaults(func=cmd_reconcile)

    listing = sub.add_parser("list-accounts", help="list all accounts")
    listing.set_defaults(func=cmd_list_accounts)

    keys = sub.add_parser("list-keys", help="list key metadata for an account")
    keys.add_argument("account_id")
    keys.set_defaults(func=cmd_list_keys)

    revoke = sub.add_parser("revoke-key", help="deactivate a key by its hash")
    revoke.add_argument("key_hash")
    revoke.set_defaults(func=cmd_revoke_key)

    return parser


async def _run(args: argparse.Namespace) -> int:
    await init_models()
    return await args.func(args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
