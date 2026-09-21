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
- [ ] Add `engineering` and `landing` workers — plans touching those roles currently stall

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