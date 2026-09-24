"""The publisher: the only process in this system that may talk to the world.

It is deliberately the *least* trusted consumer of the approval event. An event
that says "approved" is a notification on a bus any process can write to, so
this one re-reads the decision log and re-hashes the bytes before anything
leaves. Every refusal below is that check doing its job.

The dry-run default is the other half of the design: with no environment set at
all, this service describes what it would send and sends nothing, so standing it
up by accident cannot put a message in a stranger's inbox.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import pytest

import marketing_worker
import outreach_worker
import publisher_worker
from app.services import approval, payment_facts, publisher
from app.services.payment_facts import BLOCK_START
from app.services.publisher import (
    CHANNEL_BUCKET,
    Attempt,
    Publisher,
    build_publisher,
    configured_limits,
    dry_run_enabled,
)
from app.services.tools import Workspace, sha256_text
from events import Event
from llm import FakeLLM
from search import FakeSearchAPI
from worker_core import handle_tool_step

GOAL = "G-PUB"
SELLER = "0x3333333333333333333333333333333333333333"
BASE_URL = "https://api.importalpha.test"


class FakeHTTP:
    """An opener that records calls. A real one would need an OAuth client."""

    def __init__(self, response: bytes = b'{"id":"ok"}'):
        self.calls: List[tuple] = []
        self.response = response

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        outer = self

        class _Response:
            status = 200

            def read(self):
                return outer.response

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Response()


@pytest.fixture(autouse=True)
def offline_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("X402_NETWORK", "base-sepolia")
    monkeypatch.setenv("X402_WALLET_ADDRESS", SELLER)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setattr(approval, "REPO_ROOT", tmp_path / "repo")
    for name in (
        "PUBLISHER_DRY_RUN",
        "OUTREACH_DAILY_LIMIT",
        "MARKETING_DAILY_LIMIT",
        "DIRECTORY_DAILY_LIMIT",
        "EMAIL_API_KEY",
        "EMAIL_SENDER",
        "SOCIAL_BEARER_TOKEN",
        "SEARCH_PROVIDER",
        "MAX_TOOL_CALLS",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def draft(goal_id: str = GOAL, task: str = "outreach") -> List[dict]:
    """Run the drafting lane and return the artifact's file entries."""
    module = outreach_worker if task == "outreach" else marketing_worker
    title = (
        "Find 5 e-commerce agent builders and draft outreach into outreach/"
        if task == "outreach"
        else "Find 10 AI agent directories and draft manifests into marketing_out/"
    )
    event = Event(
        event_type=f"task.assigned.{task}",
        task_id=f"{goal_id}-S1",
        agent="router",
        payload={"title": title, "step_id": f"{goal_id}-S1", "goal_id": goal_id},
    )
    artifact = handle_tool_step(
        role=task,
        system_prompt=module.SYSTEM_PROMPT,
        artifact_type=module.ARTIFACT_TYPE,
        event=event,
        llm=FakeLLM(),
        search=FakeSearchAPI(),
        producer=lambda *a, **k: None,
        base_url=BASE_URL,
    )
    return artifact.payload["files"]


def approve(goal_id: str = GOAL, task: str = "outreach", paths: Optional[List[str]] = None) -> List[dict]:
    """Queue items exactly the way a human's decision does; return this goal's
    items. Filtered by goal, because the outbox is a shared directory and a
    test that reads `list_outbox()[0]` would silently re-publish another
    goal's draft and conclude the wrong thing about the channel routing."""
    files = draft(goal_id, task)
    approval.write_manifest(goal_id, "the goal", files)
    approval.approve(goal_id, paths=paths)
    return [item for item in approval.list_outbox() if item["goal_id"] == goal_id]


def queue(
    body: str,
    goal_id: str = GOAL,
    path: str = "outreach/letter.md",
    channel: str = "email",
    recipient: str = "team@cartwave.example.com",
) -> dict:
    """Approve a hand-written draft through the real gate.

    The content rules (payment terms, no human-gated asks) sit *after* the
    decision-log check in `publish_item`, so a test for them needs bytes the
    log has genuinely seen. Writing the file and calling approve() is the only
    honest way to get there; it is what a human would have done with a draft
    that came from somewhere else.
    """
    workspace = Workspace(goal_id).ensure()
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    digest = sha256_text(body)
    approval.write_manifest(
        goal_id,
        "hand-written",
        [
            {
                "workspace_path": path,
                "target_path": None,
                "kind": "publish",
                "summary": "hand-written draft",
                "sha256": digest,
                "bytes": len(body.encode("utf-8")),
                "publish": {"channel": channel, "recipient": recipient, "note": ""},
            }
        ],
    )
    approval.approve(goal_id)
    # The item just queued, found by path *and* hash: the outbox accumulates
    # over a test and several of these drafts share a path, so "this goal's
    # first item" is usually some earlier draft's — and a test that reads it
    # reports the refusal reason for the wrong body.
    for item in approval.list_outbox():
        if item["workspace_path"] == path and item["sha256"] == digest:
            return item
    raise AssertionError(f"nothing queued at {path}")


def sender(tmp_path, name="state.json", backends=None, **kwargs) -> tuple[Dict[str, list], Publisher]:
    """A live publisher whose state file and log stay inside tmp_path.

    No state path defaults to a bare filename: that is a relative path, and a
    test suite that writes into the repository root leaves today's counters
    wired into tomorrow's run.
    """
    records: Dict[str, list] = {"lines": [], "bodies": [], "recipients": []}

    def log(line: str, *, body: str = "", recipient: str = "") -> None:
        records["lines"].append(line)
        records["bodies"].append(body)
        records["recipients"].append(recipient)

    pub = Publisher(
        dry_run=False,
        backends=backends or {},
        state_path=tmp_path / name,
        log=log,
        **kwargs,
    )
    return records, pub


def letter(text: str = "Landed cost, per call.") -> str:
    return (
        f"{text}\n\n"
        + payment_facts.payment_block(BASE_URL)
    )


def collector() -> tuple[Dict[str, list], Publisher]:
    """A publisher whose log keeps everything it was handed, arguments included.

    `log` is called with the line plus keyword `body=`/`recipient=`, so a
    plain list of strings would throw away the payload — the one part the
    acceptance criterion is about.
    """
    records: Dict[str, list] = {"lines": [], "bodies": [], "recipients": []}

    def log(line: str, *, body: str = "", recipient: str = "") -> None:
        records["lines"].append(line)
        records["bodies"].append(body)
        records["recipients"].append(recipient)

    return records, Publisher(dry_run=True, log=log)


def dispatches(lines: List[str]) -> List[str]:
    """Only the lines that describe something leaving the machine."""
    return [line for line in lines if line.startswith(("WOULD", "SENT"))]


class FakeBackend:
    """Same call signature as the real backends: (recipient, subject, body)."""

    channel = "email"

    def __init__(self):
        self.sent: List[tuple] = []

    def send(self, recipient: str, subject: str, body: str) -> dict:
        self.sent.append((recipient, subject, body))
        return {"id": "fake"}


# ---------- the default ----------


def test_with_nothing_configured_the_publisher_is_in_dry_run():
    """The shipped default. A compose file that forgets the flag must not send."""
    assert dry_run_enabled() is True
    assert Publisher().dry_run is True


def test_dry_run_reads_the_environment_and_anything_explicit_wins(monkeypatch):
    assert dry_run_enabled() is True
    monkeypatch.setenv("PUBLISHER_DRY_RUN", "false")
    assert dry_run_enabled() is False
    monkeypatch.setenv("PUBLISHER_DRY_RUN", "0")
    assert dry_run_enabled() is False
    assert Publisher(dry_run=True).dry_run is True


def test_the_documented_limits_are_the_code_defaults():
    assert configured_limits() == {"outreach": 20, "marketing": 5, "directory": 10}
    assert CHANNEL_BUCKET == {"email": "outreach", "social": "marketing", "directory": "directory"}


def test_a_misconfigured_limit_raises_instead_of_becoming_unlimited(monkeypatch):
    monkeypatch.setenv("OUTREACH_DAILY_LIMIT", "twenty")
    with pytest.raises(ValueError, match="OUTREACH_DAILY_LIMIT"):
        configured_limits()


# ---------- what dry-run does ----------


def test_dry_run_logs_the_target_and_the_whole_payload_and_calls_nothing():
    """The point of the phase's acceptance run: a human reads the exact text
    that would have gone out, to the exact address it would have gone to."""
    records, pub = collector()
    item = approve()[0]
    attempt = pub.publish_item(item)

    assert attempt.status == "dry_run"
    assert pub.backends == {}, "dry run must not hold a sender at all"
    assert len(records["lines"]) == 1
    assert records["lines"][0].startswith(f"WOULD EMAIL  {item['item_id']}")
    assert records["recipients"] == [attempt.recipient] == [item["publish"]["recipient"]]
    assert records["bodies"] == [item["body"]], "the log carries the payload, not a summary"
    assert BLOCK_START in records["bodies"][0]


def test_dry_run_still_charges_the_budget():
    """The limits are the reason for the queue. A dry run that ignored them
    would approve a batch on Monday that could not actually be sent."""
    records, pub = collector()
    items = approve(paths=[f["workspace_path"] for f in draft()][:3])
    assert [pub.publish_item(i).status for i in items] == ["dry_run"] * 3
    assert pub.used("outreach") == 3
    assert pub.remaining("outreach") == 17


def test_a_batch_larger_than_the_budget_is_cut_not_dropped(monkeypatch):
    """The fifth item in a five-per-day lane must fail with a reason, not
    silently vanish from the queue."""
    monkeypatch.setenv("OUTREACH_DAILY_LIMIT", "4")
    records, pub = collector()
    items = approve()
    attempts = [pub.publish_item(i) for i in items[:4]]
    assert [a.status for a in attempts] == ["dry_run"] * 4

    fifth = pub.publish_item(items[0])
    assert fifth.status == "refused"
    assert "outreach daily limit of 4 reached" in fifth.detail
    assert pub.used("outreach") == 4


def test_the_channels_have_separate_budgets():
    """20 letters a day must not be paid for by 5 posts a day. The task names
    three limits and one shared pool would make two of them meaningless."""
    records: List[dict] = []
    pub = Publisher(
        limits={"outreach": 1, "marketing": 1, "directory": 1},
        log=lambda line, **kw: records.append(line),
    )
    letter = approve(goal_id="G-MIX1")[0]
    listing = approve(goal_id="G-MIX2", task="marketing")[0]
    assert pub.publish_item(letter).status == "dry_run"
    assert pub.publish_item(listing).status == "dry_run"
    assert pub.publish_item(letter).status == "refused"
    assert pub.publish_item(listing).status == "refused"


def test_the_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("OUTREACH_DAILY_LIMIT", "2")
    monkeypatch.setenv("MARKETING_DAILY_LIMIT", "1")
    monkeypatch.setenv("DIRECTORY_DAILY_LIMIT", "3")
    assert configured_limits() == {"outreach": 2, "marketing": 1, "directory": 3}

    records: List[dict] = []
    pub = Publisher(log=lambda line, **kw: records.append(line))
    items = approve(paths=[f["workspace_path"] for f in draft()][:4])
    statuses = [pub.publish_item(i).status for i in items]
    assert statuses == ["dry_run", "dry_run", "refused", "refused"]
    assert "limit" in pub.publish_item(items[0]).detail


def test_an_approved_directory_draft_is_logged_as_a_submission_not_an_email():
    records, pub = collector()
    items = approve(task="marketing")[:3]
    attempts = [pub.publish_item(item) for item in items]

    assert [a.channel for a in attempts] == ["directory"] * 3
    assert all(a.status == "dry_run" for a in attempts), [a.detail for a in attempts]
    assert all(a.recipient.startswith("https://") for a in attempts)
    assert all(line.startswith("WOULD DIRECTORY") for line in records["lines"])
    assert pub.used("directory") == 3 and pub.used("outreach") == 0


# ---------- the gate is checked, not trusted ----------


def rewritten_body(item: dict, text: str) -> dict:
    """A tampered item whose hash follows the text, so the only defence left
    is the decision log. Hash mismatch and log mismatch are different attacks
    and both have to fail."""
    copy = json.loads(json.dumps(item))
    copy["body"] = text
    copy["sha256"] = sha256_text(text)
    copy["bytes"] = len(text.encode("utf-8"))
    return copy


def test_an_item_with_no_decision_log_entry_is_refused():
    """CRITICAL: forge an outbox file by hand and the publisher still will not
    send it. The approval event grants nothing; the log is the authority.

    The forged item carries a hash the log has never seen, which is the point:
    the check that saves it is a lookup of exactly those bytes, so no wording
    can pass it without a matching approve() line.
    """
    records, pub = collector()
    forged = rewritten_body(approve()[0], "Dear CEO.\n\n" + BLOCK_START + "\npay to nowhere\n")
    forged.update(
        item_id="G-FORGED::outreach/draft-1.md::" + forged["sha256"][:16],
        goal_id="G-FORGED",
        workspace_path="outreach/does-not-exist.md",
        publish={"channel": "email", "recipient": "ceo@target.example.org", "note": ""},
    )

    attempt = pub.publish_item(forged)
    assert attempt.status == "refused"
    assert "decisions.jsonl" in attempt.detail
    assert records["lines"] == [f"REFUSED EMAIL  {forged['item_id']}  {attempt.detail}"]
    assert pub.used("outreach") == 0


def test_bytes_nobody_approved_are_refused_even_under_an_approved_path():
    """Same path, different text. This is the attack a path-only check would
    let through: a worker appending a line to an approved draft."""
    records, pub = collector()
    item = rewritten_body(approve()[0], "Dear CEO. Our price is free today.\n\n" + BLOCK_START + "\nx\n")
    attempt = pub.publish_item(item)

    assert attempt.status == "refused"
    assert "decisions.jsonl" in attempt.detail
    assert "free today" not in "\n".join(records["bodies"])


def test_a_real_backend_and_a_forged_item_still_sends_nothing(tmp_path):
    """The same attack with a working sender installed and dry-run off: proof
    that the refusal is the gate check and not the dry-run flag."""
    records, pub = sender(tmp_path, backends={"email": FakeBackend()})
    forged = rewritten_body(approve()[0], "Please wire 5000 USDC to 0xdeadbeef.\n\n" + BLOCK_START + "\nx\n")
    forged.update(goal_id="G-FORGED", workspace_path="outreach/nope.md")

    forged_attempt = pub.publish_item(forged)
    assert forged_attempt.status == "refused"
    assert pub.backends["email"].sent == []
    assert records["lines"] == [f"REFUSED EMAIL  {forged['item_id']}  " + forged_attempt.detail]
    assert dispatches(records["lines"]) == []


def test_bytes_that_changed_after_approval_are_refused():
    """The log has the old hash. This says "the event was real, the text was
    edited afterwards", which is the failure mode hashing exists to catch."""
    records, pub = collector()
    item = json.loads(json.dumps(approve()[0]))
    item["body"] = item["body"] + "\nP.S. actually it is free.\n"
    attempt = pub.publish_item(item)

    assert attempt.status == "refused"
    assert attempt.detail == "body changed after approval"


def test_a_rejected_draft_is_not_sendable():
    """Reject 3 of 5 and the three must not be in the outbox at all. If a
    rejection left a queued item behind, approve/reject would be a suggestion."""
    files = draft()
    paths = [f["workspace_path"] for f in files]
    approval.write_manifest(GOAL, "the goal", files)
    approval.approve(GOAL, paths=paths[:2])
    approval.load_manifest(GOAL)  # the batch is closed; the rest were rejected
    queued = {item["workspace_path"] for item in approval.list_outbox()}
    assert queued == set(paths[:2])

    records, pub = collector()
    attempts = pub.publish_pending()
    assert len(attempts) == 2
    assert all(a.status == "dry_run" for a in attempts)


def test_a_human_only_ask_is_refused_at_the_door_too():
    """The drafting lane refuses to write these. This is the second wall, for
    bytes that did not come from the drafting lane — approved by a human who
    missed the line.

    The draft is queued through approve() rather than forged, because the
    decision-log check runs before the content rules: a tampered body is
    refused as a tampered body, and this rule would never be exercised.
    """
    item = queue("Book a demo with our sales team here.\n\n" + payment_facts.payment_block(BASE_URL))
    records, pub = collector()
    attempt = pub.publish_item(item)

    assert attempt.status == "refused"
    assert "human-gated ask" in attempt.detail
    # A refusal is logged, because an empty queue that stays silent looks like a
    # bug; but no dispatch line (WOULD/SENT) is ever printed for it.
    assert dispatches(records["lines"]) == []


def test_a_draft_without_payment_terms_is_refused():
    """A letter an agent cannot pay for is not a pitch, it is a brochure."""
    item = queue("Subject: landed cost\n\nWe answer landed cost. Say the word.")
    records, pub = collector()
    attempt = pub.publish_item(item)

    assert attempt.status == "refused"
    assert attempt.detail == "no payment terms attached"
    assert dispatches(records["lines"]) == []


def test_the_shape_checks_refuse_before_the_budget_is_touched():
    """An item with no goal, no channel or no body is not a permission problem
    and must not be logged as a dispatch, let alone spend a slot."""
    records, pub = collector()

    # Channel is checked before the body, so an unchannelled item is refused as
    # a shape error even when its text would pass every content rule.
    blank = pub.publish_item({"body": "   ", "goal_id": GOAL})
    assert (blank.status, blank.detail) == ("refused", "unknown channel ''")

    no_goal = pub.publish_item({"body": "x", "goal_id": ""})
    assert (no_goal.status, "goal_id" in no_goal.detail) == ("refused", True)

    pigeon = pub.publish_item({"body": "x", "goal_id": GOAL, "publish": {"channel": "carrier_pigeon"}})
    assert (pigeon.status, "unknown channel" in pigeon.detail) == ("refused", True)

    empty = queue("   ", path="outreach/blank.md")
    empty_attempt = pub.publish_item(empty)
    assert (empty_attempt.status, empty_attempt.detail) == ("refused", "empty body")

    no_address = queue(letter(), path="outreach/x.md", recipient="")
    assert pub.publish_item(no_address).detail == "email item has no recipient"

    assert pub.used("outreach") == 0, "a refusal must not spend the budget"
    assert dispatches(records["lines"]) == []


def test_publish_ids_refuses_an_id_the_outbox_never_held():
    records, pub = collector()
    attempts = pub.publish_ids(["G-NOPE::outreach/draft-9.md::0000000000000000"], goal_id=GOAL)
    assert attempts[0].status == "refused"
    assert "not in the outbox" in attempts[0].detail


def test_publish_goal_reads_the_outbox_not_the_event():
    """The event names a goal; the outbox says what that goal queued. An
    approval notification carrying a body would be ignored by construction."""
    records, pub = collector()
    items = approve()
    attempts = pub.publish_goal(GOAL)
    assert len(attempts) == len(items) == 5
    assert all(a.status == "dry_run" for a in attempts), [a.detail for a in attempts]

    assert pub.publish_goal("G-EMPTY")[0].detail == "nothing pending in the outbox for this goal"


def test_dry_run_keeps_items_replayable():
    """A dry run is not a send. Once real keys exist the same item must still
    be queued, so the published flag has to stay false."""
    records, pub = collector()
    item = approve()[0]
    pub.publish_item(item)

    assert approval.read_outbox(item["item_id"])["published"] is False
    assert pub.publish_pending()[0].status == "dry_run", "the queue must still hold it"


def test_dry_run_writes_no_state_file(monkeypatch, tmp_path):
    """A rehearsal that persists counters would let today's test runs change
    what a real run is allowed to send."""
    records, pub = collector()
    for item in approve():
        pub.publish_item(item)
    assert pub._state_file() is None
    assert list((approval.workspace_root()).glob("publisher_state.json")) == []
    assert list(tmp_path.rglob("publisher_state.json")) == []


def test_a_real_send_never_opens_a_socket_in_dry_run(monkeypatch):
    """Independent of every other check: with the network wired to a tripwire,
    a whole approved batch still produces five WOULD lines and zero calls."""
    import urllib.request

    calls: List[str] = []

    def tripwire(request, *args, **kwargs):
        url = getattr(request, "full_url", str(request))
        calls.append(url)
        raise AssertionError(f"a dry run tried to reach {url}")

    monkeypatch.setattr(urllib.request, "urlopen", tripwire)
    records, pub = collector()
    approve()
    attempts = pub.publish_goal(GOAL)
    assert len(attempts) == 5 and all(a.status == "dry_run" for a in attempts)
    assert calls == []
    assert dispatches(records["lines"]) == records["lines"] and len(records["lines"]) == 5


# ---------- a real send, with the endpoint replaced by a recorder ----------


def test_a_real_run_sends_the_approved_bytes_to_the_approved_address(tmp_path):
    http = FakeHTTP()
    records, pub = sender(
        tmp_path, backends={"email": publisher.ResendEmailBackend("re-real-key", "team@importalpha.test", opener=http)}
    )
    item = queue(letter())

    attempt = pub.publish_item(item)
    assert attempt.status == "sent", attempt.detail
    assert attempt.ok
    assert len(http.calls) == 1

    request = http.calls[0][0]
    assert request.full_url == publisher.RESEND_URL
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer re-real-key"
    sent = json.loads(request.data.decode())
    assert sent["from"] == "team@importalpha.test"
    assert sent["to"] == ["team@cartwave.example.com"]
    assert sent["text"] == item["body"], "send the approved text, unchanged"


def test_a_sent_item_leaves_the_queue(tmp_path):
    """Otherwise every later run retries a message that already reached the
    recipient, which is how a lane becomes spam."""
    http = FakeHTTP()
    records, pub = sender(
        tmp_path, backends={"email": publisher.ResendEmailBackend("k", "s@x.test", opener=http)}
    )
    item = queue(letter())
    assert pub.publish_item(item).status == "sent"

    assert approval.read_outbox(item["item_id"])["published"] is not False
    assert pub.publish_pending() == [], "the queue is empty now"
    assert len(http.calls) == 1


def test_a_failed_send_stays_queued_and_is_retried(tmp_path):
    """A backend error must not mark the item sent. It also does not spend a
    slot, because the recipient never saw anything; the cap that matters here
    is the one on messages delivered."""

    class Broken:
        def send(self, recipient, subject, body):
            raise OSError("429 too many requests")

    records, pub = sender(tmp_path, backends={"email": Broken()})
    item = queue(letter())
    attempt = pub.publish_item(item)

    assert attempt.status == "refused"
    assert "429" in attempt.detail
    assert approval.read_outbox(item["item_id"])["published"] is False
    assert pub.used("outreach") == 0, "a send that never left did not use the day's allowance"

    http = FakeHTTP()
    _more, retry = sender(tmp_path, backends={"email": publisher.ResendEmailBackend("k", "s@x.test", opener=http)})
    assert retry.publish_item(item).status == "sent", "a failed send is retried, not dropped"
    assert len(http.calls) == 1


def test_dry_run_off_with_no_backend_refuses_instead_of_looking_sent(tmp_path):
    records, pub = sender(tmp_path)
    attempt = pub.publish_item(queue(letter()))
    assert attempt.status == "refused"
    assert "no backend configured" in attempt.detail


def test_a_second_process_shares_the_daily_count_through_the_state_file(tmp_path):
    """A per-process counter would let two runs send forty letters on a day
    capped at twenty."""
    state = tmp_path / "shared.json"
    http = FakeHTTP()
    first = Publisher(
        dry_run=False,
        limits={"outreach": 1, "marketing": 1, "directory": 1},
        backends={"email": publisher.ResendEmailBackend("k", "s@x.test", opener=http)},
        state_path=state,
        log=lambda line, **kw: None,
    )
    assert first.publish_item(queue(letter())).status == "sent"

    reloaded = Publisher(
        dry_run=False,
        limits={"outreach": 1, "marketing": 1, "directory": 1},
        backends={"email": publisher.ResendEmailBackend("k", "s@x.test", opener=http)},
        state_path=state,
        log=lambda line, **kw: None,
    )
    assert reloaded.used("outreach") == 1
    assert reloaded.remaining("outreach") == 0
    assert len(http.calls) == 1


def test_the_state_file_rolls_over_on_a_new_day(tmp_path):
    from datetime import datetime, timedelta, timezone

    day_one = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    clock = {"now": day_one}
    state = tmp_path / "roll.json"
    _records, pub = sender(tmp_path, name="roll.json", clock=lambda: clock["now"],
                           limits={"outreach": 1, "marketing": 1, "directory": 1},
                           backends={"email": publisher.ResendEmailBackend(
                               "k", "s@x.test", opener=FakeHTTP())})
    assert pub.publish_item(queue(letter())).status == "sent"
    assert pub.used("outreach") == 1
    assert state.read_text().count("2026-03-01") == 1, "the counter is filed under its day"

    clock["now"] = day_one + timedelta(days=1)
    tomorrow = Publisher(
        dry_run=False,
        limits={"outreach": 1, "marketing": 1, "directory": 1},
        backends={"email": publisher.ResendEmailBackend("k", "s@x.test", opener=FakeHTTP())},
        state_path=state,
        clock=lambda: clock["now"],
        log=lambda line, **kw: None,
    )
    assert tomorrow.used("outreach") == 0, "yesterday's send is not today's budget"
    assert tomorrow.remaining("outreach") == 1


# ---------- build_publisher: the rule that keys and the flag both have to agree ----------


def test_the_placeholder_keys_in_env_example_cannot_build_a_sender(monkeypatch):
    """Copying .env.example is what everyone does. That must not be enough to
    put a live credential path into the publisher."""
    monkeypatch.delenv("PUBLISHER_DRY_RUN", raising=False)
    for key in ("EMAIL_API_KEY", "EMAIL_SENDER", "SOCIAL_BEARER_TOKEN"):
        monkeypatch.setenv(key, "dummy_key_for_dry_run")
    pub = build_publisher()
    assert pub.dry_run is True
    assert pub.backends == {}


def test_real_keys_with_dry_run_still_on_build_no_backends(monkeypatch):
    monkeypatch.setenv("PUBLISHER_DRY_RUN", "true")
    monkeypatch.setenv("EMAIL_API_KEY", "re-abcdef123456")
    monkeypatch.setenv("EMAIL_SENDER", "team@importalpha.test")
    assert build_publisher().backends == {}


def test_real_keys_and_dry_run_off_build_the_email_backend(monkeypatch):
    monkeypatch.setenv("PUBLISHER_DRY_RUN", "false")
    monkeypatch.setenv("EMAIL_API_KEY", "re-abcdef123456")
    monkeypatch.setenv("EMAIL_SENDER", "team@importalpha.test")
    pub = build_publisher()
    assert pub.dry_run is False
    assert isinstance(pub.backends["email"], publisher.ResendEmailBackend)
    # Directories have no API: the backend exists so a submission is refused
    # with instructions rather than looking like it might have gone out.
    assert isinstance(pub.backends["directory"], publisher.DirectoryFormBackend)


def test_a_social_token_picks_the_backend_the_environment_names(monkeypatch):
    monkeypatch.setenv("PUBLISHER_DRY_RUN", "false")
    monkeypatch.setenv("SOCIAL_BEARER_TOKEN", "real-token-123")
    assert isinstance(build_publisher().backends["social"], publisher.TwitterSocialBackend)
    monkeypatch.setenv("SOCIAL_BACKEND", "linkedin")
    monkeypatch.setenv("LINKEDIN_AUTHOR", "urn:li:person:abc")
    linked = build_publisher().backends["social"]
    assert isinstance(linked, publisher.LinkedInSocialBackend)
    assert linked.author == "urn:li:person:abc"


def test_a_key_that_looks_like_a_template_is_not_real(monkeypatch):
    for placeholder in ("", "your_key_here", "YOUR_API_KEY", "dummy", "your-token"):
        monkeypatch.setenv("EMAIL_API_KEY", placeholder)
        monkeypatch.setenv("EMAIL_SENDER", "team@importalpha.test")
        pub = build_publisher(dry_run=False)
        assert "email" not in pub.backends, placeholder


def test_a_directory_submission_is_refused_with_instructions_not_silently_kept(tmp_path):
    """No directory has an API here. Pretending otherwise would turn a dry-run
    log into a false claim that listings were submitted."""
    records: List[dict] = []
    pub = Publisher(
        dry_run=False,
        backends={"directory": publisher.DirectoryFormBackend()},
        state_path=tmp_path / "s.json",
        log=lambda line, **kw: records.append(line),
    )
    items = approve(task="marketing")
    attempt = pub.publish_item(items[0])
    assert attempt.status == "refused"
    assert "no API" in attempt.detail
    assert attempt.ok is False
