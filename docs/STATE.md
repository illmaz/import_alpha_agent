# Current State

## Phase

P1 in progress — FastAPI skeleton and OpenAPI contract are up; no data layer yet.

## Completed

- Defined wedge: ImportAlpha Lite (China-to-US product viability API, home organization first)
- Defined MVP offer and pricing ($99 / $299 / $999)
- Defined agent org structure
- Decided to start with Python + Kafka event bus (no LLM yet)
- Created project context docs

**Task 1 (BACKLOG P0 items 1-5 + Event schema tests) — FINISHED 2026-09-21.**

- Created Python project structure (root modules + `tests/`)
- Added requirements.txt (confluent-kafka, pydantic, fastapi, uvicorn, pytest — pinned floors)
- Added docker-compose.yml (single-node Kafka 3.9, KRaft mode, no ZooKeeper, port 9092, auto-create topics, replication factor 1)
- Added events.py (Pydantic v2 `Event`: event_id/event_type/task_id/agent/payload/created_at)
- Added bus.py (`produce(topic, event, key)` / `consume(topics, group_id, handler)`, BOOTSTRAP_SERVERS=localhost:9092)
- Added tests/test_events.py (10 tests, all passing under Python 3.12)
- Removed the broken Redis-era files (`orchestrator.py`, `router_worker.py`, `product_worker.py`, `artifact_logger.py`, `Dockerfile`); they are recoverable from commit `30c759c` if ever needed

**Task 2 (BACKLOG P0 items 5-8 + end-to-end flow) — FINISHED 2026-09-21.**

- Added orchestrator.py (publishes 3 dummy tasks as `task.created`: PROD-001/product, ENG-001/engineering, LAND-001/landing)
- Added router_worker.py (consumes `task.created`, re-emits `task.assigned` to `task.assigned.{role}`)
- Added product_worker.py (consumes `task.assigned.product`, simulates work, emits `artifact.created`)
- Added artifact_logger.py (consumes `artifact.created`, prints the artifact to console)
- **Verified the full flow end to end against a live broker**: PROD-001 travelled Orchestrator -> Router -> Product Worker -> Logger with no errors. `bitnamilegacy/kafka:3.9` starts and serves on localhost:9092 as configured, so P0 is now complete.

**Phase 2 (LLM-powered orchestrator daemon) — FINISHED 2026-09-21.**

- Added llm.py (`LLM` base, `FakeLLM` default, `AnthropicLLM`, `OpenAICompatibleLLM`; one static cache-friendly system prompt, hard `MAX_TOKENS` ceiling)
- Added `WorkerRole` / `PlanStep` / `GoalPlan` to events.py (role restricted to known worker roles)
- Rewrote orchestrator.py as a long-running daemon: consumes `user.goals`, plans via LLM, dispatches `task.created` per step, tracks `artifact.created` on a threaded consumer, publishes `goal.completed` with an LLM synthesis
- Loop guards: `MAX_STEPS_PER_GOAL=5`, `MAX_REPLANS=2`; a breach publishes `human.approval.required` and halts that goal only
- Added scripts/submit_goal.py (CLI to publish a plain-text goal)
- Added tests/test_orchestrator.py (20 tests, FakeLLM + recording producer, no broker needed)
- **Verified live**: goal `G-3f7086c2` planned into 2 steps, both routed and worked, `goal.completed` published with synthesis. 30/30 tests pass.

**Phase 2.5 (liveness — every goal terminates) — FINISHED 2026-09-21.**

- Added engineering_worker.py and landing_worker.py, mirroring product_worker's honest stub pattern (`status: "stub"`, `datapoints: []`)
- Added `ACTIVE_ROLES` registry to events.py (env-overridable, defaults to all three roles); `GoalPlan` validation now rejects steps targeting a role with no worker, so the orchestrator cannot dispatch to one
- Added stall detection: per-step dispatch timestamps, a 10s ticker thread, `STEP_TTL_SECONDS` (default 120, env-overridable). First stall replans once against the `MAX_REPLANS` budget; second stall publishes `human.approval.required` and halts that goal only
- Replanned steps use generation-tagged ids (`{goal_id}-R1-S1`) so a late artifact from a stalled dispatch cannot falsely complete a goal
- Recorded the liveness invariant in docs/DECISIONS.md
- Added tests/test_liveness.py and tests/helpers.py (shared offline doubles); **49 tests pass**
- **Verified live**: happy path completed with all three workers running; with the product worker deliberately absent, a goal stalled, replanned once, stalled again and escalated — `human.approval.required` confirmed on the topic with `reason=step_stalled`

**Phase 3 (dockerized run lane) — FINISHED 2026-09-22.**

- Rebuilt from scratch on main's sync architecture at commit `5374037`. The
  earlier Qwen-built Phase 3 (PR #1, branch `qwen-coder-vs-code-connection-fix-bc4eb`,
  commit `407f1e7`) was **discarded, not merged** — it branched from `30c759c`,
  predating Phases 1/2/2.5, and targeted an async `Bus` class that does not
  exist here. See DECISIONS.md.
- Made `bus.BOOTSTRAP_SERVERS` env-configurable
  (`os.environ.get("BOOTSTRAP_SERVERS", "localhost:9092")`). This is the only
  Python change in Phase 3; no daemon or worker logic was touched.
- Added Dockerfile (python:3.12-slim, `WORKDIR /app`, requirements installed
  before the code copy so edits do not bust the pip layer) and .dockerignore.
- Rewrote docker-compose.yml: 7 background services (kafka, orchestrator,
  router, product_worker, engineering_worker, landing_worker, artifact_logger)
  plus a `cli` one-shot behind `profiles: ["cli"]`.
- Added .env.example (LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, LLM_BASE_URL,
  STEP_TTL_SECONDS, STALL_TICK_SECONDS, ACTIVE_ROLES); `.env` added to
  .gitignore and left untracked.
- **Verified live in Docker**: 49/49 tests pass; all 7 services up with
  RestartCount 0; goal `G-1cc988fd` ("sync check") planned into 2 steps, both
  routed, worked and logged, `goal.completed` published.
- **Broker-resilience check PASSED**: `docker compose kill kafka`, 15s outage,
  `docker compose up -d kafka`. No app container restarted (RestartCount stayed
  0 on all six). Goal `G-6e76ea44` submitted after the outage completed end to
  end with no duplicate dispatches. Recovery is not instant — librdkafka takes
  a ~45s consumer-group session timeout and rebalance before goals flow again.

**P1 Part 1 (FastAPI skeleton + OpenAPI contract) — FINISHED 2026-09-22.**

- Added `app/` package: `main.py` (FastAPI `title="ImportAlpha Lite"`,
  `GET /health`), `api/v1/endpoints.py` (router `prefix="/v1"`) and
  `schemas.py` (Pydantic v2 request/response contracts).
- All four MVP endpoints are wired as stubs: `GET /v1/opportunities`,
  `POST /v1/landed-cost`, `POST /v1/reports` (202), `GET /v1/reports/{id}`.
- **Stubs return `status="stub"` with every estimate `null`**, not dummy
  numbers. AGENTS.md forbids invented data, and a placeholder that looks like
  a real estimate is exactly what that rule exists to stop. `SourceMetadata`
  (source_url / observed_at / confidence) is the enforcement point for when
  real datapoints arrive. `tests/test_api.py::test_no_stub_endpoint_emits_an_unsourced_number`
  asserts this directly so it cannot regress quietly.
- Added `fastapi` service to docker-compose.yml (uvicorn on 8000, published to
  the host) and a profile-gated `pytest` one-shot service. `tests/` removed
  from `.dockerignore` so the image can run them.
- Added `httpx2>=2.13.0` to requirements: starlette 1.6 requires an HTTP client
  for `TestClient` and prefers `httpx2` over the now-deprecated `httpx`.
- **Verified live**: 8 services up with RestartCount 0; `/health` returns
  `{"status":"ok"}`; all four v1 endpoints return schema-valid JSON; `/docs`
  serves HTTP 200. **65 tests pass** (49 existing + 16 new), both on the host
  and via `docker compose run --rm pytest`.
- The API process is independent of Kafka — it neither produces nor consumes
  events yet, so it serves with the broker down.


## In Progress

- P1 product core. Part 1 (skeleton) is done and awaiting review; the data
  layer, scoring engine and fixtures are not started. P1 Part 1 files are
  written but **not yet committed**.

## Next Actions

1. P1 Part 2: SQLite state store (tasks, events, artifacts) + report persistence,
   so `GET /v1/reports/{id}` can 404 on an unknown id instead of echoing it back
2. P1 Part 3: product opportunity scoring engine + curated home-organization
   fixture dataset (20 products), each datapoint carrying source_url /
   observed_at / confidence
3. Wire `POST /v1/reports` to the Kafka lane so a report request becomes a goal
4. Consider worker heartbeats — the step TTL is wall-clock, so a legitimately slow step is indistinguishable from a dead worker (see docs/DECISIONS.md)
5. Optional: a second host-facing Kafka listener, so `python scripts/submit_goal.py` works from the host again (see DECISIONS.md)

## Blocked

None.

## Decisions Log

Moved to [DECISIONS.md](DECISIONS.md) — kept in one place so it does not drift.
