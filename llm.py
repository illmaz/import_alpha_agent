"""LLM providers for the planner. FakeLLM is the default: no network, no key."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from events import ACTIVE_ROLES

# Hard ceiling applied to every call, whatever the provider.
MAX_TOKENS = 4096

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

# Built once at import, so the exact same bytes go out on every call and the
# cached prefix stays valid. Never interpolate per-request data into this.
SYSTEM_PROMPT = (
    "You are the planning component of ImportAlpha, a China-to-US product "
    "sourcing intelligence service.\n\n"
    "You answer exactly two kinds of request, distinguished by the first word "
    "of the user message.\n\n"
    "PLAN: - break the stated goal into ordered steps. Reply with ONLY a JSON "
    "object, no prose and no code fences:\n"
    '{"reasoning": "<why this plan>", "steps": [{"role": "<role>", "task": "<what to do>"}]}\n'
    # sorted(): a frozenset's iteration order must not leak into the bytes we
    # send, or the cached prefix changes between processes.
    f"Allowed roles: {', '.join(sorted(ACTIVE_ROLES))}. Use at most 5 steps, and "
    "prefer the fewest that do the job — one step is normal for a single file.\n\n"
    "A step that must produce a file MUST name the exact relative path in its own "
    "task text, for example: \"write landing/index.html into the workspace\". "
    "Allowed file types: .html, .css, .js, .md, .txt. A step whose text names no "
    "path produces written analysis only and no file, so a goal that asks for a "
    "file and never names one delivers nothing.\n\n"
    "SUMMARIZE: - the steps of a goal have finished and their artifacts follow. "
    "Reply with 2-3 plain sentences describing what was produced. No JSON.\n\n"
    "Never invent facts, prices, supplier names or measurements. Every real "
    "datapoint must carry a source_url, observed_at and confidence, so when you "
    "have no source, say so rather than guessing."
)


# What FakeLLM hands back when a step asks for an .html deliverable. It is a
# real, working page rather than a marker string: the offline dogfood run is an
# acceptance test, and a placeholder would let it pass while proving nothing.
# It repeats the synthetic-data disclaimer because any page rendering the
# sample fixtures must, whoever wrote it.
FAKE_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ImportAlpha - China-to-US product intelligence API</title>
<style>
  :root { --ink:#0f172a; --muted:#475569; --line:#e2e8f0; --bg:#f8fafc; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:64rem; margin:0 auto; padding:0 1.25rem; }
  h1 { font-size:clamp(2rem,6vw,3.5rem); line-height:1.1; letter-spacing:-.02em; margin:0 0 1rem; }
  h2 { font-size:1.6rem; letter-spacing:-.01em; margin:0 0 .5rem; }
  .lede { font-size:1.125rem; color:var(--muted); max-width:44rem; }
  section { padding:3.5rem 0; }
  .grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(15rem,1fr)); }
  .card { background:#fff; border:1px solid var(--line); border-radius:.75rem; padding:1.25rem; }
  .price { font-size:2rem; font-weight:600; }
  .note { background:#fffbeb; border:1px solid #fcd34d; color:#78350f;
          border-radius:.75rem; padding:1rem; margin:1.5rem 0; }
  .scroll { overflow-x:auto; }
  table { width:100%; min-width:40rem; border-collapse:collapse; font-size:.9rem; }
  th, td { text-align:left; padding:.6rem .5rem; border-bottom:1px solid var(--line); }
  th { font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  button { font:inherit; border:1px solid var(--line); background:#fff; color:var(--ink);
           border-radius:.5rem; padding:.5rem .9rem; cursor:pointer; }
  button[aria-selected="true"] { background:var(--ink); color:#fff; border-color:var(--ink); }
  footer { border-top:1px solid var(--line); padding:2rem 0; color:var(--muted); font-size:.875rem; }
</style>
</head>
<body>
<header class="wrap" style="padding-top:2rem"><strong>ImportAlpha</strong></header>

<section class="wrap">
  <h1>China-to-US product intelligence API for e-commerce agents</h1>
  <p class="lede">Ask whether a product is worth importing and get a structured
  answer: an opportunity score, a China unit-cost range, landed cost, margin,
  a competition signal and risk flags &mdash; each datapoint carrying its own
  source, timestamp and confidence.</p>
</section>

<section class="wrap">
  <h2>What you get back</h2>
  <div class="grid">
    <div class="card"><strong>Viability score</strong><p class="lede">0&ndash;100 per SKU, with the terms that produced it.</p></div>
    <div class="card"><strong>Landed cost</strong><p class="lede">Unit cost as a range, plus freight and duty to a US door.</p></div>
    <div class="card"><strong>Risk flags</strong><p class="lede">Compliance, IP, oversized, battery, thin margin.</p></div>
    <div class="card"><strong>Provenance</strong><p class="lede">Source URL, observation time and confidence on every field.</p></div>
  </div>
</section>

<section class="wrap">
  <h2>Sample reports</h2>
  <div class="note"><strong>These samples are synthetic.</strong> Every figure is
  hand-written to show the shape of a real report. Nothing here was observed from
  a marketplace, supplier or customs source. Do not source or price against them.</div>
  <div id="tabs" style="display:flex;gap:.5rem;flex-wrap:wrap"></div>
  <div id="report"><p class="lede">Loading sample reports&hellip;</p></div>
</section>

<section class="wrap">
  <h2>Pricing</h2>
  <div class="grid" id="pricing"><p class="lede">Loading pricing&hellip;</p></div>
</section>

<section class="wrap">
  <h2>For agents</h2>
  <p class="lede">Machine-readable discovery. No signup, no browser step, no
  human in the loop. Pay per call in USDC over x402, or use an API key.</p>
  <div class="grid">
    <div class="card">
      <strong><a href="/llms.txt">/llms.txt</a></strong>
      <p class="lede">The manifest: what this sells, how to authenticate, what it costs.</p>
    </div>
    <div class="card">
      <strong><a href="/agent-guide">/agent-guide</a></strong>
      <p class="lede">Markdown guide: the x402 payment loop, testnet faucets, worked curl.</p>
    </div>
    <div class="card">
      <strong><a href="/openapi.json">/openapi.json</a></strong>
      <p class="lede">The full contract, generated from the code.</p>
    </div>
    <div class="card">
      <strong><a href="/v1/agent/info">/v1/agent/info</a></strong>
      <p class="lede">Live payment parameters: wallet, price, accepted networks.</p>
    </div>
  </div>
  <p class="lede" style="font-size:.9rem">Testnet only today: payment is settled
  on Base Sepolia. Read <code>networks[].accepted</code> from
  <code>/v1/agent/info</code> before sending anything.</p>
</section>

<footer class="wrap">ImportAlpha Lite &mdash; sample data on this page is synthetic.</footer>

<script>
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
fetch("/v1/public/sample-reports").then(r => r.json()).then(({ reports }) => {
  let active = 0;
  const tabs = document.getElementById("tabs"), out = document.getElementById("report");
  const paint = () => {
    tabs.innerHTML = reports.map((r, i) =>
      `<button role="tab" aria-selected="${i === active}" data-i="${i}">${esc(r.category.replace(/_/g, " "))}</button>`).join("");
    const r = reports[active];
    out.innerHTML = `<h3>${esc(r.title)}</h3><p class="lede">${esc(r.summary)}</p>
      <div class="scroll"><table><thead><tr>
        <th>Product</th><th>Score</th><th>Unit cost</th><th>Landed</th><th>Margin</th><th>Competition</th>
      </tr></thead><tbody>${r.products.map(p => `<tr>
        <td>${esc(p.title)}</td><td>${p.opportunity_score}</td>
        <td>$${p.estimated_unit_cost_usd.low_usd.toFixed(2)}&ndash;$${p.estimated_unit_cost_usd.high_usd.toFixed(2)}</td>
        <td>$${p.estimated_landed_cost_usd.toFixed(2)}</td>
        <td>${p.estimated_margin_pct.toFixed(1)}%</td>
        <td>${esc(p.competition_signal)}</td></tr>`).join("")}</tbody></table></div>
      <p class="lede" style="font-size:.8rem">${esc(r.disclaimer)}</p>`;
    tabs.querySelectorAll("button").forEach(b =>
      b.onclick = () => { active = Number(b.dataset.i); paint(); });
  };
  paint();
}).catch(e => {
  document.getElementById("report").innerHTML = "<p>Could not load sample reports.</p>";
});

// Prices come from the API, never from this file: a hardcoded number here
// could drift from what checkout actually charges.
fetch("/v1/public/pricing").then(r => r.json()).then(({ tiers }) => {
  document.getElementById("pricing").innerHTML = tiers.map(t => `
    <div class="card">
      <div class="price">$${(t.amount_cents / 100).toLocaleString("en-US")}</div>
      <p class="lede">${esc(t.credits ? t.credits + " report credits." : (t.description || ""))}</p>
      <p class="lede" style="font-size:.85rem">${t.self_serve ? "Buy with a card." : "Contact us &mdash; scoped with you first."}</p>
    </div>`).join("");
}).catch(e => {
  document.getElementById("pricing").innerHTML = "<p>Could not load pricing.</p>";
});
</script>
</body>
</html>
"""


class LLM(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return the model's text response."""


class FakeLLM(LLM):
    """Deterministic and offline. Default provider, so tests need no API key.

    Pass `responses` to script exact replies for a test; once that queue is
    drained it falls back to the canned plan/summary below.
    """

    def __init__(self, responses: Optional[List[str]] = None) -> None:
        self.responses = list(responses) if responses else []
        self.calls: List[Tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.responses:
            return self.responses.pop(0)
        if user.startswith("SUMMARIZE:"):
            return (
                "Every planned step reported an artifact. The briefs are "
                "placeholders: no sourced datapoints were attached yet."
            )
        if user.startswith("DELIVERABLE:"):
            return self._deliverable(user)
        if user.startswith("PLAN:"):
            return self._plan(user)
        # A tool-loop turn is addressed by the worker's own prompt, so the
        # offline stand-in answers it with the same shape rather than falling
        # through to the generic line, which the parser would reject.
        if user.startswith("TASK:") and "\nTURN: " in user:
            return self._tool_turn(user)
        return "Offline FakeLLM response: no model was called for this step."

    @staticmethod
    def _deliverable(user: str) -> str:
        path = user.split("\n", 1)[0].removeprefix("DELIVERABLE:").strip()
        if path.endswith(".html"):
            return FAKE_LANDING_HTML
        return f"/* Placeholder for {path}, produced offline by FakeLLM. */\n"

    # --- the tool loop ----------------------------------------------------
    # A tool-using step is a transcript, not a single reply, so FakeLLM plays
    # the whole conversation: search, read what came back, draft one file per
    # target, done. It reads the observations it was handed rather than
    # reciting fixed text, so a dogfood draft names the targets the search
    # actually returned. That makes the offline run an end-to-end test of the
    # plumbing instead of a replay of a string.

    _OUTREACH_QUERIES = (
        "e-commerce agent builders shopify tiktok seller bots",
        "indie hackers dropshipping margin tools builder",
        "dropshipper community forum shipping weight problems",
    )
    _MARKETING_QUERIES = (
        "ai agent tool directories api marketplaces registries",
        "langchain hubs composio registries agent integration directory",
    )

    @staticmethod
    def _turn_number(user: str) -> int:
        for line in user.splitlines():
            if line.startswith("TURN:"):
                digits = "".join(ch for ch in line.split()[1] if ch.isdigit())
                return int(digits or 1)
        return 1

    @staticmethod
    def _task_of(user: str) -> str:
        for line in user.splitlines():
            if line.startswith("TASK:"):
                return line.removeprefix("TASK:").strip()
        return ""

    @staticmethod
    def _observations(user: str, label: str) -> list:
        """Parse what earlier turns of this loop actually returned."""
        found = []
        for line in user.splitlines():
            marker = f"OBSERVATION {label} -> "
            if not line.startswith(marker):
                continue
            try:
                found.append(json.loads(line[len(marker) :]))
            except json.JSONDecodeError:
                continue
        return found

    def _tool_turn(self, user: str) -> str:
        task = self._task_of(user)
        lowered = task.lower()
        marketing = any(
            word in lowered
            for word in ("director", "registr", "marketplace", "submission", "manifest", "seo")
        )
        queries = self._MARKETING_QUERIES if marketing else self._OUTREACH_QUERIES

        wanted_match = re.search(r"\b(\d{1,3})\b", task)
        wanted = int(wanted_match.group(1)) if wanted_match else (3 if marketing else 2)
        wanted = max(1, min(wanted, 10))

        results = [row for obs in self._observations(user, "search_web") for row in obs.get("results", [])]
        pages = {obs["url"]: obs for obs in self._observations(user, "read_url") if "url" in obs}
        # Only a write that got a path counts as written. A refused draft is
        # reported under the same label, and counting it would tell the loop its
        # work was done when nothing was on disk.
        written = [w for w in self._observations(user, "write") if w.get("path")]
        used_queries = self._calls(user, "search_web")
        reads_issued = self._calls(user, "read_url")

        # Candidates, deduped by URL, in the order the lookups ranked them. The
        # position in this list — not a set of already-covered recipients —
        # decides which target is drafted next. Matching on recipients cannot
        # work: a draft is echoed back aimed at whatever the recipient field
        # holds, which is an address for a prospect that publishes one and a page
        # URL for one that does not, so the filter never matches and the loop
        # writes the same letter five times while four prospects go uncontacted.
        seen: set = set()
        targets = []
        for row in results:
            url = str(row.get("url") or "")
            if url and url not in seen:
                seen.add(url)
                targets.append(row)
        # How many drafts this loop can produce at all: one per target found.
        draftable = min(wanted, len(targets))

        # 1. One search per query, so the corpus is actually consulted.
        if used_queries < len(queries):
            return json.dumps(
                {"call": {"tool": "search_web", "args": {"query": queries[used_queries], "max_results": 8}}}
            )

        # 2. Read context for the targets we intend to write about. Each read
        # costs a turn out of the loop's budget, so the cap is what keeps a
        # ten-target goal from spending every turn on page fetches.
        if reads_issued < min(len(targets), draftable, 5):
            return json.dumps({"call": {"tool": "read_url", "args": {"url": targets[reads_issued]["url"]}}})

        # 3. Draft one file per distinct target, using what the lookups said.
        if len(written) < draftable:
            pick = targets[len(written)]
            return json.dumps({"write": self._draft(pick, pages.get(pick["url"]), marketing)})

        kind = "submission manifest" if marketing else "outreach message"
        return json.dumps(
            {
                "done": f"Wrote {len(written)} {kind}(s), one per target, from {len(targets)} distinct "
                f"candidate(s) returned by {len(queries)} lookup(s); read {len(pages)} page(s)."
            }
        )

    @staticmethod
    def _calls(user: str, tool: str) -> int:
        """How many times this loop already invoked `tool`.

        Only `ACTION` lines count. The instruction block of the prompt shows one
        example call per tool, and a substring search over the whole prompt reads
        those examples as history — which silently spends a search and a page
        fetch before the loop has taken a single step.
        """
        count = 0
        for line in user.splitlines():
            if not line.startswith("ACTION "):
                continue
            body = line[len("ACTION ") :]
            try:
                echoed = json.loads(body)
            except json.JSONDecodeError:
                # The transcript caps an echo at 600 characters, so a long action
                # can arrive cut off. Fall back to the substring test rather than
                # losing the step.
                count += 1 if f'"tool": "{tool}"' in body else 0
                continue
            if isinstance(echoed, dict) and str(echoed.get("tool") or "") == tool:
                count += 1
        return count

    @staticmethod
    def _draft(target: dict, page: Optional[dict], marketing: bool) -> dict:
        name = str(target.get("title") or "there").strip()
        url = str(target.get("url") or "")
        snippet = str(target.get("snippet") or "").strip()
        # The address comes from the page or nothing. An outreach draft aimed at
        # an invented address is a wasted send, and worse, a sent message whose
        # one factual claim is wrong.
        contact = str((page or {}).get("contact") or "").strip()
        requires = str((page or {}).get("requires") or "").strip()
        context = str((page or {}).get("text") or snippet).replace("\n", " ").strip()
        recipient = contact or url

        if marketing:
            body = "\n".join(
                [
                    f"# Submission manifest — ImportAlpha Lite at {name}",
                    "",
                    f"Target: {url}",
                    "",
                    "## Fields",
                    "",
                    "- **Name:** ImportAlpha Lite",
                    "- **One-line description:** China-to-US product viability and landed-cost "
                    "answers, as an API.",
                    "- **Long description:** Given a product or a category, returns an opportunity "
                    "score, a China unit-cost range, a landed cost, a margin, a competition signal "
                    "and risk flags. Every datapoint carries a source URL, an observation timestamp "
                    "and a confidence.",
                    "- **OpenAPI URL:** /openapi.json, generated from the running service",
                    "- **Agent discovery:** /llms.txt  ·  /agent-guide  ·  /v1/agent/info",
                    "- **Categories:** shopping, pricing, data-quality",
                    "- **Use cases:** restock decisions for thin-catalogue sellers, landed-cost "
                    "quoting inside a sourcing agent, pre-listing viability screening",
                    "- **Pricing:** per call in USDC over x402, one credit per verified transfer; "
                    "card packs also exist. Live numbers at /v1/agent/info.",
                    "- **Auth:** bearer API key, or no account at all via the payment header",
                    "",
                    f"## Why it fits {name}",
                    "",
                    f"What the listing says: {snippet}",
                    "",
                    f"What the page asks submitters for: {requires or context[:420]}",
                    "",
                    "## Say this, do not skip it",
                    "",
                    "- Sample reports served by this API are synthetic and labelled synthetic. They "
                    "are not to be quoted as observed results.",
                    "- Settlement is testnet-only in this deployment. Read `networks[].accepted` at "
                    "/v1/agent/info before describing payment as available.",
                    "- Coverage today is home organisation and adjacent categories, not global.",
                ]
            )
            return {
                "content": body,
                "channel": "directory",
                "recipient": url,
                "summary": f"Directory submission manifest for {name}",
            }

        greeting = name.split(" ")[0]
        body = "\n".join(
            [
                f"Subject: Landed-cost answers {name} does not have to estimate",
                "",
                f"Hi {greeting},",
                "",
                f"What we found on {name}: {context[:420]}",
                "",
                "That is the gap we sell into. ImportAlpha is an API, not a dashboard: ask whether a "
                "product is worth importing from China to the US and get back an opportunity score, a "
                "China unit-cost range, a landed cost, a margin, a competition signal and risk flags — "
                "each with a source URL, an observation timestamp and a confidence.",
                "",
                "Two things worth saying plainly. The sample reports we publish are synthetic and "
                "labelled that way, so do not quote them as observed results. And the data behind the "
                "scoring is curated by hand today rather than crawled, which means the honest claim is "
                "a sourced answer on a narrow category — home organisation and what sits next to it — "
                "not global coverage.",
                "",
                "If you want it behind an agent, the whole loop is below. No account, no key request, "
                "nothing to fill in:",
            ]
        )
        return {
            "content": body,
            "channel": "email",
            "recipient": recipient,
            "summary": f"Outreach draft to {name}",
        }

    @staticmethod
    def _plan(user: str) -> str:
        goal = user.removeprefix("PLAN:").strip()
        lowered = goal.lower()
        # The same doctrine as the landing branch, and for the same reason: a
        # canned offline lane has to be driveable from a plain-text goal, or
        # testing P3.6 means paying a model to plan it. The step carries the
        # goal verbatim rather than a paraphrase, because the paraphrase loses
        # the count — "draft 5 messages" is the whole spec of the step, and a
        # double that drops it produces two drafts and calls the run a success.
        if "outreach" in lowered or "agent builder" in lowered or "dropshipper" in lowered:
            return json.dumps(
                {
                    "reasoning": "Offline plan: one outreach deliverable.",
                    "steps": [{"role": "outreach", "task": goal}],
                }
            )
        if any(word in lowered for word in ("director", "regist", "marketplace", "seo", "marketing")):
            return json.dumps(
                {
                    "reasoning": "Offline plan: one agent-SEO deliverable.",
                    "steps": [{"role": "marketing", "task": goal}],
                }
            )
        goal = lowered
        if "landing" in goal or "index.html" in goal:
            return json.dumps(
                {
                    "reasoning": "Offline plan: one landing deliverable.",
                    "steps": [
                        {
                            "role": "landing",
                            "task": (
                                "Write landing/index.html into the workspace: hero, value "
                                "proposition, pricing table ($99 / $299 / $999), and a "
                                "sample-report viewer that fetches /v1/public/sample-reports"
                            ),
                        }
                    ],
                }
            )
        return json.dumps(
            {
                "reasoning": "Canned offline plan from FakeLLM.",
                "steps": [
                    {"role": "product", "task": "Shortlist candidate home organization products"},
                    {"role": "product", "task": "Draft a sourcing brief for the shortlist"},
                ],
            }
        )


class AnthropicLLM(LLM):
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # lazy: only this provider needs the SDK installed

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": user}],
        )
        # Skip thinking blocks, which are on by default for current models.
        return "".join(block.text for block in response.content if block.type == "text")


class OpenAICompatibleLLM(LLM):
    """Any OpenAI-shaped endpoint, including Qwen via DashScope's compat mode."""

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        from openai import OpenAI  # lazy: only this provider needs the SDK

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""


def get_llm() -> LLM:
    """Build the provider named by LLM_PROVIDER. Defaults to FakeLLM."""
    provider = os.environ.get("LLM_PROVIDER", "fake").strip().lower()

    if provider == "fake":
        return FakeLLM()

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(f"LLM_PROVIDER={provider} requires LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")

    if provider == "anthropic":
        return AnthropicLLM(api_key, model or DEFAULT_ANTHROPIC_MODEL)

    if provider == "openai":
        base_url = os.environ.get("LLM_BASE_URL")
        if not base_url:
            raise RuntimeError("LLM_PROVIDER=openai requires LLM_BASE_URL")
        if not model:
            raise RuntimeError("LLM_PROVIDER=openai requires LLM_MODEL")
        return OpenAICompatibleLLM(api_key, model, base_url)

    raise ValueError(f"unknown LLM_PROVIDER: {provider!r} (fake, anthropic, openai)")
