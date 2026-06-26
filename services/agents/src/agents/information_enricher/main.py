from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis

from agents.information_enricher.enrich import enrich_information_event
from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_INFO_ENRICHED, STREAM_INFO_RAW
from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import InformationEvent
from fincept_core.tracing import configure_tracing

log = get_logger(__name__)
SERVICE_NAME = "information_enricher"
GROUP_NAME = "information_enricher.v1"


async def handle_information_event(event: Event, *, producer: Producer) -> None:
    if event.type != "information" or not isinstance(event.payload, InformationEvent):
        return
    enriched = enrich_information_event(event.payload)
    await producer.publish(
        STREAM_INFO_ENRICHED,
        Event(type="information", payload=enriched),
    )
    log.info(
        "information.enriched",
        event_id=enriched.event_id,
        source_type=enriched.source_type,
        event_category=enriched.event_category,
        symbols=enriched.symbols,
        dedupe_group_id=enriched.dedupe_group_id,
    )


async def run_loop(*, consumer_name: str, stop: asyncio.Event) -> None:
    settings = get_settings()
    assert_safe_for_runtime(settings)
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    consumer = Consumer(redis)
    heartbeat_task = asyncio.create_task(beat_periodically(redis, SERVICE_NAME))
    consume_task: asyncio.Task[None] | None = None

    async def handler(event: Event) -> None:
        await handle_information_event(event, producer=producer)

    try:
        log.info("information_enricher.start", consumer_name=consumer_name)
        consume_task = asyncio.create_task(
            consumer.consume(
                [STREAM_INFO_RAW],
                GROUP_NAME,
                consumer_name,
                handler,
                block_ms=1000,
                batch=50,
            )
        )
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {consume_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            await task
    finally:
        if consume_task is not None:
            consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consume_task
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await redis.aclose()  # type: ignore[attr-defined]


async def _main(args: argparse.Namespace) -> None:
    configure_logging()
    configure_tracing(SERVICE_NAME)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_loop(consumer_name=args.consumer_name, stop=stop)
    finally:
        log.info("information_enricher.stop")


def main() -> None:
    parser = argparse.ArgumentParser(prog="information_enricher.main")
    parser.add_argument("--consumer-name", default="information-enricher-1")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
