# Current State

## Phase

Phase 3: Dockerized run lane with Kafka event bus.

## Completed

- Defined wedge: ImportAlpha Lite (China-to-US product viability API, home organization first)
- Defined MVP offer and pricing ($99 / $299 / $999)
- Defined agent org structure
- Decided to start with Python + Kafka event bus (no LLM yet)
- Created project context docs
- Phase 2.5 shipped (5374037), 49 tests green, stall and escalation verified live
- **Phase 3: Dockerized run lane** — `docker compose up` replaces six manual terminal windows
  - Single Dockerfile (python:3.12-slim, requirements.txt, application code)
  - docker-compose.yml with six services (orchestrator, router, product_worker, engineering_worker, landing_worker, artifact_logger) + Kafka
  - .env.example with LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, STEP_TTL_SECONDS, STALL_TICK_SECONDS, ACTIVE_ROLES
  - BOOTSTRAP_SERVERS configurable via env in bus.py (default localhost:9092 for host dev; kafka:9092 inside compose)
  - CLI script for submitting goals: `docker compose run --rm cli python scripts/submit_goal.py "..."`
  - Backlog re-prioritization: step.started ack = P1, heartbeats = P2
  - BROKER-RESILIENCE CHECK documented in DECISIONS.md

## In Progress

None.

## Next Actions

Run acceptance tests and record outcomes.

## Blocked

None.

## Decisions Log

- 2026-09-21: Wedge = home organization category, US market.
- 2026-09-21: Sell report-first + API credits; x402 is phase 2.
- 2026-09-21: Build with Claude Code in normal VS Code first; Agent Sessions UI for parallel agents later.