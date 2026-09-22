"""FastAPI application entrypoint for ImportAlpha Lite.

Runs alongside the Kafka agent lane but is independent of it: the API process
does not produce or consume events yet, so it starts and serves even with the
broker down. It does require its SQLite file, which is created on startup.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api.v1.endpoints import router as v1_router
from app.database import init_models
from app.schemas import HealthResponse


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
    version="0.1.0",
    description=(
        "China-to-US product viability and landed-cost intelligence for "
        "e-commerce agents. All v1 responses are currently "
        'status="curated": plausible hand-written values, never observed.'
    ),
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    """Liveness probe. Reports on this process only — not Kafka, not the database."""
    return HealthResponse(status="ok")


app.include_router(v1_router)
