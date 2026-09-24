"""The drafting lane: research with two read-only tools, write drafts, stop.

Two things are being pinned here and they are not the same thing.

The first is the productive contract — a goal that names five prospects produces
five different letters, each aimed at a route that a lookup actually returned,
each carrying the payment terms verbatim.

The second is the safety contract, and it is the reason the publisher exists
separately: `handle_tool_step` has no way to send anything. A model that asks
for a send tool is told it does not exist, and a model that writes a signup ask
into a draft has the write refused. Those refusals are asserted here because
"the prompt says not to" is not a control.
"""

from __future__ import annotations

import json
from typing import List, Optional

import pytest

import marketing_worker
import outreach_worker
import worker_core
from app.services import approval
from app.services.payment_facts import BLOCK_START, human_only_violations
from app.services.tools import Workspace, sha256_text
from events import Event
from llm import FakeLLM, LLM
from search import FakeSearchAPI, Page, SearchResult
from worker_core import (
    ALLOWED_TOOLS,
    _publish_target,
    handle_tool_step,
    output_path_for,
    parse_tool_action,
)

GOAL = "G-DRAFT"
SELLER = "0x2222222222222222222222222222222222222222"
BASE_URL = "https://api.importalpha.test"

OUTREACH_TASK = (
    "Find 5 e-commerce agent builders and draft personalised outreach into outreach/"
)
MARKETING_TASK = (
    "Find 10 AI agent directories or registries and draft submission manifests "
    "into marketing_out/"
)


@pytest.fixture(autouse=True)
def offline_workspace(tmp_path, monkeypatch):
    """A workspace, a payment configuration, and no ambient .env.

    The harness injects .env into the process, so a test that wants a code
    default has to clear the variable it depends on.
    """
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("X402_NETWORK", "base-sepolia")
    monkeypatch.setenv("X402_WALLET_ADDRESS", SELLER)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setattr(approval, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("MAX_TOOL_CALLS", raising=False)
    return tmp_path


def assigned(title: str, role: str = "outreach", goal_id: Optional[str] = GOAL) -> Event:
    payload = {"title": title, "step_id": f"{goal_id}-S1"}
    if goal_id is not None:
        payload["goal_id"] = goal_id
    return Event(event_type=f"task.assigned.{role}", task_id=f"{goal_id}-S1", agent="router", payload=payload)


class ScriptLLM(LLM):
    """Exact replies, so a refusal is attributable to the code and not to a model."""

    def __init__(self, replies: List[object]):
        self.replies = [
            reply if isinstance(reply, str) else json.dumps(reply) for reply in replies
        ]

    def complete(self, system: str, user: str) -> str:
        if not self.replies:
            return json.dumps({"done": "script exhausted"})
        return self.replies.pop(0)


class SpySearch(FakeSearchAPI):
    """Records every method the loop reaches for."""

    def __init__(self):
        super().__init__()
        self.touched: List[str] = []

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        self.touched.append("search")
        return super().search(query, max_results=max_results)

    def read_url(self, url: str) -> Page:
        self.touched.append("read_url")
        return super().read_url(url)


def run_step(title=OUTREACH_TASK, role="outreach", llm=None, search=None, **kwargs):
    """One step, offline, with the artifact and everything it emitted."""
    emitted = []
    module = outreach_worker if role == "outreach" else marketing_worker
    artifact = handle_tool_step(
        role=role,
        system_prompt=module.SYSTEM_PROMPT,
        artifact_type=module.ARTIFACT_TYPE,
        event=assigned(title, role),
        llm=llm if llm is not None else FakeLLM(),
        search=search if search is not None else FakeSearchAPI(),
        producer=lambda topic, event, key=None: emitted.append((topic, event)),
        base_url=BASE_URL,
        **kwargs,
    )
    return artifact, emitted


def draft_files(artifact: Event) -> List[dict]:
    return artifact.payload["files"]


def body(entry: dict) -> str:
    return Workspace(GOAL).read_file(entry["workspace_path"])


# ---------- the productive contract ----------


def test_outreach_drafts_one_letter_per_prospect():
    artifact, _ = run_step()

    files = draft_files(artifact)
    assert len(files) == 5, artifact.payload["summary"]
    assert len({f["sha256"] for f in files}) == 5, "five drafts, one text"
    assert len({f["publish"]["recipient"] for f in files}) == 5
    assert {f["publish"]["channel"] for f in files} == {"email"}


def test_each_letter_is_aimed_at_a_contact_route_a_lookup_returned():
    """A recipient the corpus never published would be a model's guess, and a
    guessed address is either a bounce or a stranger's inbox."""
    known = {
        page_contact.url: page_contact.contact
        for page_contact in [
            FakeSearchAPI().read_url(row.url)
            for row in FakeSearchAPI().search("e-commerce agent builders", max_results=20)
        ]
    }
    artifact, _ = run_step()
    for entry in draft_files(artifact):
        recipient = entry["publish"]["recipient"]
        assert recipient in known.values() or recipient in known, recipient


def test_marketing_drafts_ten_manifests_aimed_at_listings():
    artifact, _ = run_step(title=MARKETING_TASK, role="marketing")

    files = draft_files(artifact)
    assert len(files) == 10, artifact.payload["summary"]
    assert {f["publish"]["channel"] for f in files} == {"directory"}
    assert len({f["publish"]["recipient"] for f in files}) == 10
    assert all(f["publish"]["recipient"].startswith("https://") for f in files)


def test_every_draft_states_the_payment_terms():
    """The x402 instructions are the payload of the lane: a letter without them
    asks a stranger to go find a website."""
    artifact, _ = run_step()
    for entry in draft_files(artifact):
        text = body(entry)
        assert BLOCK_START in text
        assert SELLER in text
        assert "USDC" in text
        assert "base-sepolia" in text


def test_no_draft_asks_a_human_for_anything():
    """The product is bought by an agent over HTTP. A signup link in an
    outreach letter is the failure mode the discovery layer exists to prevent."""
    for title, role in ((OUTREACH_TASK, "outreach"), (MARKETING_TASK, "marketing")):
        artifact, _ = run_step(title=title, role=role)
        for entry in draft_files(artifact):
            assert human_only_violations(body(entry)) == []


def test_the_hash_binds_the_text_that_will_be_sent():
    """The payment block goes in before hashing. Appending it at send time
    would mean a human approved one text and something else left the machine."""
    artifact, _ = run_step()
    for entry in draft_files(artifact):
        text = body(entry)
        assert entry["sha256"] == sha256_text(text)
        assert BLOCK_START in text


def test_the_step_emits_one_artifact_with_the_files_the_gate_needs():
    artifact, emitted = run_step()

    assert [topic for topic, _ in emitted] == ["artifact.created"]
    assert emitted[0][1] is artifact
    payload = artifact.payload
    assert payload["artifact_type"] == "drafts"
    assert payload["goal_id"] == GOAL
    assert payload["status"] == "draft"
    # `artifact_type` plus `files` is what makes the orchestrator treat this as
    # a deliverable and open an approval request; a rename on either side turns
    # the lane into a worker that silently does nothing.
    assert payload["files"]
    assert all({"workspace_path", "sha256", "publish", "kind"} <= set(f) for f in payload["files"])
    assert payload["datapoints"] == []


def test_the_lane_writes_nothing_outside_the_goal_workspace():
    artifact, _ = run_step()
    root = Workspace(GOAL).ensure()
    for entry in draft_files(artifact):
        written = (root / entry["workspace_path"]).resolve()
        assert written.is_file()
        assert str(written).startswith(str(root.resolve()))
        assert entry["target_path"] is None, "a draft is not a repository change"


def test_drafts_land_in_the_directory_the_task_named():
    artifact, _ = run_step(title="Draft prospect letters into emails/")
    assert {f["workspace_path"].split("/")[0] for f in draft_files(artifact)} == {"emails"}


def test_the_loop_only_reaches_for_the_two_lookups():
    spy = SpySearch()
    run_step(search=spy)
    assert set(spy.touched) == {"search", "read_url"}


def test_tool_budget_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("MAX_TOOL_CALLS", "7")
    import importlib

    reloaded = importlib.reload(worker_core)
    assert reloaded.MAX_TOOL_CALLS == 7


# ---------- the refusals ----------


def test_a_model_asking_for_a_send_tool_is_told_it_does_not_exist():
    """The CRITICAL property, first instance: there is no tool to send with."""
    llm = ScriptLLM(
        [
            {"call": {"tool": "send_email", "args": {"to": "anyone@example.org", "body": "hi"}}},
            {"call": {"tool": "smtp_relay", "args": {}}},
            {"done": "gave up"},
        ]
    )
    artifact, _ = run_step(llm=llm)

    assert artifact.payload["files"] == []
    assert set(artifact.payload["refused_tools"]) == {"send_email", "smtp_relay"}
    assert artifact.payload["status"] == "failed", "no draft, so nothing to approve"


def test_no_module_on_the_drafting_path_exposes_a_send_thing():
    """Second instance of the same property, read off the code rather than
    off a model's behaviour: the worker modules import no transport, no SMTP
    client and no publisher, so there is nothing to call."""
    import inspect
    import pathlib

    forbidden = (
        "smtplib",
        "smtp",
        "sendmail",
        "urllib.request",
        "http.client",
        "requests",
        "httpx",
        "publisher",
        "resend",
        "tweepy",
        "socket",
    )
    for filename in ("worker_core.py", "outreach_worker.py", "marketing_worker.py"):
        source = (pathlib.Path(__file__).resolve().parents[1] / filename).read_text()
        tree = __import__("ast").parse(source)
        imported = set()
        for node in __import__("ast").walk(tree):
            if isinstance(node, __import__("ast").Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, __import__("ast").ImportFrom):
                imported.add(node.module or "")
        for name in forbidden:
            assert name not in imported, f"{filename} imports {name}"
        # `inspect` again as a sanity check that the module has no send method.
        assert "def send" not in source, filename
        assert "async def send" not in source, filename
    assert inspect.isfunction(handle_tool_step)


def test_the_only_tools_a_worker_may_name_are_the_two_lookups():
    assert ALLOWED_TOOLS == ("search_web", "read_url")
    assert not any("send" in tool or "post" in tool for tool in ALLOWED_TOOLS)


def test_a_draft_asking_for_a_signup_is_refused_before_it_is_written():
    llm = ScriptLLM(
        [
            {
                "write": {
                    "content": "Hi there, please sign up at our portal to get an API key.",
                    "channel": "email",
                    "recipient": "team@cartwave.example.com",
                    "summary": "bad letter",
                }
            },
            {"done": "one refused"},
        ]
    )
    artifact, _ = run_step(llm=llm)

    assert artifact.payload["files"] == []
    refused = artifact.payload["refused_tools"]
    assert any("signup" in item or "human-gated" in item for item in refused), refused


def test_a_write_with_no_channel_is_not_a_draft():
    """An unaimed draft cannot be dispatched later, so it is not queued."""
    for channel, recipient in (("", "x@y.dev"), ("sms", "555-0100"), ("email", "not-an-address")):
        llm = ScriptLLM(
            [
                {
                    "write": {
                        "content": "A perfectly ordinary letter.",
                        "channel": channel,
                        "recipient": recipient,
                    }
                },
                {"done": "refused"},
            ]
        )
        artifact, _ = run_step(llm=llm)
        assert artifact.payload["files"] == [], (channel, recipient)


@pytest.mark.parametrize(
    "channel, recipient, expected",
    [
        ("email", "team@example.org", ("email", "team@example.org")),
        ("directory", "https://listing.example.org/submit", ("directory", "https://listing.example.org/submit")),
        ("social", "https://x.example.org/importalpha", ("social", "https://x.example.org/importalpha")),
        # A post has no addressee; it goes to whichever account the token names.
        ("social", "", ("social", "")),
    ],
)
def test_publish_targets_that_make_sense_are_kept(channel, recipient, expected):
    target = _publish_target(channel, recipient)
    assert (target["channel"], target["recipient"]) == expected


@pytest.mark.parametrize(
    "channel, recipient",
    [
        ("email", "https://example.org"),
        ("email", "no-at-sign"),
        ("directory", "not a url"),
        ("sms", "555-0100"),
        ("", "team@example.org"),
        ("email", "http://169.254.169.254/"),
    ],
)
def test_publish_targets_that_do_not_make_sense_are_refused(channel, recipient):
    assert _publish_target(channel, recipient)["channel"] == "none"


def test_a_refused_target_never_leaves_a_file_behind():
    llm = ScriptLLM(
        [
            {"write": {"content": "hi", "channel": "email", "recipient": "http://169.254.169.254/"}},
            {"done": "refused"},
        ]
    )
    artifact, _ = run_step(llm=llm)
    assert artifact.payload["files"] == []
    assert artifact.payload["status"] == "failed"


def test_payment_terms_are_not_invented_when_the_wallet_is_unset(monkeypatch):
    """Better a step that fails than a letter telling an agent to send money to
    nowhere."""
    monkeypatch.delenv("X402_WALLET_ADDRESS")
    llm = ScriptLLM(
        [
            {
                "write": {
                    "content": "A normal letter about an API.",
                    "channel": "email",
                    "recipient": "team@cartwave.example.com",
                }
            },
            {"done": "nothing written"},
        ]
    )
    artifact, _ = run_step(llm=llm)
    assert artifact.payload["files"] == []
    assert any("wallet" in item or "refused" in item for item in artifact.payload["refused_tools"])


def test_an_exhausted_budget_reports_partial_not_done():
    llm = ScriptLLM([{"call": {"tool": "search_web", "args": {"query": "e-commerce agent builders"}}}])
    artifact, _ = run_step(llm=llm, max_tool_calls=1)

    assert artifact.payload["status"] == "failed"
    assert "exhausted" in artifact.payload["summary"] or "budget" in artifact.payload["summary"]


def test_a_partial_batch_is_labelled_partial():
    llm = ScriptLLM(
        [
            {
                "write": {
                    "content": "First letter.",
                    "channel": "email",
                    "recipient": "team@cartwave.example.com",
                }
            }
        ]
    )
    artifact, _ = run_step(llm=llm, max_tool_calls=1)
    assert artifact.payload["status"] == "partial"
    assert len(draft_files(artifact)) == 1


def test_a_step_with_no_goal_fails_loudly_instead_of_doing_invisible_work():
    """No goal_id means no workspace, and a loop that ran anyway would spend a
    budget and report nothing."""
    emitted = []
    artifact = handle_tool_step(
        role="outreach",
        system_prompt=outreach_worker.SYSTEM_PROMPT,
        artifact_type="drafts",
        event=assigned(OUTREACH_TASK, goal_id=None),
        llm=FakeLLM(),
        search=FakeSearchAPI(),
        producer=lambda topic, event, key=None: emitted.append((topic, event)),
        base_url=BASE_URL,
    )
    assert artifact.payload["status"] == "failed"
    assert "goal_id" in artifact.payload["summary"]
    assert [topic for topic, _ in emitted] == ["artifact.created"]


@pytest.mark.parametrize(
    "raw, valid",
    [
        ('{"done": "x"}', True),
        ('```json\n{"done": "x"}\n```', True),
        ('Sure! Here you go:\n{"done": "x"}\nHope that helps.', True),
        ("I will now search the web.", False),
        ('{"done": "x", "extra": 1}', True),
        ("", False),
        ("null", False),
    ],
)
def test_the_action_parser_is_forgiving_of_prose_and_strict_about_shape(raw, valid):
    action, error = parse_tool_action(raw)
    assert bool(action) == valid, (raw, action, error)
    assert bool(error) != valid


def test_output_paths_are_numbered_per_target():
    assert output_path_for(OUTREACH_TASK, "outreach", 1) == "outreach/draft-1.md"
    assert output_path_for(MARKETING_TASK, "marketing", 10) == "marketing_out/draft-10.md"


# ---------- the worker entrypoints ----------


@pytest.mark.parametrize(
    "module, source_topic, group",
    [
        (outreach_worker, "task.assigned.outreach", "outreach-worker"),
        (marketing_worker, "task.assigned.marketing", "marketing-worker"),
    ],
)
def test_worker_wiring(module, source_topic, group):
    assert module.SOURCE_TOPIC == source_topic
    assert module.OUTPUT_TOPIC == "artifact.created"
    assert module.GROUP_ID == group
    assert module.ARTIFACT_TYPE == "drafts"


def test_both_prompts_tell_the_worker_it_cannot_send():
    """The prompt is not the control — the refusals above are. But a worker that
    reads as though it may publish will spend its budget asking to, and the lane
    has no budget to spare for that."""
    for module in (outreach_worker, marketing_worker):
        text = module.SYSTEM_PROMPT.lower()
        assert "may not ask for one" in text
        assert "a human" in text
        assert "never invent" in text or "never a price" in text


def test_handle_produces_drafts_for_its_own_kind_of_task():
    for module, title in ((outreach_worker, OUTREACH_TASK), (marketing_worker, MARKETING_TASK)):
        emitted = []
        handle_tool_step(
            role=module.ROLE,
            system_prompt=module.SYSTEM_PROMPT,
            artifact_type=module.ARTIFACT_TYPE,
            event=assigned(title, module.ROLE),
            llm=FakeLLM(),
            search=FakeSearchAPI(),
            producer=lambda topic, ev, key=None: emitted.append(ev),
            base_url=BASE_URL,
        )
        assert emitted and emitted[0].payload["files"]


# --- the offline planner has to know these two intents ---------------------


def plan_for(goal: str) -> List[dict]:
    """What the orchestrator would schedule, with no model and no network."""
    return json.loads(FakeLLM().complete("planner system prompt", f"PLAN: {goal}"))["steps"]


def test_the_planner_sends_an_outreach_goal_to_the_outreach_lane():
    steps = plan_for(
        "Find 5 e-commerce agent builders and draft outreach messages to each of them."
    )
    assert [step["role"] for step in steps] == ["outreach"]


def test_the_planner_sends_a_directory_goal_to_the_marketing_lane():
    steps = plan_for("Find 10 AI agent directories or registries and draft submission manifests.")
    assert [step["role"] for step in steps] == ["marketing"]


def test_the_step_keeps_the_count_the_goal_asked_for():
    """The number is not decoration. FakeLLM reads it out of the step text to
    decide how many drafts to write, and a planner that paraphrases it away
    produces a two-draft run against a five-draft goal and still reports
    success — which is exactly what it did before this was pinned."""
    for goal, count in (
        ("Find 5 e-commerce agent builders and draft outreach messages.", 5),
        ("Find 10 AI agent directories or registries and draft submission manifests.", 10),
    ):
        steps = plan_for(goal)
        assert str(count) in steps[0]["task"], steps
        assert steps[0]["task"].strip() == goal.strip()


def test_the_planner_still_routes_a_landing_goal_to_landing():
    """The two new branches sit above the old one, so this is the regression
    check that they did not swallow it."""
    steps = plan_for("Rewrite the landing page with a hero and a pricing table.")
    assert [step["role"] for step in steps] == ["landing"]


def test_the_publisher_is_reachable_from_one_module_only():
    """The structural half of "no code path auto-sends".

    Everything else here shows that a worker *behaves* well. This one shows it
    cannot behave otherwise: `app.services.publisher` — the only module holding
    an SMTP-or-HTTP sender — is imported by exactly one file, the daemon that a
    supervisor starts and a human configures. A drafting worker has no import
    path to a send, so no tool call, prompt injection or model error can reach
    one either.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    holders = set()
    for source in sorted(list(root.glob("*.py")) + list((root / "app").rglob("*.py")) +
                         list((root / "scripts").glob("*.py"))):
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the suite would have failed first
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "app.services.publisher":
                holders.add(source.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.services.publisher":
                        holders.add(source.name)

    assert holders == {"publisher_worker.py"}, f"the sender became reachable from {sorted(holders)}"
