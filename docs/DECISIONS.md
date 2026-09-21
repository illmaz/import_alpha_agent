# Decisions

Durable architectural decisions and their reasoning. `docs/STATE.md` tracks
what is done; this file records *why* things are the way they are.

## Phase 2 — LLM orchestration (2026-09-21)

### FakeLLM is the default provider

`LLM_PROVIDER` unset means `FakeLLM`: deterministic canned JSON, no network, no
key. Tests and a fresh clone therefore run with zero credentials, and nobody
accidentally spends money by running the daemon. Real providers are opt-in via
`LLM_PROVIDER=anthropic|openai`.

### One static system prompt for both planning and synthesis

`llm.SYSTEM_PROMPT` is built once at import and sent byte-identical on every
call; the *user* message carries a `PLAN:` or `SUMMARIZE:` prefix to select the
mode. Splitting it into two prompts would have meant two cache prefixes.

**Known caveat:** the prompt is roughly 250 tokens, and Anthropic's minimum
cacheable prefix is 512–4096 tokens depending on model. The `cache_control:
ephemeral` marker is therefore correct but currently a no-op — nothing is
actually cached yet. It starts paying off once the system prompt grows (adding
scoring rules, category context, few-shot examples). Verify with
`usage.cache_read_input_tokens` before assuming it works.

### Step completion is correlated by task_id, not goal_id

`product_worker` builds a fresh payload for its artifact and does not echo
`goal_id`, but it does preserve `task_id`. Since Phase 2 was required to leave
workers unchanged, the orchestrator assigns deterministic step ids
(`{goal_id}-S{n}`) and keeps a `task_id -> goal_id` map. Tasks still carry
`goal_id` and `depth` in their payload for downstream consumers.

If workers later echo `goal_id`, this map can go away.

### The orchestrator owns goal_id, never the model

`GoalPlan.goal_id` is overwritten with our own id before validation. The model
never sees the real id, so trusting an echoed one would let a bad completion
misroute or collide with another goal.

### MAX_REPLANS is the planning-attempt ceiling

One counter covers both rules: a goal gets at most `MAX_REPLANS` (2) planning
attempts — the first, plus the single retry on invalid output. Exhausting them
publishes `human.approval.required` and drops the goal.

A `MAX_STEPS_PER_GOAL` (5) breach is different: it is a guard breach, not a bad
response, so it escalates *immediately* without burning a retry. Both halt only
the offending goal; other goals keep running.

### Default Anthropic model is claude-opus-5

Set in `llm.DEFAULT_ANTHROPIC_MODEL`, overridable with `LLM_MODEL`.

### LLM_BASE_URL is required for the openai provider

No default endpoint is hardcoded. The OpenAI-compatible path covers several
vendors (Qwen/DashScope among them) whose URLs differ by region, and guessing
one would be inventing configuration. Missing it raises at construction.

### Only the product role has a live worker

`WorkerRole` allows `product`, `engineering` and `landing`, and the router will
happily fan out to `task.assigned.engineering` / `.landing` — but no process
consumes those topics yet, so such steps never produce an artifact and their
goal never completes. `FakeLLM`'s canned plan uses `product` steps only, so the
default demo runs to completion. Add the missing workers before letting a real
model plan across all three roles.

## Phase 1 — Event bus foundation (2026-09-21)

- **Wedge:** home organization category, US market.
- **Monetization:** report-first + API credits; x402 is phase 2.
- **Tooling:** Claude Code in VS Code first; Agent Sessions UI for parallel agents later.
- **Python:** local system Python is 3.9 but the stack requires 3.12. Use
  `/opt/homebrew/bin/python3.12 -m venv .venv`; plain `python3` fails on this
  repo's type hints.
- **Kafka image:** `bitnami/kafka:3.9` is gone from Docker Hub (404) — Bitnami
  retired versioned tags to the `bitnamilegacy` namespace in 2025, so
  docker-compose pins `bitnamilegacy/kafka:3.9`. Verified alternative if that
  namespace is ever pulled: `apache/kafka:3.9.0`, which uses `KAFKA_*` env vars
  instead of Bitnami's `KAFKA_CFG_*`.
- **Stub artifacts:** `product_worker` emits `status: "stub"` with an empty
  `datapoints` list rather than a plausible-looking fake brief. Inventing
  numbers would violate the AGENTS.md sourcing rule and risks a placeholder
  being mistaken for real data later.
- **Benign Kafka errors:** `bus.consume` treats `UNKNOWN_TOPIC_OR_PART` as
  benign alongside `_PARTITION_EOF`. Consumers subscribe before a topic exists
  (auto-created on first produce), which otherwise logged a misleading ERROR on
  every worker startup.
- **Redis-era files deleted:** the original orchestrator/router/worker/logger
  and Dockerfile were written against a Redis bus and broke when bus.py was
  rewritten. Recoverable at commit `30c759c`.
