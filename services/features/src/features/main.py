"""
features.main — entrypoint for the online feature runner.

Wires the standard async lifecycle:

  Redis -> Consumer(md.bars.1m) -> OnlineRunner.handle_event ->
  Producer.publish(features.online) -> Redis

Same shutdown pattern as ingestor.main / quality_main: SIGINT/SIGTERM
sets a stop event, the consumer task is cancelled, Redis closed.

Run with: ``python -m features.main``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis

from features.online import OnlineRunner
from features.store import OnlineStore
from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_BARS_1M
from fincept_core.config import get_settings
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.tracing import configure_tracing

log = get_logger(__name__)

CONSUMER_GROUP = "features.online"
CONSUMER_NAME = "features.online.1"


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    consumer = Consumer(redis)
    runner = OnlineRunner(producer, online_store=OnlineStore(redis))

    consume_task = asyncio.create_task(
        consumer.consume(
            streams=[STREAM_MD_BARS_1M],
            group=CONSUMER_GROUP,
            consumer_name=CONSUMER_NAME,
            handler=runner.handle_event,
        )
    )
    heartbeat_task = asyncio.create_task(beat_periodically(redis, "features"))

    try:
        await stop.wait()
    finally:
        for task in (heartbeat_task, consume_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await redis.aclose()  # type: ignore[attr-defined]


async def _main() -> None:
    configure_logging()
    configure_tracing("features.online")
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("features.online.start")
    try:
        await run(stop)
    finally:
        log.info("features.online.stop")


def main() -> None:
    """Synchronous CLI entrypoint: ``python -m features.main``."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
