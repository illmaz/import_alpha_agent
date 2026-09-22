"""FastAPI application entrypoint for ImportAlpha Lite.

Runs alongside the Kafka agent lane but is independent of it: the API process
does not produce or consume events yet, so it starts and serves even with the
broker down.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.v1.endpoints import router as v1_router
from app.schemas import HealthResponse

app = FastAPI(
    title="ImportAlpha Lite",
    version="0.1.0",
    description=(
        "China-to-US product viability and landed-cost intelligence for "
        "e-commerce agents. All v1 endpoints are skeleton stubs: they return "
        'status="stub" with no sourced datapoints attached.'
    ),
)


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    """Liveness probe. Reports only on this process, not on Kafka."""
    return HealthResponse(status="ok")


app.include_router(v1_router)
