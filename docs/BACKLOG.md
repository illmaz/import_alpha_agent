# Backlog

## P0 — Foundation (do first)

- [ ] Create Python project structure
- [ ] Add docker-compose.yml for single-node Kafka (KRaft)
- [ ] Create events.py (Pydantic Event model)
- [ ] Create bus.py (Kafka produce/consume helpers, localhost:9092)
- [ ] Create orchestrator.py (publishes task.created events)
- [ ] Create router_worker.py (routes to task.assigned.{role})
- [ ] Create product_worker.py (emits artifact.created)
- [ ] Create artifact_logger.py (records events)
- [ ] Add tests for Event schema
- [ ] Run full local event flow end to end

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