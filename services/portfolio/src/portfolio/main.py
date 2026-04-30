"""
portfolio.main — entrypoint.

Pipeline:

  Redis -> Consumer(ord.fills) -> apply_fill ->
        Producer.publish(ord.positions)
        PositionStore.put (Redis hash for fast UI reads)

Strategy attribution: Fills don't carry strategy_id, so we recover it
via the OMS audit log (``oms.intent`` row keyed on correlation_id =
order_id, which holds the OrderIntent including strategy_id).  In v1
this is a single audit query per fill; if it becomes a bottleneck a
small in-memory order_id -> strategy_id LRU cache gates it (deferred).

UI integration notes:
  - Positions are published to ``ord.positions`` so the WebSocket
    service (TASK-051) can tail it and push real-time changes to the
    dashboard.
  - The Redis hash ``positions:{strategy_id}`` is the read path for
    the REST ``/positions`` endpoint (TASK-050).  HGETALL is O(symbols
    in that strategy) — sub-millisecond in practice.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis

from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_FILLS, STREAM_POSITIONS
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import Fill
from fincept_core.tracing import configure_tracing
from fincept_db import audit
from portfolio.state import FillStrategyResolver, PortfolioState, apply_fill
from portfolio.store import PositionStore

log = get_logger(__name__)

CONSUMER_GROUP = "portfolio"


def _make_audit_resolver() -> FillStrategyResolver:
    """Recover strategy_id by querying the OMS audit log for the order_id."""

    async def resolver(fill: Fill) -> str | None:
        try:
            entries = await audit.read_by_correlation(fill.order_id)
        except Exception:
            log.warning("portfolio.audit_unreachable", order_id=fill.order_id)
            return None
        for entry in entries:
            if entry["event_type"] == "oms.intent":
                return str(entry["payload"].get("strategy_id"))
        log.warning("portfolio.no_intent_for_fill", order_id=fill.order_id)
        return None

    return resolver


def _make_fill_handler(
    state: PortfolioState,
    store: PositionStore,
    producer: Producer,
    resolver: FillStrategyResolver,
) -> Any:
    async def handler(event: Event) -> None:
        payload = event.payload
        if not isinstance(payload, Fill):
            return
        position = await apply_fill(payload, state=state, store=store, resolve_strategy=resolver)
        if position is None:
            return  # already logged inside apply_fill / resolver
        await producer.publish(STREAM_POSITIONS, Event(type="position", payload=position))
        log.info(
            "portfolio.position_updated",
            strategy_id=position.strategy_id,
            symbol=position.symbol,
            quantity=str(position.quantity),
        )

    return handler


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    consumer = Consumer(redis)

    state = PortfolioState()
    store = PositionStore(redis)
    await state.hydrate(store)

    log.info(
        "portfolio.start",
        strategies=len(state.known_strategies()),
    )

    handler = _make_fill_handler(state, store, producer, _make_audit_resolver())
    consume_task = asyncio.create_task(
        consumer.consume(
            streams=[STREAM_FILLS],
            group=CONSUMER_GROUP,
            consumer_name="portfolio-1",
            handler=handler,
        )
    )
    heartbeat_task = asyncio.create_task(beat_periodically(redis, "portfolio"))

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
    configure_tracing("portfolio")
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await run(stop)
    finally:
        log.info("portfolio.stop")


def main() -> None:
    """Synchronous CLI entrypoint: ``python -m portfolio.main``."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
