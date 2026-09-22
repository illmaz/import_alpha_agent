# Decisions

Durable architectural decisions and their reasoning. `docs/STATE.md` tracks
what is done; this file records *why* things are the way they are.

## Phase 3 — Dockerized run lane (2026-09-22)

### PR #1 was discarded, not merged

Phase 3 was first built in a separate Qwen Coder session, on branch
`qwen-coder-vs-code-connection-fix-bc4eb` (`407f1e7`, PR #1). It was closed
without merging. Its merge base with `main` is `30c759c` — the commit *before*
Phase 1 — so it never saw the event bus, the LLM orchestrator or the liveness
work. It shipped its own async `Bus` class (`await bus.publish(Event)`) and
imported `Topic`, `RouterRequest`, `RouterResult` and `ProductResult`, none of
which exist in this `events.py`. A dry-run merge conflicted in 9 files, and
resolving them would still have left code that fails on import.

The branch is kept for reference. What was salvaged from it is design, not
code: compose-level healthcheck gating and the broker-resilience acceptance
test. Three bugs in it were fixed rather than carried over — two broker
listeners bound to the same port (the broker refuses to start), the
engineering and landing services both running `product_worker.py`, and a hard
`env_file: .env` that fails when no `.env` exists.

The lesson worth keeping: a parallel agent session must be branched from the
current head, and its work checked in before the main line moves on. Four
commits of divergence made an otherwise reasonable piece of work unusable.

### Compose is the only place bootstrap servers are configured

`bus.BOOTSTRAP_SERVERS` reads `BOOTSTRAP_SERVERS`, defaulting to
`localhost:9092`. Every compose service sets `kafka:9092`. That env var is the
*only* Python-visible change Phase 3 made; the orchestrator daemon and all
workers run byte-identical inside and outside Docker, so a container-only bug
cannot hide in a code path tests never take.

### The broker advertises kafka:9092, so host clients no longer work

`KAFKA_CFG_ADVERTISED_LISTENERS=PLAINTEXT://kafka:9092` is a single listener,
which is what makes container-to-container traffic correct and avoids PR #1's
same-port collision. The cost: a client on the host that connects to
`localhost:9092` is redirected to `kafka:9092`, which does not resolve outside
the compose network. Running `python scripts/submit_goal.py` from the host
therefore hangs; use `docker compose run --rm cli ...` instead. The port is
still published on `127.0.0.1:9092` for probes. Restoring host access means a
second `PLAINTEXT_HOST` listener on its own port (9093 is taken by the
controller, so 29092) — deliberately not done yet, since nothing needs it.

### Kafka has no restart policy; the app services do

The six application services are `restart: unless-stopped`. Kafka deliberately
is not: the broker-resilience check kills it and expects it to stay down until
brought back by hand. An auto-restarting broker would make that check untestable.

### The cli service is profile-gated

`profiles: ["cli"]` keeps the one-shot goal submitter out of
`docker compose up`, which would otherwise start it, submit a hardcoded goal
and leave an exited container in `ps` on every boot. It is reached only through
`docker compose run --rm cli ...`.

### BROKER-RESILIENCE CHECK — outcome

**PASSED, 2026-09-22.** Killed kafka, waited 15s, restarted it, submitted a new
goal. All six app containers stayed up (RestartCount 0) and the goal completed
end to end with no duplicate dispatches.

**Caveat worth knowing before relying on this:** recovery is not immediate.
librdkafka logs `Connection refused` throughout the outage, then needs a ~45s
consumer-group session timeout plus a rebalance before goals are consumed
again. A goal submitted during that window is not lost — it sits on the topic
and is picked up after the rebalance — but "resilient" here means *eventually
self-healing*, not *uninterrupted*. Note that 45s is well under the 120s
`STEP_TTL_SECONDS`, so an outage does not by itself trip stall detection; a
longer outage would, and the goal would replan rather than hang.

## Phase 2.5 — Liveness (2026-09-21)

### The liveness invariant

**Every goal terminates in exactly one of three states:**

1. **completed** — every step produced an artifact; `goal.completed` published.
2. **escalated** — `human.approval.required` published for an unplannable or
   stalled goal (`plan_validation_failed`, `step_stalled`, `empty_goal`).
3. **guard-breached** — `human.approval.required` published because a loop
   guard tripped (`max_steps_exceeded`).

No goal may sit pending forever. Before Phase 2.5 two paths violated this: a
plan targeting a role with no worker hung silently, and a crashed or slow worker
left its step pending with nothing watching. Both now terminate.

Anything added later that can leave a goal pending — new roles, sub-tasks,
nested depth, external calls — must come with the path that ends it. When
changing this area, the question to answer is "what ends this goal if the happy
path never happens?"

### ACTIVE_ROLES gates planning, not just dispatch

`events.ACTIVE_ROLES` (env-overridable, default all three roles) is enforced
inside `GoalPlan` validation, so an inactive role fails the plan rather than
being filtered out of it. A silently dropped step would give a goal that
completes while having skipped work the model thought was necessary — worse
than a loud escalation. It also feeds `llm.SYSTEM_PROMPT`, so the model is only
ever told about roles that can actually run.

Set `ACTIVE_ROLES=product,landing` to run a subset while a worker is down.

### Stall detection replans once, then halts

The orchestrator stamps a dispatch time per step. A ticker thread (10s, env
`STALL_TICK_SECONDS`) finds pending steps older than `STEP_TTL_SECONDS`
(default 120). The first stall on a goal triggers one replan, drawn from the
same `MAX_REPLANS` budget as validation retries — so a goal whose plan already
needed a retry escalates on its first stall instead of replanning. The second
stall always escalates.

Replanned steps get generation-tagged ids (`{goal_id}-R1-S1`). Without that, a
merely-slow worker's late artifact would arrive after the replan and satisfy a
step it never worked on, completing the goal on false evidence.

The TTL is wall-clock, not a worker heartbeat: a long-running legitimate step
looks identical to a dead one. Raise `STEP_TTL_SECONDS` before adding slow work,
or add heartbeats.

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
