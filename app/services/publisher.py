"""Publisher — the one place a draft is allowed to become a message.

It listens to `human.approval.approved` and, for each item, either sends it or
says in a log line that it would have. Everything else in P3.6 exists to feed
this module or to stop it: the workers can only write, the approval gate is the
only writer of the decision log, and the decision log is the only thing this
module accepts as permission.

An item is refused unless all of these hold, in this order:

1. It is in the outbox — the copy `approve()` wrote at the moment of the
   decision. Anything not in the outbox was never queued by a human action.
2. `decisions.jsonl` records an *approved* entry for that exact path and that
   exact content hash. The Kafka event is a notification, not the authority: a
   message someone produced by hand without running the approval CLI finds no
   decision here and goes nowhere.
3. The body still hashes to what was approved. A file edited after a human read
   it is a different file.
4. The payment terms block is present and intact.
5. The draft's own prose contains no human-gated ask — no signup, no demo
   booking, no form. Checked again here because this is the boundary.
6. The channel's budget for today has room.

Only then does dry-run decide between a log line and a network call. The
default is dry-run, and the default is deliberately not `False`-with-a-warning:
`PUBLISHER_DRY_RUN` unset means nothing leaves this machine, so a deployment
that forgot to configure it is quiet rather than loud.

Backends are constructors, not imports at the call site, so a test can assert
the shape of a request without an internet connection, and so a missing API key
is a refusal here rather than an exception in a consumer loop.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.services import approval
from app.services.payment_facts import BLOCK_START, human_only_violations, payment_section

RESEND_URL = "https://api.resend.com/emails"
TWITTER_URL = "https://api.twitter.com/2/tweets"
LINKEDIN_URL = "https://api.linkedin.com/v2/ugcPosts"

# Channel -> rate-limit bucket. One bucket per channel, so the three numbers in
# .env mean what they say. A shared budget would be defensible on other grounds
# — a directory submission and a tweet are both "publishing" — but a limit an
# operator cannot find in the config file is a limit nobody honours.
CHANNEL_BUCKET = {
    "email": "outreach",
    "social": "marketing",
    "directory": "directory",
}

DEFAULT_LIMITS = {"outreach": 20, "marketing": 5, "directory": 10}

STATE_FILE = "publisher_state.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        # A typo in a limit must not silently become the default, and it must
        # not become "unlimited" either. Refuse the number loudly at import.
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    return max(0, value)


def configured_limits() -> Dict[str, int]:
    return {
        "outreach": _env_int("OUTREACH_DAILY_LIMIT", DEFAULT_LIMITS["outreach"]),
        "marketing": _env_int("MARKETING_DAILY_LIMIT", DEFAULT_LIMITS["marketing"]),
        "directory": _env_int("DIRECTORY_DAILY_LIMIT", DEFAULT_LIMITS["directory"]),
    }


def _truthy(raw: Optional[str], default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def dry_run_enabled() -> bool:
    """Unset means dry-run. Only an explicit false enables real sending."""
    return _truthy(os.environ.get("PUBLISHER_DRY_RUN"), True)


@dataclass
class Attempt:
    """One item's outcome. `sent` is true only when bytes left the machine."""

    item_id: str
    channel: str
    bucket: str
    recipient: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("dry_run", "sent")

    def to_line(self) -> str:
        target = self.recipient or "(broadcast)"
        if self.status == "dry_run":
            # The prefix says WOULD and nothing else. Built from the status
            # rather than passed in by the caller, so a log line cannot disagree
            # with the attempt it describes — an earlier version formatted the
            # dry-run line while the status was still its initial "refused" and
            # printed "REFUSED" for a rehearsal that had just succeeded.
            return f"WOULD {self.channel.upper()}  {self.item_id}  ->  {target}"
        if self.status == "sent":
            return f"SENT {self.channel.upper()}  {self.item_id}  ->  {target}"
        return f"REFUSED {self.channel.upper()}  {self.item_id}  {self.detail}"


def default_log() -> Callable[[str], None]:
    """A logger that prints the payload it is describing.

    The acceptance criterion for a dry run is that a reader can tell what would
    have been sent and to whom. A one-line "would send email" log would satisfy
    the words and fail the purpose, so the body is included in full.
    """

    def _log(line: str, *, body: str = "", recipient: str = "") -> None:
        print(line)
        if body:
            print("  " + "·" * 68)
            for text in body.splitlines():
                print(f"  | {text}")
            print("  " + "·" * 68)

    return _log


# --- backends -------------------------------------------------------------
#
# Each backend is one method, one endpoint, and no fallback. They are never
# constructed in dry-run, so the fact that these make real network calls is not
# reachable without both a real key and PUBLISHER_DRY_RUN=false.


@dataclass
class ResendEmailBackend:
    """Transactional email via Resend. `opener` is injectable for tests."""

    api_key: str
    sender: str
    opener: Optional[Callable] = None

    def send(self, recipient: str, subject: str, body: str) -> Dict[str, Any]:
        payload = {
            "from": self.sender,
            "to": [recipient],
            "subject": subject,
            "text": body,
        }
        request = urllib.request.Request(
            RESEND_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return json.loads(self._open(request))

    def _open(self, request) -> str:
        open_fn = self.opener or urllib.request.urlopen
        with open_fn(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")


@dataclass
class TwitterSocialBackend:
    """One post per item. Long threads are the author's job, not this loop's."""

    bearer_token: str
    opener: Optional[Callable] = None

    def send(self, recipient: str, subject: str, body: str) -> Dict[str, Any]:
        payload = {"text": body[:280]}
        request = urllib.request.Request(
            TWITTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return json.loads(self._open(request))

    def _open(self, request) -> str:
        open_fn = self.opener or urllib.request.urlopen
        with open_fn(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")


@dataclass
class LinkedInSocialBackend:
    author: str
    bearer_token: str
    opener: Optional[Callable] = None

    def send(self, recipient: str, subject: str, body: str) -> Dict[str, Any]:
        payload = {
            "author": self.author,
            "lifecycles": [{"decision": "PUBLISHED"}],
            "subject": subject,
            "text": {"type": "STRING", "text": body},
            "visibility": {"code": "PUBLIC"},
        }
        request = urllib.request.Request(
            LINKEDIN_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            method="POST",
        )
        return json.loads(self._open(request))

    def _open(self, request) -> str:
        open_fn = self.opener or urllib.request.urlopen
        with open_fn(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")


@dataclass
class DirectoryFormBackend:
    """A directory submission is a form nobody has filled in for us.

    Most registries want a browser and a captcha. Rather than pretend there is
    an API, a directory item produces a checklist of the fields to paste, so a
    dry-run log reads like the work order it describes.
    """

    def send(self, recipient: str, subject: str, body: str) -> Dict[str, Any]:
        raise NotImplementedError(
            "directory submissions have no API here: a human pastes the manifest "
            "into the listing form. Run scripts/publish_pending.py --list to read them."
        )


# --- the publisher --------------------------------------------------------


@dataclass
class Publisher:
    """Rate-limited, approval-verified dispatch of drafted items."""

    dry_run: bool = True
    limits: Dict[str, int] = field(default_factory=configured_limits)
    backends: Dict[str, Any] = field(default_factory=dict)
    log: Callable[..., None] = field(default_factory=default_log)
    clock: Callable[[], datetime] = _utc_now
    state_path: Optional[Path] = None
    _counts: Dict[str, Dict[str, int]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._load_state()

    # -- daily counters --

    def _state_file(self) -> Optional[Path]:
        if self.state_path is not None:
            return self.state_path
        if self.dry_run:
            # A dry run has no reason to persist anything, and writing a state
            # file it does not need would make the next real run's counts depend
            # on what a test happened to do today.
            return None
        return approval.workspace_root() / STATE_FILE

    def _load_state(self) -> None:
        path = self._state_file()
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        if isinstance(raw, dict):
            self._counts = {
                day: dict(counts)
                for day, counts in raw.items()
                if isinstance(counts, dict)
            }

    def _save_state(self) -> None:
        path = self._state_file()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._counts, indent=2) + "\n", encoding="utf-8")

    def _today(self) -> str:
        return self.clock().date().isoformat()

    def used(self, bucket: str) -> int:
        return self._counts.get(self._today(), {}).get(bucket, 0)

    def remaining(self, bucket: str) -> int:
        return max(0, self.limits.get(bucket, 0) - self.used(bucket))

    def _reserve(self, bucket: str) -> None:
        day = self._today()
        counts = self._counts.setdefault(day, {})
        counts[bucket] = counts.get(bucket, 0) + 1

    # -- the one item path --

    def publish_item(self, item: Dict[str, Any]) -> Attempt:
        """Verify, then send or log. Never raises for a policy refusal."""
        publish = item.get("publish") or {}
        channel = str(publish.get("channel") or "")
        recipient = str(publish.get("recipient") or "")
        bucket = CHANNEL_BUCKET.get(channel, "")
        attempt = Attempt(
            item_id=str(item.get("item_id", "?")),
            channel=channel or "none",
            bucket=bucket or "none",
            recipient=recipient,
            status="refused",
        )

        if not item.get("goal_id"):
            attempt.detail = "outbox item carries no goal_id, so no decision can be checked"
            return self._record(attempt)
        if channel not in CHANNEL_BUCKET:
            attempt.detail = f"unknown channel {channel!r}"
            return self._record(attempt)

        body = str(item.get("body") or "")

        # 2. The authority. Everything else here is a sanity check; this is the
        # permission, read from the file the human's own command wrote.
        if not approval.is_approved(
            str(item.get("workspace_path")), str(item.get("sha256")), str(item["goal_id"])
        ):
            attempt.detail = (
                "no approved entry in decisions.jsonl for this path and hash — the event "
                "says approved, the log does not"
            )
            return self._record(attempt)

        # 3. The bytes are still the ones that were approved.
        from app.services.tools import sha256_text

        if sha256_text(body) != item.get("sha256"):
            attempt.detail = "body changed after approval"
            return self._record(attempt)

        if not body.strip():
            attempt.detail = "empty body"
            return self._record(attempt)

        # 4 and 5. The two content rules of the lane, re-checked at the boundary.
        if BLOCK_START not in body or not payment_section(body).strip():
            attempt.detail = "no payment terms attached"
            return self._record(attempt)

        banned = human_only_violations(body)
        if banned:
            attempt.detail = f"human-gated ask: {', '.join(banned)}"
            return self._record(attempt)

        # An email and a submission both need an address; a post does not, it
        # goes to whichever account the token belongs to. Checked again here
        # because the worker's check is on the way in and this is the way out.
        if channel in ("email", "directory") and not recipient:
            attempt.detail = f"{channel} item has no recipient"
            return self._record(attempt)

        # 6. Budget.
        if self.remaining(bucket) <= 0:
            attempt.detail = (
                f"{bucket} daily limit of {self.limits.get(bucket)} reached "
                f"({self.used(bucket)} already)"
            )
            return self._record(attempt)

        subject = _subject(body)

        if self.dry_run:
            attempt.status = "dry_run"
            budget = f"[{bucket} {self.used(bucket) + 1}/{self.limits.get(bucket)}]"
            self.log(
                attempt.to_line()
                + f"  {budget}"
                + f"  sha256={str(item.get('sha256'))[:12]}"
                + f"  subject={subject!r}",
                body=body,
                recipient=recipient,
            )
            # Counted in dry-run too. A rehearsal that reports "0 of 20 used"
            # would mislead the operator about what a real run of this batch
            # would cost, and the point of the cap is to be visible.
            self._reserve(bucket)
            self._save_state()
            return attempt

        backend = self.backends.get(channel)
        if backend is None:
            attempt.detail = f"no backend configured for {channel} while dry-run is off"
            return self._record(attempt)

        try:
            result = backend.send(recipient, subject, body)
        except (urllib.error.URLError, NotImplementedError, OSError, ValueError) as exc:
            # A failed send is not an approved-and-done send: the item stays
            # unpublished so the next run can retry it.
            attempt.detail = f"backend failed: {exc}"
            return self._record(attempt)

        self._reserve(bucket)
        self._save_state()
        self.log(f"SENT {channel.upper()}  {attempt.item_id}  ->  {recipient}  {result}")
        attempt.status = "sent"
        _mark_published(item)
        return attempt

    def _record(self, attempt: Attempt) -> Attempt:
        self.log(attempt.to_line())
        return attempt

    # -- entry points --

    def publish_ids(self, item_ids: Sequence[str], goal_id: str = "") -> List[Attempt]:
        attempts = []
        for item_id in item_ids:
            item = approval.read_outbox(item_id)
            if item is None:
                attempts.append(
                    self._record(
                        Attempt(
                            item_id=item_id,
                            channel="none",
                            bucket="none",
                            recipient="",
                            status="refused",
                            detail="not in the outbox — approve() never queued it",
                        )
                    )
                )
                continue
            attempts.append(self.publish_item(item))
        return attempts

    def publish_goal(self, goal_id: str) -> List[Attempt]:
        """Everything an approved goal queued that has not gone out yet."""
        items = [
            item
            for item in approval.list_outbox()
            if item.get("goal_id") == goal_id and not item.get("published")
        ]
        if not items:
            return [
                self._record(
                    Attempt(
                        item_id=goal_id,
                        channel="none",
                        bucket="none",
                        recipient="",
                        status="refused",
                        detail="nothing pending in the outbox for this goal",
                    )
                )
            ]
        return [self.publish_item(item) for item in items]

    def publish_pending(self) -> List[Attempt]:
        return [self.publish_item(item) for item in approval.list_outbox() if not item.get("published")]


def _subject(body: str) -> str:
    """The first non-empty line, which is how these drafts are headed."""
    for line in (body or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.lower().startswith("subject:"):
            return text[len("subject:") :].strip()[:200]
        return text.lstrip("# ").strip()[:200]
    return "(no subject)"


def _mark_published(item: Dict[str, Any]) -> None:
    """Rewrite the outbox file with the fact that it went out.

    The item is the record of a decision, so the decision is never edited —
    only this flag, and only to say the approved thing has happened.
    """
    path = approval.outbox_path(str(item["item_id"]))
    if not path.is_file():
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["published"] = _utc_now().isoformat()
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_publisher(
    *, dry_run: Optional[bool] = None, state_path: Optional[Path] = None
) -> Publisher:
    """Wire the publisher from the environment.

    A backend exists only when dry-run is off *and* its key is real. That is the
    rule from the task, and building it here means no caller has to remember it
    and no channel can be half-configured into a live call. The placeholder keys
    in .env.example are refused by name, so copying that file — which is what
    everyone does — cannot produce a configured sender.
    """
    run_dry = dry_run_enabled() if dry_run is None else dry_run
    backends: Dict[str, Any] = {}
    if run_dry:
        return Publisher(dry_run=True, backends=backends, state_path=state_path)

    email_key = os.environ.get("EMAIL_API_KEY", "").strip()
    sender = os.environ.get("EMAIL_SENDER", "").strip()
    if _real(email_key) and sender:
        backends["email"] = ResendEmailBackend(api_key=email_key, sender=sender)
    social_token = os.environ.get("SOCIAL_BEARER_TOKEN", "").strip()
    if _real(social_token):
        which = os.environ.get("SOCIAL_BACKEND", "twitter").strip().lower()
        backends["social"] = (
            LinkedInSocialBackend(
                author=os.environ.get("LINKEDIN_AUTHOR", "").strip(),
                bearer_token=social_token,
            )
            if which == "linkedin"
            else TwitterSocialBackend(bearer_token=social_token)
        )
    # Directories have no API here. The backend exists so a directory item
    # fails with instructions instead of looking like it might have been sent.
    backends["directory"] = DirectoryFormBackend()
    return Publisher(dry_run=False, backends=backends, state_path=state_path)


def _real(key: str) -> bool:
    """Reject empty and reject the sample values, so a copied .env is dry."""
    return bool(key) and not key.lower().startswith(("dummy", "your-", "your_"))
