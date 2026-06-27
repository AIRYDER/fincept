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
from api.task_manager import TaskManager
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


class RedisPredictionPublisher:
    """Sync publisher for paper-approved predictions to the ``sig.predict`` stream.

    The CallbackProcessor runs in a thread (via ``asyncio.to_thread``), so we
    need a sync Redis client — the async ``Producer`` can't be awaited from
    a sync context. This class creates a dedicated sync Redis connection and
    publishes ``Prediction`` events using the same serialization as the async
    Producer.
    """

    def __init__(self, redis: Redis[Any]) -> None:
        # Store the URL so we can create a sync client lazily.
        # The async Redis client is passed for URL extraction; the sync
        # client is created on first publish to avoid blocking the event
        # loop at construction time.
        self._redis_url = "redis://localhost:6379/0"
        # Extract URL from the async client's connection pool.
        try:
            pool = redis.connection_pool
            kw = pool.connection_kwargs
            host = kw.get("host", "localhost")
            port = kw.get("port", 6379)
            db = kw.get("db", 0)
            password = kw.get("password")
            if password:
                self._redis_url = f"redis://:{password}@{host}:{port}/{db}"
            else:
                self._redis_url = f"redis://{host}:{port}/{db}"
        except Exception:
            self._redis_url = "redis://localhost:6379/0"
        self._sync_redis: Any = None

    def _get_sync_redis(self) -> Any:
        if self._sync_redis is None:
            import redis as sync_redis_mod

            self._sync_redis = sync_redis_mod.Redis.from_url(self._redis_url)
        return self._sync_redis

    def publish_prediction(self, prediction: dict[str, Any]) -> str:
        """Publish a Prediction event to ``sig.predict``.

        Returns the Redis stream ID. Uses the same Event serialization as
        the async Producer so consumers see identical message format.
        """
        from fincept_bus.streams import STREAM_SIG_PREDICT, RETENTION
        from fincept_core.events import make_event, serialize
        from fincept_core.clock import now_ns
        from fincept_core.ids import new_id

        event = make_event("prediction", prediction)
        fields = serialize(event, event_id=new_id(), published_at=now_ns())
        client = self._get_sync_redis()
        message_id = client.xadd(
            STREAM_SIG_PREDICT,
            fields,
            maxlen=RETENTION.get(STREAM_SIG_PREDICT),
            approximate=True,
        )
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the shared Redis client + tracing at startup."""
    # Fail closed on dev JWT secret in non-dev envs (audit R4/P3).
    assert_safe_for_runtime()
    configure_logging()
    configure_tracing("api")
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    app.state.redis = redis
    # Create the gateway with the Redis-backed prediction publisher so
    # paper-approved models can publish to sig.predict.
    publisher = RedisPredictionPublisher(redis)
    quant_foundry_gateway = configure_quant_foundry_gateway(
        app, prediction_publisher=publisher
    )
    # --- TaskManager: uniform background task lifecycle ---
    tm = TaskManager()
    app.state.task_manager = tm
    # Schedulers (start/stop pattern).
    tm.add_scheduler("alpaca", AlpacaScheduler(redis))
    tm.add_scheduler("news", NewsScheduler(redis))
    tm.start_all()
    app.state.alpaca_scheduler = tm._schedulers["alpaca"]
    app.state.news_scheduler = tm._schedulers["news"]
    # Background poll tasks.
    tm.add_task("heartbeat", beat_periodically(redis, "api"))
    # Quant Foundry poll tasks (conditional on gateway config).
    poll_interval = _quant_foundry_poll_interval_seconds()
    if (
        quant_foundry_gateway.enabled
        and quant_foundry_gateway.mode in {"runpod", "runpod_research", "runpod_shadow"}
        and poll_interval > 0
    ):
        tm.add_task(
            "quant_foundry_poll",
            _poll_quant_foundry_runpod(quant_foundry_gateway, poll_interval),
        )
    tournament_interval = _quant_foundry_tournament_interval_seconds()
    if quant_foundry_gateway.enabled and tournament_interval > 0:
        tm.add_task(
            "quant_foundry_tournament",
            _poll_quant_foundry_tournament(quant_foundry_gateway, tournament_interval),
        )
    settlement_interval = _quant_foundry_settlement_interval_seconds()
    if quant_foundry_gateway.enabled and settlement_interval > 0:
        tm.add_task(
            "quant_foundry_settlement",
            _poll_quant_foundry_settlement(quant_foundry_gateway, settlement_interval),
        )
    shadow_dispatch_interval = _quant_foundry_shadow_dispatch_interval_seconds()
    if quant_foundry_gateway.enabled and shadow_dispatch_interval > 0:
        tm.add_task(
            "quant_foundry_shadow_dispatch",
            _poll_quant_foundry_shadow_dispatch(
                quant_foundry_gateway, shadow_dispatch_interval
            ),
        )
    # New settlements worker (fincept_core.datasets spine).
    # Runs regardless of gateway mode so the /models/{name}/outcomes route
    # is fed even when quant_foundry is disabled; set SETTLEMENTS_WORKER_POLL_S=0
    # to disable.
    settlements_worker_interval = _settlements_worker_interval_seconds()
    if settlements_worker_interval > 0:
        tm.add_task(
            "settlements_worker",
            _poll_settlements_worker(settlements_worker_interval),
        )
    log.info("api.start", version=API_VERSION, redis_url=settings.REDIS_URL)
    try:
        yield
    finally:
        log.info("api.stop")
        await tm.shutdown()
        await redis.aclose()  # type: ignore[attr-defined]


def configure_quant_foundry_gateway(
    app: FastAPI,
    *,
    base_dir: pathlib.Path | str | None = None,
    prediction_publisher: Any | None = None,
) -> QuantFoundryGateway:
    gateway = QuantFoundryGateway.from_env(base_dir=base_dir)
    # Inject the prediction publisher after construction (from_env doesn't
    # have access to the Redis client, so we inject it here).
    if prediction_publisher is not None and gateway._paper_bridge is not None:
        gateway._prediction_publisher = prediction_publisher
        # Re-construct the processor with the publisher wired in.
        from quant_foundry.callbacks import CallbackProcessor

        gateway.processor = CallbackProcessor(
            outbox=gateway.outbox,
            inbox=gateway.inbox,
            callback_secret=gateway.callback_secret,
            shadow_ledger=gateway.shadow_ledger,
            dossier_store=gateway.dossier_store,
            paper_bridge=gateway._paper_bridge,
            prediction_publisher=prediction_publisher,
            dossier_lookup=gateway._dossier_registry_lazy(),
        )
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
