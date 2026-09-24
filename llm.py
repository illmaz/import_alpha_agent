"""LLM providers for the planner. FakeLLM is the default: no network, no key."""

from __future__ import annotations

import json
import os
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
        return "Offline FakeLLM response: no model was called for this step."

    @staticmethod
    def _deliverable(user: str) -> str:
        path = user.split("\n", 1)[0].removeprefix("DELIVERABLE:").strip()
        if path.endswith(".html"):
            return FAKE_LANDING_HTML
        return f"/* Placeholder for {path}, produced offline by FakeLLM. */\n"

    @staticmethod
    def _plan(user: str) -> str:
        goal = user.removeprefix("PLAN:").strip().lower()
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
