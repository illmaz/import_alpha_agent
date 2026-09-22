"""API key issuance and verification.

Keys are stored only as SHA-256 hashes. The plaintext is returned once at
creation and never written down, so a dump of `api_keys` yields nothing a
caller could authenticate with.

Why SHA-256 and not bcrypt
--------------------------
bcrypt exists to make *low-entropy human passwords* expensive to brute-force.
These keys are 32 bytes from `secrets.token_urlsafe` — 256 bits of entropy,
which is not brute-forceable regardless of hash speed. Using bcrypt here would
add a deliberate ~100ms cost to *every authenticated request* and buy nothing.
A fast hash over a high-entropy secret is the correct construction, and it is
what API-key schemes generally use.

This reasoning holds only while keys stay machine-generated. If a
user-chosen key is ever accepted, the entropy assumption breaks and this must
become a slow hash.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Optional, Tuple

from sqlalchemy import select

from app.database import get_sessionmaker
from app.models import ApiKey

# Identifies the key's origin in logs and pasted secrets, and lets an obviously
# malformed value be rejected before a database round trip.
KEY_PREFIX = "ia_"

# 32 bytes -> 43 urlsafe characters.
KEY_ENTROPY_BYTES = 32


def generate_api_key() -> str:
    """A fresh plaintext key. Never persisted — hash it before storing."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_ENTROPY_BYTES)}"


def hash_api_key(key: str) -> str:
    """Stable SHA-256 hex digest of a key."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def looks_like_api_key(key: str) -> bool:
    """Cheap shape check, so junk never reaches the database."""
    candidate = key.strip()
    return candidate.startswith(KEY_PREFIX) and len(candidate) > len(KEY_PREFIX) + 20


async def verify_api_key(key: str) -> Optional[str]:
    """Return the account_id this key belongs to, or None.

    None covers every failure the caller should not be able to distinguish:
    malformed, unknown, and revoked all look the same from outside.
    """
    if not key or not looks_like_api_key(key):
        return None

    statement = select(ApiKey).where(ApiKey.key_hash == hash_api_key(key))
    async with get_sessionmaker()() as session:
        row = (await session.execute(statement)).scalar_one_or_none()

    if row is None or not row.active:
        return None
    return row.account_id


async def issue_api_key(account_id: str, label: str = "") -> Tuple[str, str]:
    """Create a key for an account.

    Returns (plaintext_key, key_hash). The plaintext is the only copy that
    will ever exist — the caller must show it to the user now or lose it.
    """
    key = generate_api_key()
    key_hash = hash_api_key(key)

    async with get_sessionmaker()() as session:
        async with session.begin():
            session.add(ApiKey(key_hash=key_hash, account_id=account_id, label=label))

    return key, key_hash


async def revoke_api_key(key_hash: str) -> bool:
    """Deactivate a key by its hash. Returns False if there was no such key."""
    async with get_sessionmaker()() as session:
        async with session.begin():
            row = await session.get(ApiKey, key_hash)
            if row is None:
                return False
            row.active = False
    return True


async def list_api_keys(account_id: str) -> list[ApiKey]:
    """Key metadata for an account. Never includes anything usable as a key."""
    statement = select(ApiKey).where(ApiKey.account_id == account_id)
    async with get_sessionmaker()() as session:
        return list((await session.execute(statement)).scalars().all())


async def clear_api_keys() -> None:
    """Delete every key. Test hook."""
    from sqlalchemy import delete

    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(delete(ApiKey))
