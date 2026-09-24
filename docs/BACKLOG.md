# Backlog

## P0 — Foundation (do first)

- [x] Create Python project structure
- [x] Add docker-compose.yml for single-node Kafka (KRaft)
- [x] Create events.py (Pydantic Event model)
- [x] Create bus.py (Kafka produce/consume helpers, localhost:9092)
- [x] Create orchestrator.py (publishes task.created events)
- [x] Create router_worker.py (routes to task.assigned.{role})
- [x] Create product_worker.py (emits artifact.created)
- [x] Create artifact_logger.py (records events)
- [x] Add tests for Event schema
- [x] Run full local event flow end to end

## Phase 2 — Autonomous orchestration (done)

- [x] Create llm.py (LLM base, FakeLLM default, AnthropicLLM, OpenAICompatibleLLM)
- [x] Create GoalPlan model with roles restricted to known workers
- [x] Rewrite orchestrator.py as a `user.goals` daemon that plans via LLM
- [x] Track step completion via a threaded `artifact.created` consumer
- [x] Publish `goal.completed` with an LLM synthesis summary
- [x] Loop guards (MAX_STEPS_PER_GOAL, MAX_REPLANS) escalating to `human.approval.required`
- [x] Create scripts/submit_goal.py
- [x] Tests using FakeLLM and a mocked producer (no live broker)
- [x] Add `engineering` and `landing` workers — plans touching those roles currently stall

## Phase 2.5 — Liveness (done)

- [x] Add engineering_worker.py and landing_worker.py (honest stub pattern)
- [x] Add ACTIVE_ROLES registry, enforced in GoalPlan validation
- [x] Add stall detection (per-step TTL + ticker thread)
- [x] First stall replans once; second stall escalates and halts that goal
- [x] Record the liveness invariant in DECISIONS.md
- [x] Tests with an injectable fake clock (no sleeping in tests)
- [ ] Worker heartbeats — the TTL cannot tell a slow step from a dead worker

## Phase 3 — Dockerized run lane (done)

- [x] Make `bus.BOOTSTRAP_SERVERS` env-configurable
- [x] Add Dockerfile (python:3.12-slim) + .dockerignore
- [x] Rewrite docker-compose.yml: 7 background services + profile-gated `cli`
- [x] Add .env.example; keep `.env` untracked
- [x] Acceptance: 49 tests, clean `up --build -d`, goal end to end in Docker
- [x] Acceptance: broker-resilience check (kill kafka, 15s, restart, new goal)
- [ ] Optional: second host-facing listener so host-side submit_goal.py works

## P3.0 — Workers with brains and hands (done)

- [x] Role-specific system prompts for landing / product / engineering workers
- [x] Sandboxed file tools confined to `data/work/<goal_id>/` (no network, no shell)
- [x] Step contract: deliverable paths parsed from step text, artifacts carry sha256
- [x] Approval gate: manifest + `scripts/approve.py` list / show / approve / reject
- [x] Dogfood: the lane rewrites the landing page over v0
- [ ] Multi-file deliverables — a step writes exactly one file today
- [ ] Let a worker read the current target before rewriting it, so the lane can
      revise a page instead of replacing it wholesale
- [ ] Give a worker the response shapes of endpoints it is writing a client for.
      The live run invented `data.forEach` over objects and field names that do
      not exist; it was writing against our own API blind (see DECISIONS.md)
- [ ] A smoke check before a manifest is offered: load the page headless and
      confirm it renders without console errors. The gate caught the broken
      live page by human reading, which will not scale

## P3.7 — Agent discovery (done)

- [x] `public/llms.txt` served at `/llms.txt` as text/plain
- [x] `public/agent-guide.md` served at `/agent-guide` as text/markdown
- [x] `/openapi.json` confirmed public and valid (generated, not checked in)
- [x] `app/api/v1/agent_endpoints.py` — `/v1/agent/info`, public
- [x] "For Agents" section on the landing page, drafted by the lane
- [x] Every discovery route answers GET and HEAD without credentials
- [ ] **Decide the x402 price.** Discovery publishes $0.01/credit against
      $2.99-$9.90 on the card tiers — a 300-990x gap any agent can now find
- [ ] Stop the catch-all StaticFiles mount swallowing method mismatches, so a
      wrong-method request is a 405 rather than a 404. Three per-route HEAD
      patches so far (`/health`, the two discovery files, `/v1/agent/info`)
- [ ] Let an agent obtain an API key without a human — the guide currently has
      to say "contact the operator", which is the one manual step left

## P1 — Product core

- [x] Add FastAPI skeleton (`app/main.py`, `/health`)
- [x] Add OpenAPI contract for the 4 MVP endpoints (stubs, `status="stub"`)
- [x] Add Pydantic request/response schemas (`app/schemas.py`)
- [x] Add `fastapi` + profile-gated `pytest` services to docker-compose.yml
- [x] Add tests/test_api.py (rewritten for real data, 27 tests)
- [x] Add product opportunity scoring engine (`app/services/scoring.py`)
- [x] Add curated home-organization fixture dataset (20 products)
- [x] Add landed-cost estimator (`app/services/landed_cost.py`)
- [x] Add report generation endpoint (in-memory store)
- [x] GET /v1/reports/{id} 404s on an unknown id
- [x] Tests: scoring, landed cost, data loader, API (141 total)
- [x] Add SQLite persistence for reports (`app/database.py`, `app/models.py`)
- [x] Async SQLAlchemy + aiosqlite, WAL mode, busy timeout
- [x] Persist reports across container restarts (`scripts/verify_persistence.sh`)
- [x] Tests against a temporary SQLite file (159 total)
- [ ] Extend the state store to tasks, events and artifacts (only reports done)
- [ ] Replace curated fixture with sourced data (`status="curated"` -> `"ok"`)
- [ ] Revisit margin saturation — currently flat across all 20 products
- [x] Wire POST /v1/reports to the Kafka lane (`app/services/kafka_publisher.py`)
- [x] Add report_listener daemon settling rows from `goal.completed`
- [x] Tests: listener + publisher, broker never touched (186 total)
- [x] Reap stale pending reports (`scripts/report_reaper.py`, 10th service)
- [x] Prepare the first live LLM run (`scripts/live_demo.sh`, .env.example)
- [ ] **Run** the first live LLM run (needs a real key + approval to spend)
- [ ] Move the SQLite file off the bind mount (named volume, then Postgres)

## P2 — Monetization

### P2.1 — Auth and credits (done)

- [x] Add API key auth (SHA-256 hashed keys, Bearer header, 401)
- [x] Add prepaid credit ledger (integer balances, atomic deduction)
- [x] 402-on-empty-balance for POST /v1/reports
- [x] Add scripts/manage_accounts.py (create/issue/top-up/revoke)

### P2.1.5 — Migrations and refunds (done)

- [x] Add Alembic migrations (baseline + account_id on reports)
- [x] Add account_id to the report row
- [x] Refund credits when the reaper or the listener fails a report
- [x] Make refunds idempotent across both daemons (settle_if_pending)

### P2.2 — Append-only ledger (done)

- [x] Add CreditTransaction (signed delta, reason, reference, balance_after)
- [x] Migration with an opening-balance backfill for existing accounts
- [x] Single mutation path: `_apply()` is the only writer of a balance
- [x] reconcile(account_id): cached balance vs SUM(delta)
- [x] Universal refund — keyed on status=failed, never on the reason
- [x] `manage_accounts.py history` / `reconcile`
- [x] GET /v1/account/transactions (own account only, paginated)
- [ ] Enforce append-only in the database (needs Postgres grants; SQLite
      cannot, so it is currently a service-layer discipline)

### P2.3 — Stripe, test mode (code complete)

- [x] PRICE_TABLE (report_pack $99/10, api_credits $299/100)
- [x] POST /v1/billing/checkout + GET /v1/billing/packs
- [x] POST /v1/webhooks/stripe, signature-verified, unauthenticated
- [x] Credits re-derived server-side; amount checked against the pack price
- [x] Idempotency via a partial unique index on purchase references
- [x] Live `sk_live_` keys refused at the call site
- [x] Fix webhook crash: Stripe v12 StripeObject is not a dict
- [x] Real /billing/success and /billing/cancel pages (was example.com)
- [x] Test fixtures use real stripe.Event objects, not dicts
- [ ] **Complete the card payment** with 4242 4242 4242 4242 (needs a browser;
      everything else in the flow is verified)
- [ ] Recurring billing — CONTEXT.md calls api_credits "$299/month"; it is
      currently a one-time pack

### P2.4 — x402 agent payments (code complete, testnet)

- [x] verify_payment against Base Sepolia (ERC-20 Transfer logs, not tx.value)
- [x] Confirmation depth check; mainnet refused at the call site
- [x] Middleware: X-Payment-Hash -> verified payment -> access
- [x] Per-payer accounts; re-used hash refused with 402
- [x] docs/X402_SETUP.md (wallet, faucets, curl, mainnet checklist)
- [ ] **One real Base Sepolia USDC payment** — the log decoder is only
      covered by fixtures until then
- [ ] Decide the x402 credit price ($0.01 vs $2.99-$9.90 via Stripe)
- [ ] Upgrade to EIP-3009 signed authorisations (a tx hash is public and
      proves payment, not identity)

### P2.5 — Metering

- [ ] Add usage metering (per-account request history, not just spend)

## P3 — Growth

- [x] Landing page (copy + sample JSON + pricing, served at `/`)
- [x] 3 sample reports (home organization, kitchen gadgets, pet accessories)
- [ ] Wire the pricing CTAs to real Stripe Checkout — they are mailto links
      today, because self-serve checkout needs an account before it has a key
- [ ] Demo agent script that consumes the API
- [ ] Outreach list + draft messages (draft-only, human approves sends)

## P4 — Quality & safety

- [ ] Golden eval suite (10 queries, schema + score bounds)
- [ ] Security/compliance checklist
- [ ] Human approval gates for deploy / send / spend