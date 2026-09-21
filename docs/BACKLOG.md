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
- [ ] Run full local event flow end to end (requires Docker)

## Phase 3 — Dockerized Run Lane

- [x] Single Dockerfile (python:3.12-slim, install requirements.txt, copy app code)
- [x] Six services in docker-compose.yml (orchestrator, router, product_worker, engineering_worker, landing_worker, artifact_logger)
- [x] .env.example with LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, STEP_TTL_SECONDS, STALL_TICK_SECONDS, ACTIVE_ROLES
- [x] BOOTSTRAP_SERVERS configurable via env in bus.py
- [x] CLI script for goal submission without seventh window
- [x] Backlog re-prioritization documented

## P1 — Product core

- [ ] Add SQLite state store (tasks, events, artifacts)
- [ ] Add FastAPI skeleton
- [ ] Add OpenAPI contract for the 4 MVP endpoints
- [ ] Add product opportunity schema + scoring engine
- [ ] Add curated home-organization fixture dataset (20 products)
- [ ] Add landed-cost estimator endpoint
- [ ] Add report generation endpoint

## P2 — Monetization

- [ ] Add API key auth + prepaid credit ledger
- [ ] Add Stripe Checkout for credit purchases
- [ ] Add usage metering + 402-on-empty-balance
- [ ] Add x402 sandbox payment challenge (testnet only)

## P3 — Growth

- [ ] Landing page (copy + sample JSON + Stripe link)
- [ ] 3 sample reports in home organization
- [ ] Demo agent script that consumes the API
- [ ] Outreach list + draft messages (draft-only, human approves sends)

## P4 — Quality & safety

- [ ] Golden eval suite (10 queries, schema + score bounds)
- [ ] Security/compliance checklist
- [ ] Human approval gates for deploy / send / spend