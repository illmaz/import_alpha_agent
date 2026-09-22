# Current State

## Phase

P2 started — the API is no longer open. Every `/v1` route needs an API key,
and report generation is charged against a prepaid credit balance. The first
live LLM run is done; data is still curated, not sourced.

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


**P1 Part 2 (scoring + fixtures + landed cost) — FINISHED 2026-09-22.**

- Added `app/services/scoring.py`: `calculate_opportunity_score()` —
  margin 0.35 + trend 0.25 + competition_inverse 0.20 + risk_inverse 0.10 +
  confidence 0.10. Pure, deterministic, no I/O. Returns `(int, explanation)`
  where the explanation is a term-by-term breakdown. Margin saturates at 60%
  (`MARGIN_SATURATION_PCT`); out-of-range inputs raise rather than clamp.
- Added `app/services/landed_cost.py`: flat $0.50/kg freight + 6.5% ad valorem
  duty. `confidence="low"`, and the `assumptions` list travels with every
  number so an estimate cannot be quoted without its caveats.
- Added `data/fixtures/home_organization_products.json`: 20 products.
  **Labelled `data_class: "curated_synthetic"`** with `curated://` source URIs,
  not http(s) links. Fabricating a marketplace URL would be worse than
  fabricating a number, because it looks verifiable. Confidence is a uniform
  0.25 — varying it would imply differential evidence that does not exist.
- Added `estimated_retail_price_usd` to the fixture: margin has to be derived
  from something, and deriving it from a curated retail anchor is honest where
  hardcoding a margin would not be.
- Added `app/services/data_loader.py`: fixture -> landed cost -> margin ->
  score -> `ProductOpportunity`, sorted best first, `lru_cache`d on the default
  path. Adds `RiskFlag.LOW_DATA` to every curated record automatically.
- Added `app/services/report_store.py`: in-memory, lock-guarded. **Reports do
  not survive a restart and do not work across replicas** — run one replica
  until the SQLite store lands.
- Added `ResultStatus.CURATED` to schemas. It is not a lesser `OK`: it means
  the numbers were never observed anywhere.
- Wired all four endpoints to real services. `GET /v1/reports/{id}` now 404s
  on an unknown id, which it could not do while there was nothing to look up in.
- **141 tests pass** (was 65): +22 scoring, +19 landed cost, +24 data loader,
  and test_api.py rewritten to 27. Verified identically on host and via
  `docker compose run --rm pytest`.
- **Verified live**: 8 services up; `/v1/opportunities` returns 20 scored
  products; `/v1/landed-cost` returns $1365.50 for 500 x $2.40 @ 0.35kg;
  `/v1/reports` returns 202 + fetchable report; unknown id returns 404.


**P1 Part 3 (SQLite persistence for reports) — FINISHED 2026-09-22.**

- Added `app/database.py`: async SQLAlchemy engine over aiosqlite,
  `DATABASE_URL` (default `sqlite+aiosqlite:///./data/reports.db`),
  `AsyncSessionLocal`, `Base`, `init_models()`, `reset_engine()`. The engine is
  built lazily so tests can redirect the URL without controlling import order.
- Added `app/models.py`: `Report` (id PK, status, payload_json, created_at,
  updated_at) with `ix_reports_status`. The body is stored as JSON text — the
  report is a document, written once and read whole, never queried by its
  inner fields.
- Rewrote `app/services/report_store.py` on SQLite: `create_report()`,
  `update_report()`, `get_report()` plus `get_report_response()`,
  `list_reports()`, `count_reports()`, `clear_reports()`. WAL journal mode and
  a 5s busy timeout so a concurrent writer waits rather than erroring.
- `POST /v1/reports` and `GET /v1/reports/{id}` are now `async def`. POST
  inserts a `pending` row *before* generating, so a crash mid-generation leaves
  a visible pending report rather than nothing.
- `GET /v1/reports/{id}` now distinguishes three cases: 200 ready,
  **409 pending** (row exists, body not written yet), 404 unknown.
- Added `greenlet>=3.0.0` to requirements — SQLAlchemy's async bridge needs it
  and does not always pull it in on arm64 macOS, where it fails at first
  connect rather than at import.
- `app/main.py` now uses a lifespan handler calling `init_models()`;
  `data/` is created automatically if missing.
- docker-compose.yml: `./data:/app/data` bind mount on **every** app service
  (via the shared anchor), so a worker that later needs report access already
  has it. `data/*.db*` added to .gitignore and .dockerignore.
- Added `scripts/verify_persistence.sh` — the acceptance check the in-memory
  store would have failed while passing every unit test.
- **159 tests pass** (was 141): +15 report store, +3 API persistence, and
  tests/conftest.py redirects every test at a throwaway SQLite *file* (not
  `:memory:`, which is per-connection and would make persistence tests pass
  for the wrong reason). Verified on host and in-container.
- **Verified live**: report survived `docker compose restart fastapi`, and
  survived a full `docker compose down` + `up`. DB confirmed in WAL mode with
  `ix_reports_status` present.


**P1 Part 4 (API wired to the Kafka orchestrator) — FINISHED 2026-09-22.**

- Added `UserGoalPayload` to app/schemas.py (goal, report_id, category,
  max_products).
- Added `app/services/kafka_publisher.py`: builds the `user.goals` event and
  publishes via `asyncio.to_thread`, so `bus.produce`'s blocking `flush()`
  never stalls the FastAPI event loop.
- **Correlation rides on `task_id`, not the payload.** The orchestrator builds
  a fresh payload for `goal.completed` and does not echo the one it received,
  so a report_id in the payload alone would vanish. It does adopt
  `event.task_id` as its `goal_id` and echoes that on both outcome topics.
  Needed no orchestrator change. See DECISIONS.md.
- Added `scripts/report_listener.py`: consumes `goal.completed` ->
  `status=ready` with the lane's synthesis, and `human.approval.required` ->
  `status=failed` with the reason. Bad events are logged and dropped so one
  cannot stop every later report from settling. CLI goals have no report row
  and are skipped.
- `POST /v1/reports` now stores the curated body as `pending`, publishes the
  goal, returns 202. A failed publish marks the row `failed` and returns 503
  rather than leaving it pending forever.
- `GET /v1/reports/{id}`: 404 unknown, 409 pending, 200 once settled.
- Added `report_listener` service to docker-compose.yml — **9 services**.
- **Switched SQLite journal mode from WAL to DELETE.** WAL broke as soon as a
  second process opened the bind-mounted database: every INSERT failed with
  `disk I/O error`. `SQLITE_JOURNAL_MODE` overrides it. See DECISIONS.md.
- **186 tests pass** (was 159): +16 listener, +9 publisher, +2 API. The suite
  never touches a broker — `publish_goal` is monkeypatched.
- **Verified live end to end**: POST -> 202 pending -> orchestrator planned and
  dispatched 2 steps -> workers produced artifacts -> `goal.completed` ->
  listener wrote `ready` -> GET returned 200 with the lane's synthesis. Three
  concurrent reports all settled correctly; DB rows confirmed with distinct
  created/updated timestamps.


**P1 Part 4.5 (report reaper + live-run prep) — FINISHED 2026-09-22.**

- Added `scripts/report_reaper.py`: sweeps every `REAP_INTERVAL_SECONDS`
  (60) and fails any report pending longer than `REPORT_STALE_AFTER_SECONDS`
  (300). **The liveness invariant now covers reports**, not just goals — a
  report always ends ready, failed-by-listener, or failed-by-reaper.
- Added `report_store.list_stale_pending()`, the query `ix_reports_status`
  was created for.
- The reaper **merges** the failure into the stored body rather than
  replacing it with `{"error": ...}`. A bare error payload would fail
  `ReportResponse` validation on read and turn `GET /v1/reports/{id}` into a
  500. See DECISIONS.md.
- Added `report_reaper` to docker-compose.yml — **10 services**.
- Added `tests/test_report_reaper.py` (15 tests) using artificially old
  timestamps, no sleeping. **201 tests pass.**
- Rewrote `.env.example` with current model ids (`claude-opus-5`,
  `claude-sonnet-5`, `claude-haiku-4-5`) and the reaper knobs. The ids
  suggested in the task brief were stale.
- Added `scripts/live_demo.sh`: validates provider config, **asks before
  spending** (AGENTS.md), writes `.env`, recreates the orchestrator, submits a
  report, tails the orchestrator while it plans, polls to completion and
  prints the report.
- **Verified live**: a 30-minute-old pending row was reaped within one sweep
  (`R-staletest: FAILED (agent_lane_timeout, pending 1850s)`) and still read
  back as HTTP 200 with `report_status=failed`. A concurrently submitted
  healthy report settled to `ready` untouched.

### Ready for first live LLM run

Everything so far has run on `FakeLLM` — canned plans, no network, no spend.
The stack is now ready for a real provider. Nothing has been run against one
yet, and no key exists in this checkout.

    export LLM_PROVIDER=anthropic
    export LLM_API_KEY=sk-ant-...
    export LLM_MODEL=claude-haiku-4-5     # cheapest; omit for claude-opus-5
    ./scripts/live_demo.sh

One goal is two model calls (one plan, one synthesis) — cents, not free. The
script confirms before spending and prints how to revert to FakeLLM.


**P2.1 (API key auth + prepaid credit ledger) — FINISHED 2026-09-22.**

- Added `ApiKey` and `CreditAccount` to app/models.py. Keys are stored as
  SHA-256 hashes only; plaintext is shown once at issuance and never
  persisted.
- Added `app/services/auth.py`: `generate_api_key()` (32 bytes from
  `secrets.token_urlsafe`, `ia_` prefix), `hash_api_key()`,
  `verify_api_key()`, plus issue/revoke/list.
  **SHA-256, not bcrypt** — these are 256-bit machine-generated secrets, not
  passwords; bcrypt would cost ~100ms per request and buy nothing. See
  DECISIONS.md.
- Added `app/services/billing.py`: integer balances, 1 credit = 1 report.
  **Deduction is a single conditional UPDATE** (`WHERE balance >= :n`), so two
  concurrent requests cannot both spend the last credit. A test proves it.
- Added `get_current_account` dependency, declared **on the router** so new
  endpoints are authenticated by default. All four `/v1` routes require
  `Authorization: Bearer <key>`; `/health` stays public.
- `POST /v1/reports` deducts 1 credit before publishing the goal, answers
  **402** with the current balance when short, and **refunds** if publishing
  then fails.
- Added `scripts/manage_accounts.py`: create-account, issue-key, add-credits,
  balance, list-accounts, list-keys, revoke-key.
- Added tests/test_auth.py (25) and tests/test_billing.py (23); test_api.py
  grew to 53 with 401/402 coverage. conftest now wipes accounts and keys too.
  **270 tests pass**, host and in-container.
- **Verified live**: no key -> 401; account created with 2 credits; two
  reports -> 202 and balance 2 -> 1 -> 0; third -> 402 with
  `"a report costs 1 and your balance is 0"`; top-up -> 5, spend -> 4. A paid
  report settled through the agent lane to `ready`.

**Known gap:** a report failed by the *reaper* is not refunded — the customer
paid and got nothing. Reports carry no `account_id`, so the reaper cannot tell
whom to credit. On the backlog as the first P2.2 item.


**Operational hardening (shutdown, secrets, LLM toggle) — FINISHED 2026-09-22.**

- **Graceful shutdown.** Every daemon ran as PID 1, and the kernel does not
  apply default signal dispositions to PID 1 — so SIGTERM was *ignored*,
  Docker's grace period expired, and SIGKILL gave exit 137 with offsets never
  committed. Confirmed from `/proc/1/status` (`SigCgt` had SIGINT but not
  SIGTERM) before fixing.
  Added `bus.install_signal_handlers()`, `request_shutdown()`,
  `wait_for_shutdown()`; `bus.consume()` now loops on a shutdown Event and
  always reaches `consumer.close()`. Wired into orchestrator, router, all
  three workers, artifact_logger, report_listener and report_reaper.
  **All nine containers now exit 0 on `docker compose stop`** (was 137).
- **Secret hygiene.** .gitignore now denies by pattern, not filename:
  `.env`, `.env.*` (with `!.env.example`), `*.backup*`, `*.log`, `*.dump`,
  `*.sql`, `*.sqlite*`. Verified the negation still leaves `.env.example`
  tracked.
- **Added `scripts/toggle_llm.sh`** (`fake` | `real` | `status`). Reads the
  key with `read -rs` so it never enters scrollback or shell history, and
  `status` prints only a character count. Switched the stack to
  `LLM_PROVIDER=fake` — **unintended spend has stopped**.
- **270 tests still pass.**

**On the reported git leak:** no leak was found. Working tree was clean and in
sync with origin; nothing staged, nothing tracked, and zero matches for
`sk-proj-`/`sk-ant-` anywhere in git history. The only real key on disk is in
`.env`, which is correctly ignored. The one scanner hit was a `.pyc` in a
gitignored `__pycache__`, containing the literal `"sk-ant-something"` from a
test fixture. The earlier `.env.backup` incident was already resolved and
never pushed. The hardening above was applied anyway.


**P2.1.5 (Alembic + refunds on failure) — FINISHED 2026-09-22.**

- Added Alembic (`alembic>=1.13.0`, `alembic.ini`, `alembic/env.py`,
  `alembic/versions/`). `env.py` takes the URL and metadata from
  `app.database`, so alembic.ini holds **no** URL and cannot drift;
  `render_as_batch=True` because SQLite has no real ALTER COLUMN.
- Two revisions: `f2c00b0a0a6e` (initial schema) and `456aca293990`
  (add account_id to reports). The baseline exists so a fresh database builds
  from migrations alone.
- **The autogenerated migration would have failed** — `nullable=False` with no
  default against 11 existing rows. Rewritten to add nullable, backfill with
  the `legacy-unknown` sentinel, then constrain and index.
- **Live database migrated**: stamped at the baseline, upgraded to head,
  11 rows preserved and backfilled.
- Added `Report.account_id` (NOT NULL, indexed); `POST /v1/reports` records
  the paying account, which is what makes a refund possible at all.
- `refund_credits` is now a **single atomic UPDATE** — it previously used
  read-modify-write, and with refunds arriving from two daemons that
  lost-update race was real.
- Added `report_store.settle_if_pending()`: a conditional pending -> failed
  transition that returns the owning account **only to the caller that won
  it**. That is the refund authorisation. Without it, at-least-once Kafka
  delivery or a reaper/listener overlap would refund the same report twice.
- `report_listener` and `report_reaper` both refund via
  `billing.refund_report()`, logging
  `[refund] R-xxx refunded 1 credit to account Y (reason)`.
- Added tests/test_refunds.py (17) and tests/test_migrations.py (8), including
  `test_migrations_match_the_models` — the guard against `create_all` and the
  migration chain drifting apart. **295 tests pass**, host and in-container.
- **Verified live**: happy path charged 2->1 and did *not* refund; a report
  whose lane was stopped was reaped and refunded 0->1
  (`[refund] R-9120c4fb refunded 1 credit to account test-customer-535e9c`);
  when the lane came back the listener logged
  `was already settled; skipping` and the balance did not move.


**P2.2 (append-only ledger + universal refund) — FINISHED 2026-09-22.**

- Added `CreditTransaction` (id, account_id, delta signed, reason, reference,
  balance_after, created_at) and `TransactionReason`
  (charge/refund/topup/purchase/adjustment).
- Migration `6c23c0d74c9b`. Autogenerated DDL was clean, but the file was
  edited before applying to add an **opening-balance backfill** — existing
  accounts had balances with no ledger rows, so `reconcile()` would have
  called every one of them corrupt on day one.
- **Live database migrated**: the one account's balance of 1 became a
  `pre-ledger opening balance` adjustment. Reconciles.
- Rewrote `app/services/billing.py` around `_apply()`, **the only writer of a
  balance**. It writes the ledger row and updates the cached balance in one
  transaction; `require_funds` keeps the atomic conditional UPDATE from P2.1.
  A failed spend writes no row.
- `reconcile(account_id)` compares cached balance against `SUM(delta)`.
  `reconcile_detail()` returns both numbers for reporting a mismatch.
- **Refunds now key on the status, never the reason.** P2.1's bug was
  reason-specific branches, so an unenumerated failure kept the customer's
  money. Whoever wins `settle_if_pending(..., failed)` refunds, full stop.
- Added `GET /v1/account/transactions` — authenticated, paginated, and taking
  **no account_id**, so reading another account is unrepresentable rather than
  a permission check that could be wrong.
- Added `manage_accounts.py history <account_id>` and `reconcile [account_id]`.
- Added tests/test_billing_ledger.py (23), tests/test_account_history.py (13),
  plus universal-refund and migration-backfill cases.
  **341 tests pass**, host and in-container.
- **Verified live**: topup, two charges and a timeout refund each produced
  exactly one ledger row; the account reconciles; the history endpoint returns
  the same story over HTTP and 401s without a key.

**Known limitation:** append-only is enforced by the service layer and a test
that reads its own source, not by the database. SQLite cannot revoke
UPDATE/DELETE here; Postgres can, and should.


## In Progress

- P2 monetization. P2.1, operational hardening, P2.1.5 and P2.2 (ledger)
  are done and awaiting review. Stripe (now P2.3), metering and x402 are not
  started.
- LLM provider is **fake** — no spend. `./scripts/toggle_llm.sh real` to switch.

## Next Actions

1. **P2.3: Stripe Checkout** for credit purchases. The ledger is ready for it —
   `record_purchase()` and `TransactionReason.PURCHASE` exist and are unused
2. Replace the curated fixture with sourced data — still the single change
   that moves responses from `status="curated"` to `status="ok"`
3. Move the database off the bind mount (named volume, then Postgres). Postgres
   also lets append-only be enforced by grants rather than by discipline
4. Persist the agent lane's tasks/events/artifacts — only reports are stored
5. Revisit margin weighting: every product clears the 60% saturation cap, so
   the margin term does no ranking work (see DECISIONS.md)
6. Consider worker heartbeats — the step TTL is wall-clock, so a legitimately slow step is indistinguishable from a dead worker (see docs/DECISIONS.md)

## Blocked

None.

## Decisions Log

Moved to [DECISIONS.md](DECISIONS.md) — kept in one place so it does not drift.
