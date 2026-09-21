# Project Context — ImportAlpha Lite

## What we are building

A paid product/sourcing intelligence API for e-commerce agents.

Agents (Amazon / TikTok Shop / Shopify seller bots, sourcing copilots, procurement agents) call our API to find, validate, price, and de-risk products sourced from China.

We are NOT an e-commerce store. We are NOT the agent. We are the data/API tollbooth other agents pay to use.

## Main wedge

China-to-US product viability + landed cost intelligence.

First category: **home organization** (drawer organizers, shelf bins, cable management, desk organizers, under-sink organizers).

Why home organization: low compliance risk, lightweight, broad demand, good margins, not electronics/supplements/IP-heavy.

## Business model

- $99 one-time product viability report pack
- $299/month API credits / pilot access
- $999 custom category feed

## MVP endpoints

- `GET /v1/opportunities`
- `POST /v1/landed-cost`
- `POST /v1/reports`
- `GET /v1/reports/{report_id}`

Each response includes: product opportunity score, estimated China unit cost range, estimated landed cost, estimated margin, competition signal, risk flags, confidence score, source metadata.

## Technical direction

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2
- SQLite now, Postgres later
- Kafka as the event bus for the agentic workflow
- Docker Compose for local infra
- Stripe prepaid credits first
- x402/USDC agent micropayments later (sandbox/testnet only)

## Agent system

Agentic workflow with human approval gates:

- Founder (human) — final approvals
- Orchestrator agent
- Product & Offer agent
- Architecture agent
- Data Engineer agent
- Backend/API agent
- Landing Page agent
- Marketing Content agent
- Sales/Outreach agent
- QA/Evals agent
- DevOps agent
- Security & Compliance agent

## Current phase

Build the Python + Kafka event bus skeleton first:

- orchestrator publishes `task.created`
- router routes to `task.assigned.{role}`
- worker agents emit `artifact.created`
- logger/state agent records events

LLM (Qwen/Claude/etc.) is plugged into specific agents LATER, after the event flow works.

## Out of scope (do not build yet)

Discord membership, full supplier verification, live scraping of many sites, crypto wallet custody, x402 mainnet, multi-niche scoring, fancy dashboards, automated purchasing, automated supplier outreach.