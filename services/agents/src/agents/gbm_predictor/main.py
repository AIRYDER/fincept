"""
agents.gbm_predictor.main - long-running entrypoint.

  python -m agents.gbm_predictor.main

Reads ``GBM_MODEL_DIR`` from the environment (default
``models/gbm_predictor``), loads the trained Booster, and publishes
:class:`Prediction` events to ``STREAM_SIG_PREDICT`` at a fixed cadence.

Graceful shutdown on SIGINT / SIGTERM cancels the run loop so
``teardown`` runs and the Redis connection closes cleanly.

If the model artifacts are missing, the process exits non-zero with a
clear error.  Run the trainer first:

  python -m agents.gbm_predictor.train --input <bars.parquet>
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import signal
from typing import Any

from redis.asyncio import Redis

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_PREDICT
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import Prediction
from fincept_core.tracing import configure_tracing

from agents.gbm_predictor.infer import GBMPredictor

log = get_logger(__name__)

DEFAULT_MODEL_DIR = "models/gbm_predictor"


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    model_dir = pathlib.Path(os.getenv("GBM_MODEL_DIR", DEFAULT_MODEL_DIR))
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    agent = GBMPredictor(model_dir=model_dir, redis=redis)
    log.info("gbm.start", model_dir=str(model_dir), universe=list(settings.UNIVERSE))

    await agent.setup()
    publish_task = asyncio.create_task(_publish_loop(agent, producer))
    try:
        await stop.wait()
    finally:
        publish_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await publish_task
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
