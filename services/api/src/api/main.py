"""
api.main — FastAPI app construction.

Lifespan creates one Redis client at startup and closes it at shutdown;
all routes pull it via ``request.app.state.redis`` through the
``api.deps.get_redis`` dependency, so test code can override the slot
to inject a fakeredis instance without monkey-patching.

CORS is read from ``Settings.CORS_ALLOW_ORIGINS`` (CSV).  The default
preserves the localhost-only behaviour the dashboard relies on; the
operator can override per-environment.

A small middleware stamps every request with a correlation id
(``X-Request-ID``) which is logged on the response and exposed on
``request.state.request_id`` so route handlers can echo it in their own
logs.  This is the on-ramp for request-id propagation through outbound
HTTP (see audit R7 / P1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from redis.asyncio import Redis

from api.background import AlpacaScheduler, NewsScheduler
from api.routes import (
    backtest as backtest_route,
    control,
    data,
    health as health_route,
    models as models_route,
    news,
    news_impact,
    orders,
    positions,
    regime as regime_route,
    research,
    services as services_route,
    strategies,
)
from api.ws import router as ws_router
from fincept_core.heartbeat import beat_periodically
from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.ids import new_id
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
    # Fail closed on the dev JWT secret in non-dev envs.  The check is
    # idempotent; running it on every entrypoint keeps the safety
    # property even if a future change drops it from the lifespan.
    assert_safe_for_runtime(settings)
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    app.state.redis = redis
    scheduler = AlpacaScheduler(redis)
    scheduler.start()
    app.state.alpaca_scheduler = scheduler
    news_scheduler = NewsScheduler(redis)
    news_scheduler.start()
    app.state.news_scheduler = news_scheduler
    heartbeat_task = asyncio.create_task(beat_periodically(redis, "api"))
    app.state.heartbeat_task = heartbeat_task
    log.info("api.start", version=API_VERSION, redis_url=settings.REDIS_URL)
    try:
        yield
    finally:
        log.info("api.stop")
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await news_scheduler.stop()
        await scheduler.stop()
        await redis.aclose()  # type: ignore[attr-defined]


app = FastAPI(title="Fincept API", version=API_VERSION, lifespan=lifespan)

# CORS: read the allowlist from Settings so production deploys can pin
# the dashboard origin(s) without a code change.  Empty / unset values
# fall back to the legacy localhost list for backwards compatibility.
_cors_origins = [
    o.strip()
    for o in get_settings().CORS_ALLOW_ORIGINS.split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next) -> Response:
    """Stamp a correlation id on every request and echo it on the response.

    Reads ``X-Request-ID`` from the inbound headers when present (so a
    dashboard or upstream proxy can thread the id through); otherwise
    generates a fresh one.  The id is also stored on
    ``request.state.request_id`` so route handlers can include it in
    their own structured logs.
    """
    rid = request.headers.get("X-Request-ID") or new_id()
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.get("/health")
async def health() -> dict[str, Any]:
    """Public liveness check.  No auth so load balancers can probe."""
    return {"ok": True, "version": API_VERSION}


# Read endpoints (auth-required).
app.include_router(data.router, prefix="/data", tags=["data"])
app.include_router(positions.router, prefix="/positions", tags=["positions"])
app.include_router(orders.router, prefix="/orders", tags=["orders"])
app.include_router(strategies.router, prefix="/strategies", tags=["strategies"])
app.include_router(news.router, prefix="/news", tags=["news"])
app.include_router(news_impact.router, prefix="/news-impact", tags=["news-impact"])
app.include_router(services_route.router, prefix="/services", tags=["services"])
app.include_router(models_route.router, prefix="/models", tags=["models"])
app.include_router(regime_route.router, prefix="/regime", tags=["regime"])
app.include_router(backtest_route.router, prefix="/backtest", tags=["backtest"])
app.include_router(research.router, prefix="/research", tags=["research"])
# Health/readiness (additive; /health public liveness stays in this file)
app.include_router(health_route.router, prefix="/health", tags=["health"])
# Control endpoints (auth-required, write).
app.include_router(control.router, prefix="", tags=["control"])
# WebSocket multiplexer.
app.include_router(ws_router, prefix="/ws", tags=["ws"])
