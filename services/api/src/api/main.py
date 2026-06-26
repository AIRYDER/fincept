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

import asyncio
import os
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from api.background import AlpacaScheduler, NewsScheduler
from api.approved_roots import register_approved_roots_handler
from api.settlements_poller import (
    _poll_settlements_worker,
    _settlements_worker_interval_seconds,
)
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
from api.routes import quant_foundry as quant_foundry_route
from api.routes import quant_foundry_alpha as quant_foundry_alpha_route
from api.routes import modules as modules_route
from api.ws import router as ws_router
from fincept_core.heartbeat import beat_periodically
from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.logging import configure_logging, get_logger
from fincept_core.tracing import configure_tracing
from quant_foundry.gateway import QuantFoundryGateway

API_VERSION = "0.1.0"

log = get_logger(__name__)

# Fail closed on dev JWT secret in non-dev envs (audit R4/P3).
assert_safe_for_runtime()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the shared Redis client + tracing at startup."""
    configure_logging()
    configure_tracing("api")
    settings = get_settings()
    quant_foundry_gateway = configure_quant_foundry_gateway(app)
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
    quant_foundry_poll_task: asyncio.Task[None] | None = None
    quant_foundry_tournament_task: asyncio.Task[None] | None = None
    # --- Settlement wiring (Agent A) ---
    quant_foundry_settlement_task: asyncio.Task[None] | None = None
    # --- Shadow dispatch wiring (Agent C) ---
    quant_foundry_shadow_dispatch_task: asyncio.Task[None] | None = None
    # --- New settlements worker (fincept_core.datasets spine) ---
    settlements_worker_task: asyncio.Task[None] | None = None
    poll_interval = _quant_foundry_poll_interval_seconds()
    if (
        quant_foundry_gateway.enabled
        and quant_foundry_gateway.mode in {"runpod", "runpod_research", "runpod_shadow"}
        and poll_interval > 0
    ):
        quant_foundry_poll_task = asyncio.create_task(
            _poll_quant_foundry_runpod(quant_foundry_gateway, poll_interval)
        )
        app.state.quant_foundry_poll_task = quant_foundry_poll_task
    tournament_interval = _quant_foundry_tournament_interval_seconds()
    if quant_foundry_gateway.enabled and tournament_interval > 0:
        quant_foundry_tournament_task = asyncio.create_task(
            _poll_quant_foundry_tournament(quant_foundry_gateway, tournament_interval)
        )
        app.state.quant_foundry_tournament_task = quant_foundry_tournament_task
    # --- Settlement wiring (Agent A) ---
    settlement_interval = _quant_foundry_settlement_interval_seconds()
    if quant_foundry_gateway.enabled and settlement_interval > 0:
        quant_foundry_settlement_task = asyncio.create_task(
            _poll_quant_foundry_settlement(quant_foundry_gateway, settlement_interval)
        )
        app.state.quant_foundry_settlement_task = quant_foundry_settlement_task
    # --- Shadow dispatch wiring (Agent C) ---
    shadow_dispatch_interval = _quant_foundry_shadow_dispatch_interval_seconds()
    if quant_foundry_gateway.enabled and shadow_dispatch_interval > 0:
        quant_foundry_shadow_dispatch_task = asyncio.create_task(
            _poll_quant_foundry_shadow_dispatch(
                quant_foundry_gateway, shadow_dispatch_interval
            )
        )
        app.state.quant_foundry_shadow_dispatch_task = (
            quant_foundry_shadow_dispatch_task
        )
    # --- New settlements worker (fincept_core.datasets spine) ---
    # Coexists with the quant_foundry settlement sweep above: the new
    # worker writes to fincept_core.datasets.SettlementStore (keyed by
    # agent_id, cost model v1.default) while the old sweep writes to
    # quant_foundry.settlement.SettlementLedger (keyed by model_id,
    # cost model cm-v1).  See api.settlements_poller for the full
    # reconciliation strategy.  Runs regardless of gateway mode so the
    # /models/{name}/outcomes route is fed even when quant_foundry is
    # disabled; set SETTLEMENTS_WORKER_POLL_S=0 to disable.
    settlements_worker_interval = _settlements_worker_interval_seconds()
    if settlements_worker_interval > 0:
        settlements_worker_task = asyncio.create_task(
            _poll_settlements_worker(settlements_worker_interval)
        )
        app.state.settlements_worker_task = settlements_worker_task
    log.info("api.start", version=API_VERSION, redis_url=settings.REDIS_URL)
    try:
        yield
    finally:
        log.info("api.stop")
        # --- Shadow dispatch wiring (Agent C) ---
        if quant_foundry_shadow_dispatch_task is not None:
            quant_foundry_shadow_dispatch_task.cancel()
            try:
                await quant_foundry_shadow_dispatch_task
            except asyncio.CancelledError:
                pass
        # --- New settlements worker (fincept_core.datasets spine) ---
        if settlements_worker_task is not None:
            settlements_worker_task.cancel()
            try:
                await settlements_worker_task
            except asyncio.CancelledError:
                pass
        # --- Settlement wiring (Agent A) ---
        if quant_foundry_settlement_task is not None:
            quant_foundry_settlement_task.cancel()
            try:
                await quant_foundry_settlement_task
            except asyncio.CancelledError:
                pass
        if quant_foundry_tournament_task is not None:
            quant_foundry_tournament_task.cancel()
            try:
                await quant_foundry_tournament_task
            except asyncio.CancelledError:
                pass
        if quant_foundry_poll_task is not None:
            quant_foundry_poll_task.cancel()
            try:
                await quant_foundry_poll_task
            except asyncio.CancelledError:
                pass
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await news_scheduler.stop()
        await scheduler.stop()
        await redis.aclose()  # type: ignore[attr-defined]


def configure_quant_foundry_gateway(
    app: FastAPI,
    *,
    base_dir: pathlib.Path | str | None = None,
) -> QuantFoundryGateway:
    gateway = QuantFoundryGateway.from_env(base_dir=base_dir)
    app.state.quant_foundry_gateway = gateway
    return gateway


def _quant_foundry_poll_interval_seconds() -> float:
    raw = os.environ.get("QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS", "15")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 15.0


async def _poll_quant_foundry_runpod(
    gateway: QuantFoundryGateway,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(gateway.poll_runpod_results)
        except Exception as exc:
            log.warning(
                "quant_foundry.runpod_poll_failed",
                error=f"{type(exc).__name__}: {exc}",
            )


def _quant_foundry_tournament_interval_seconds() -> float:
    raw = os.environ.get("QUANT_FOUNDRY_TOURNAMENT_INTERVAL_SECONDS", "300")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 300.0


async def _poll_quant_foundry_tournament(
    gateway: QuantFoundryGateway,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(gateway.run_tournament_sweep)
        except Exception as exc:
            log.warning(
                "quant_foundry.tournament_poll_failed",
                error=f"{type(exc).__name__}: {exc}",
            )


# --- Settlement wiring (Agent A) ---


def _quant_foundry_settlement_interval_seconds() -> float:
    raw = os.environ.get("QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS", "60")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


async def _poll_quant_foundry_settlement(
    gateway: QuantFoundryGateway,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(gateway.run_settlement_sweep)
        except Exception as exc:
            log.warning(
                "quant_foundry.settlement_poll_failed",
                error=f"{type(exc).__name__}: {exc}",
            )


# --- Shadow dispatch wiring (Agent C) ---


def _quant_foundry_shadow_dispatch_interval_seconds() -> float:
    raw = os.environ.get("QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS", "300")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 300.0


async def _poll_quant_foundry_shadow_dispatch(
    gateway: QuantFoundryGateway,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(gateway.dispatch_shadow_inference_batch)
        except Exception as exc:
            log.warning(
                "quant_foundry.shadow_dispatch_poll_failed",
                error=f"{type(exc).__name__}: {exc}",
            )


app = FastAPI(title="Fincept API", version=API_VERSION, lifespan=lifespan)
# Shared approved-roots violation handler -> uniform 422 body
# {"detail": ..., "code": "approved_roots_violation"} for every route
# that gates a user-supplied path through ApprovedRoots.resolve(...).
register_approved_roots_handler(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
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
app.include_router(news.router, prefix="/news", tags=["news"])
app.include_router(news_impact.router, prefix="/news-impact", tags=["news-impact"])
app.include_router(services_route.router, prefix="/services", tags=["services"])
app.include_router(models_route.router, prefix="/models", tags=["models"])
app.include_router(regime_route.router, prefix="/regime", tags=["regime"])
app.include_router(backtest_route.router, prefix="/backtest", tags=["backtest"])
app.include_router(research.router, prefix="/research", tags=["research"])
# Health/readiness (MINIMAL additive only per task scope; /health public liveness stays in this file)
app.include_router(health_route.router, prefix="/health", tags=["health"])
# Control endpoints (auth-required, write).
app.include_router(control.router, prefix="", tags=["control"])
# Quant Foundry gateway (TASK-0306). Disabled by default; operator endpoints
# bearer-auth, callback endpoint HMAC-auth. No bus / sig.predict writes.
app.include_router(
    quant_foundry_route.router, prefix="/quant-foundry", tags=["quant-foundry"]
)
# Alpha Genome Lab (TASK-1005) — recipe sweep surface, mounted under the same
# gateway prefix so the operator URL is consistent. Bearer-auth; opt-in; no
# bypass of tournament / promotion gates.
app.include_router(
    quant_foundry_alpha_route.router,
    prefix="/quant-foundry/alpha",
    tags=["quant-foundry-alpha"],
)
# On-demand module control (TASK-0203). Auth-required, local-only launches.
app.include_router(modules_route.router, prefix="/modules", tags=["modules"])
# WebSocket multiplexer.
app.include_router(ws_router, prefix="/ws", tags=["ws"])
