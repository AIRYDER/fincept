from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis

from agents.sentiment_features.store import SentimentFeatureStore
from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_FEATURES_ONLINE, STREAM_SIG_SENT
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import SentimentSignal
from fincept_core.tracing import configure_tracing

log = get_logger(__name__)
SERVICE_NAME = "sentiment_features"
GROUP_NAME = "sentiment_features.v1"
DEFAULT_REFRESH_INTERVAL_SEC = 60.0


async def handle_sentiment_event(
    event: Event,
    *,
    store: SentimentFeatureStore,
    producer: Producer,
) -> None:
    if event.type != "sentiment" or not isinstance(event.payload, SentimentSignal):
        return
    frame = await store.add_signal(event.payload)
    await producer.publish(
        STREAM_FEATURES_ONLINE,
        Event(type="feature_frame", payload=frame),
    )
    log.info(
        "sentiment_features.updated",
        symbol=frame.symbol,
        ts_event=frame.ts_event,
        windows=list(store.windows_min),
        populated=sum(1 for value in frame.values.values() if value is not None),
    )


async def refresh_sentiment_features(
    *,
    store: SentimentFeatureStore,
    producer: Producer,
) -> int:
    frames = await store.refresh_all()
    for frame in frames:
        await producer.publish(
            STREAM_FEATURES_ONLINE,
            Event(type="feature_frame", payload=frame),
        )
    if frames:
        log.info("sentiment_features.refresh", count=len(frames))
    return len(frames)


async def refresh_loop(
    *,
    store: SentimentFeatureStore,
    producer: Producer,
    stop: asyncio.Event,
    interval_sec: float = DEFAULT_REFRESH_INTERVAL_SEC,
) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
        if stop.is_set():
            break
        await refresh_sentiment_features(store=store, producer=producer)


async def run_loop(*, consumer_name: str, stop: asyncio.Event) -> None:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    consumer = Consumer(redis)
    store = SentimentFeatureStore(redis)
    heartbeat_task = asyncio.create_task(beat_periodically(redis, SERVICE_NAME))
    consume_task: asyncio.Task[None] | None = None
    refresh_task: asyncio.Task[None] | None = None

    async def handler(event: Event) -> None:
        await handle_sentiment_event(event, store=store, producer=producer)

    try:
        log.info("sentiment_features.start", consumer_name=consumer_name)
        refresh_task = asyncio.create_task(
            refresh_loop(store=store, producer=producer, stop=stop)
        )
        consume_task = asyncio.create_task(
            consumer.consume(
                [STREAM_SIG_SENT],
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
        if refresh_task is not None:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
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
        log.info("sentiment_features.stop")


def main() -> None:
    parser = argparse.ArgumentParser(prog="sentiment_features.main")
    parser.add_argument("--consumer-name", default="sentiment-features-1")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
