"""
ingestor.quality_main — standalone entrypoint for the QualityMonitor.

Subscribes to ``md.trades`` and ``md.books`` via a Redis Streams consumer
group, dispatches every incoming ``Event`` to ``QualityMonitor.on_trade``
or ``QualityMonitor.on_book``, and runs ``staleness_check`` on a periodic
loop concurrently.

Out of scope (per TASK-014 §"Out of scope"):
  - Auto-reconnect / self-healing of broken adapters — that's TASK-010.
  - Alert-routing (PagerDuty / Slack) — Phase H (TASK-073).
  - Persistent alert log — Phase H (TASK-074); v1 lives only on the
    ``events.alerts`` stream.

The process is a thin shim: connect → consume + dispatch + sweep → close.
Any uncaught exception in the consume loop propagates, the process exits,
and the orchestrator (k8s / docker-compose) restarts it.  We deliberately
do **not** wrap the loop in a try/except — silent recovery here would
hide bugs that should page the operator.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis

from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_BOOKS, STREAM_MD_TRADES
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import BookDeltaEvent, BookSnapshotEvent, TradeEvent
from fincept_core.tracing import configure_tracing
from ingestor.quality import STALENESS_LOOP_INTERVAL_S, QualityMonitor

log = get_logger(__name__)

CONSUMER_GROUP = "ingestor.quality"


async def _staleness_loop(
    monitor: QualityMonitor,
    stop: asyncio.Event,
    *,
    interval_s: float = STALENESS_LOOP_INTERVAL_S,
) -> None:
    """Periodic staleness sweep — wakes immediately on shutdown."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        if stop.is_set():
            return
        await monitor.staleness_check()


def _make_handler(monitor: QualityMonitor) -> Any:
    """Return a Consumer-compatible handler that routes by payload type."""

    async def handler(event: Event) -> None:
        payload = event.payload
        if isinstance(payload, TradeEvent):
            await monitor.on_trade(payload)
        elif isinstance(payload, BookSnapshotEvent | BookDeltaEvent):
            await monitor.on_book(payload)
        # Other payload types (bar / alert) are not the monitor's concern;
        # silently drop so consumer-group ack progresses.

    return handler


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    consumer = Consumer(redis)
    monitor = QualityMonitor(producer)

    handler = _make_handler(monitor)

    consume_task = asyncio.create_task(
        consumer.consume(
            streams=[STREAM_MD_TRADES, STREAM_MD_BOOKS],
            group=CONSUMER_GROUP,
            consumer_name="quality-monitor",
            handler=handler,
        )
    )
    staleness_task = asyncio.create_task(_staleness_loop(monitor, stop))

    try:
        await stop.wait()
    finally:
        consume_task.cancel()
        staleness_task.cancel()
        for task in (consume_task, staleness_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await redis.aclose()  # type: ignore[attr-defined]


async def _main() -> None:
    configure_logging()
    configure_tracing("ingestor.quality")
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("quality_monitor.start")
    try:
        await run(stop)
    finally:
        log.info("quality_monitor.stop")


def main() -> None:
    """Synchronous CLI entrypoint: ``python -m ingestor.quality_main``."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
