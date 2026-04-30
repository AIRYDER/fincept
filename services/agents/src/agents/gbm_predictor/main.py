"""
agents.gbm_predictor.main - long-running entrypoint.

  python -m agents.gbm_predictor.main

Resolves the model directory in this order:

  1. ``models/active/gbm_predictor.v1.json``  (operator promotion)
  2. ``$GBM_MODEL_DIR``                        (env override)
  3. ``models/gbm_predictor``                  (default)

then loads the trained Booster and publishes :class:`Prediction`
events to ``STREAM_SIG_PREDICT`` at a fixed cadence.

The active.json pointer is written by the api's ``POST /models/{name}/promote``
route.  Operators who prefer not to use the dashboard can ignore it
and keep using ``GBM_MODEL_DIR`` -- the resolver falls through cleanly
when the file is absent or malformed.

Graceful shutdown on SIGINT / SIGTERM cancels the run loop so
``teardown`` runs and the Redis connection closes cleanly.

If the model artifacts are missing, the process exits non-zero with a
clear error.  Run the trainer first:

  python -m agents.gbm_predictor.train --input <bars.parquet>
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import signal
from typing import Any

from redis.asyncio import Redis

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_PREDICT
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import Prediction
from fincept_core.tracing import configure_tracing

from agents.gbm_predictor.infer import GBMPredictor

log = get_logger(__name__)

AGENT_ID = "gbm_predictor.v1"
DEFAULT_MODEL_DIR = "models/gbm_predictor"


def _resolve_model_dir() -> pathlib.Path:
    """Pick the model directory the agent should load on startup.

    Three-tier resolution (see module docstring).  Duplicated here
    rather than imported from ``api.promotions`` because the agent
    process must not depend on the api package -- the api ships
    fastapi/pydantic and a long tail of HTTP-side baggage that the
    agent doesn't need.

    The function is intentionally fail-soft: a corrupted active.json
    is logged and skipped.  The agent would rather fall through to
    the env-var path than refuse to start, since a model-loading
    failure later will produce a clearer error in setup().
    """
    models_root = pathlib.Path(os.environ.get("MODELS_DIR", "models"))
    active_dir_override = os.environ.get("ACTIVE_MODELS_DIR")
    active_dir = (
        pathlib.Path(active_dir_override)
        if active_dir_override
        else models_root / "active"
    )
    pointer = active_dir / f"{AGENT_ID}.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text())
            name = data.get("model_name")
            if name and isinstance(name, str):
                resolved = models_root / name
                log.info("gbm.model.from_active", pointer=str(pointer), model=name)
                return resolved
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            # Don't fail the agent -- fall through to env / default.
            logging.getLogger(__name__).warning(
                "gbm.model.active_ignored: %s (%s)", pointer, exc
            )
    env_override = os.environ.get("GBM_MODEL_DIR")
    if env_override:
        return pathlib.Path(env_override)
    return models_root / "gbm_predictor"


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    model_dir = _resolve_model_dir()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    agent = GBMPredictor(model_dir=model_dir, redis=redis)
    log.info("gbm.start", model_dir=str(model_dir), universe=list(settings.UNIVERSE))

    await agent.setup()
    publish_task = asyncio.create_task(_publish_loop(agent, producer))
    heartbeat_task = asyncio.create_task(beat_periodically(redis, "gbm_predictor"))
    try:
        await stop.wait()
    finally:
        for task in (heartbeat_task, publish_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await agent.teardown()
        await redis.aclose()  # type: ignore[attr-defined]


async def _publish_loop(agent: GBMPredictor, producer: Producer) -> None:
    async for event_payload in agent.run():
        if not isinstance(event_payload, Prediction):
            continue
        await producer.publish(
            STREAM_SIG_PREDICT, Event(type="prediction", payload=event_payload)
        )
        log.info(
            "gbm.pred",
            symbol=event_payload.symbol,
            direction=event_payload.direction,
            confidence=event_payload.confidence,
        )


async def _main() -> None:
    configure_logging()
    configure_tracing("agents.gbm_predictor")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run(stop)
    finally:
        log.info("gbm.stop")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
