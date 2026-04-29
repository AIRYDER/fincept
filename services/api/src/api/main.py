"""
api.main — FastAPI app construction.

Lifespan creates one Redis client at startup and closes it at shutdown;
all routes pull it via ``request.app.state.redis`` through the
``api.deps.get_redis`` dependency, so test code can override the slot
to inject a fakeredis instance without monkey-patching.

CORS is open in dev (``http://localhost:3000``) for the Next.js
dashboard; production deploys should override via env config (a future
task — for v1 the API only runs behind localhost or a reverse proxy).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from api.routes import control, data, orders, positions, strategies
from api.ws import router as ws_router
from fincept_core.config import get_settings
from fincept_core.logging import configure_logging, get_logger
from fincept_core.tracing import configure_tracing

API_VERSION = "0.1.0"

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the shared Redis client + tracing at startup."""
    configure_logging()
    configure_tracing("api")
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    app.state.redis = redis
    log.info("api.start", version=API_VERSION, redis_url=settings.REDIS_URL)
    try:
        yield
    finally:
        log.info("api.stop")
        await redis.aclose()  # type: ignore[attr-defined]


app = FastAPI(title="Fincept API", version=API_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Public liveness check.  No auth so load balancers can probe."""
    return {"ok": True, "version": API_VERSION}


# Read endpoints (auth-required).
app.include_router(data.router, prefix="/data", tags=["data"])
app.include_router(positions.router, prefix="/positions", tags=["positions"])
app.include_router(orders.router, prefix="/orders", tags=["orders"])
app.include_router(strategies.router, prefix="/strategies", tags=["strategies"])
# Control endpoints (auth-required, write).
app.include_router(control.router, prefix="", tags=["control"])
# WebSocket multiplexer.
app.include_router(ws_router, prefix="/ws", tags=["ws"])
