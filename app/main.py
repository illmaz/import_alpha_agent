"""FastAPI application entrypoint for ImportAlpha Lite.

Runs alongside the Kafka agent lane but is independent of it: the API process
does not produce or consume events yet, so it starts and serves even with the
broker down. It does require its SQLite file, which is created on startup.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1.endpoints import router as v1_router
from app.api.v1.public_endpoints import router as public_router
from app.api.v1.webhooks import router as webhooks_router
from app.billing_pages import router as billing_pages_router
from app.services.x402_middleware import X402PaymentMiddleware
from app.database import get_engine, init_models
from app.schemas import HealthResponse

logger = logging.getLogger("app")

APP_VERSION = "0.1.0"

# Process start, for the uptime figure in /health.
_STARTED_AT = time.monotonic()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create the schema before the first request is served.

    `create_all` only ever adds missing tables — it will not alter an existing
    one. That is fine while the schema is append-only and nothing is deployed;
    the first column change to a table holding real rows needs Alembic. See
    docs/DECISIONS.md.
    """
    await init_models()
    yield


app = FastAPI(
    title="ImportAlpha Lite",
    version=APP_VERSION,
    description=(
        "China-to-US product viability and landed-cost intelligence for "
        "e-commerce agents. All v1 responses are currently "
        'status="curated": plausible hand-written values, never observed.'
    ),
    lifespan=lifespan,
)


# Runs before routing, so an unauthenticated request carrying a verified
# X-Payment-Hash reaches the endpoints as a paying account. An Authorization
# header always wins; see X402PaymentMiddleware.
app.add_middleware(X402PaymentMiddleware)


# HEAD as well as GET: uptime monitors and load balancers routinely probe with
# HEAD, and the StaticFiles mount at "/" would otherwise answer 404 for it —
# the catch-all claims any request the API routes decline on method, turning
# what should be a 405 into a missing-page answer from a health check.
@app.api_route("/health", methods=["GET", "HEAD"], response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    """Liveness plus a shallow readiness check.

    The database is probed with a trivial query because an unreachable SQLite
    file is the failure most likely to be silent — the API would keep serving
    the landing page while every report request failed. Kafka is deliberately
    not probed: the API neither produces nor consumes, so a broker outage does
    not make this process unhealthy, and dialling a broker on every probe
    would make the check slow and flaky.
    """
    services = {"api": "ok", "database": await _database_state()}
    degraded = [name for name, state in services.items() if state != "ok"]

    return HealthResponse(
        status="degraded" if degraded else "ok",
        version=APP_VERSION,
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 3),
        services=services,
    )


async def _database_state() -> str:
    """"ok", or a short reason. Never raises: a probe must not 500."""
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        logger.warning("health: database unreachable: %s", exc)
        return f"unavailable: {type(exc).__name__}"
    return "ok"


app.include_router(v1_router)
# Separate router: signature-verified, not API-key authenticated.
app.include_router(webhooks_router)
# Browser landing pages for Stripe redirects; unauthenticated by necessity.
app.include_router(billing_pages_router)
# Public marketing surface: sample reports and the price table, no API key.
app.include_router(public_router)


LANDING_DIR = Path(__file__).resolve().parents[1] / "landing"

# Mounted last, and this ordering is load-bearing: a mount at "/" matches every
# path that no earlier route claimed, so registering it above would shadow the
# entire API. html=True serves index.html for "/".
app.mount("/", StaticFiles(directory=LANDING_DIR, html=True), name="landing")
