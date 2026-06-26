from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from agents.news_outcome_labeler.store import NewsOutcomeStore
from fincept_bus.consumer import Consumer
from fincept_bus.streams import STREAM_FEATURES_ONLINE, STREAM_MD_TRADES
from fincept_core.clock import now_ns
from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import FeatureFrame, TradeEvent
from fincept_core.tracing import configure_tracing

log = get_logger(__name__)
SERVICE_NAME = "news_outcome_labeler"
GROUP_NAME = "news_outcome_labeler.v1"
DEFAULT_LABEL_INTERVAL_SEC = 60.0


def mark_key(symbol: str) -> str:
    return f"md:last:{symbol}"


async def write_mark(redis: Redis[Any], symbol: str, price: Decimal) -> None:
    await redis.hset(
        mark_key(symbol), mapping={"px": str(price), "ts_ns": str(now_ns())}
    )


async def read_mark(redis: Redis[Any], symbol: str) -> Decimal | None:
    raw = await redis.hget(mark_key(symbol), "px")
    if raw is None:
        return None
    return Decimal(raw.decode() if isinstance(raw, bytes) else raw)


async def read_mark_at_or_after(
    redis: Redis[Any],
    symbol: str,
    ts_event: int,
) -> Decimal | None:
    data = await redis.hgetall(mark_key(symbol))
    if not data:
        return None
    decoded = {
        (key.decode() if isinstance(key, bytes) else key): (
            value.decode() if isinstance(value, bytes) else value
        )
        for key, value in data.items()
    }
    mark_ts = decoded.get("ts_ns")
    price = decoded.get("px")
    if mark_ts is None or price is None or int(mark_ts) < ts_event:
        return None
    return Decimal(price)


async def handle_event(
    event: Event,
    *,
    redis: Redis[Any],
    store: NewsOutcomeStore,
) -> None:
    payload = event.payload
    if event.type == "trade" and isinstance(payload, TradeEvent):
        await write_mark(redis, payload.symbol, payload.price)
        return
    if event.type != "feature_frame" or not isinstance(payload, FeatureFrame):
        return
    if payload.freq != "sentiment":
        return
    start_price = await read_mark(redis, payload.symbol)
    example_id = await store.capture_snapshot(payload, start_price=start_price)
    if example_id is not None:
        log.info(
            "news_outcome.snapshot",
            example_id=example_id,
            symbol=payload.symbol,
            ts_event=payload.ts_event,
        )


async def label_loop(
    *,
    redis: Redis[Any],
    store: NewsOutcomeStore,
    stop: asyncio.Event,
    interval_sec: float = DEFAULT_LABEL_INTERVAL_SEC,
) -> None:
    async def price_lookup(symbol: str, _ts_event: int) -> Decimal | None:
        return await read_mark_at_or_after(redis, symbol, _ts_event)

    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
        if stop.is_set():
            break
        labels = await store.label_due(now_ns=now_ns(), price_lookup=price_lookup)
        if labels:
            log.info("news_outcome.labels", count=len(labels))


async def run_loop(*, consumer_name: str, stop: asyncio.Event) -> None:
    settings = get_settings()
    assert_safe_for_runtime(settings)
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    consumer = Consumer(redis)
    store = NewsOutcomeStore(redis)
    heartbeat_task = asyncio.create_task(beat_periodically(redis, SERVICE_NAME))
    label_task: asyncio.Task[None] | None = None
    consume_task: asyncio.Task[None] | None = None

    async def handler(event: Event) -> None:
        await handle_event(event, redis=redis, store=store)

    try:
        log.info("news_outcome.start", consumer_name=consumer_name)
        label_task = asyncio.create_task(
            label_loop(redis=redis, store=store, stop=stop)
        )
        consume_task = asyncio.create_task(
            consumer.consume(
                [STREAM_FEATURES_ONLINE, STREAM_MD_TRADES],
                GROUP_NAME,
                consumer_name,
                handler,
                block_ms=1000,
                batch=100,
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
        if label_task is not None:
            label_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await label_task
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
        log.info("news_outcome.stop")


def main() -> None:
    parser = argparse.ArgumentParser(prog="news_outcome_labeler.main")
    parser.add_argument("--consumer-name", default="news-outcome-labeler-1")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
