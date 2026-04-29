"""
oms.main — paper OMS entrypoint.

Wires the bus + DB + price cache + processor:

  Redis -> Consumer(md.trades)   -> LivePrices.update            (price feed)
  Redis -> Consumer(ord.orders)  -> process_intent ->
                                    Producer.publish(ord.orders) (state events)
                                    Producer.publish(ord.fills)  (fill events)
                                    fincept_db.audit.append      (audit trail)

Stream conventions (per CONTRACTS.md §6):
  - ``ord.orders``  carries both OrderIntent (input) and Order (state events).
  - ``ord.fills``   carries Fill events.

A Live-mode strategy submits an OrderIntent to ``ord.orders`` via a
LiveStrategyContext (TASK-040 territory) — this OMS doesn't care where
the intents come from.  In v1, tests publish manually.

Out of scope for this commit:
  - HA / leadership election (deferred to Phase H).
  - Live venue routing (TASK-075).
  - Cancel / replace / partial fills (Phase H refinements).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis

from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_FILLS, STREAM_MD_TRADES, STREAM_ORDERS
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import OrderIntent, TradeEvent
from fincept_core.tracing import configure_tracing
from fincept_db import audit
from oms.paper import PaperFiller
from oms.prices import LivePrices
from oms.processor import process_intent

log = get_logger(__name__)

CONSUMER_GROUP = "oms"


def _make_price_handler(prices: LivePrices) -> Any:
    async def handler(event: Event) -> None:
        payload = event.payload
        if isinstance(payload, TradeEvent):
            prices.update(payload.symbol, payload.price)

    return handler


def _make_intent_handler(producer: Producer, prices: LivePrices, filler: PaperFiller) -> Any:
    async def handler(event: Event) -> None:
        payload = event.payload
        if not isinstance(payload, OrderIntent):
            return  # not our event - ack and skip
        result = process_intent(payload, prices=prices, filler=filler)

        # Audit the intent up-front so we have a record even if a later
        # publish fails.  Audit is best-effort: log but don't raise.
        with contextlib.suppress(Exception):
            await audit.append(
                actor="oms.paper",
                event_type="oms.intent",
                payload=payload.model_dump(mode="json"),
                correlation_id=payload.order_id,
            )

        for order in result.order_states:
            await producer.publish(STREAM_ORDERS, Event(type="order", payload=order))
            with contextlib.suppress(Exception):
                await audit.append(
                    actor="oms.paper",
                    event_type="oms.state",
                    payload={"status": order.status.value, "order": order.model_dump(mode="json")},
                    correlation_id=order.order_id,
                )

        if result.fill is not None:
            await producer.publish(STREAM_FILLS, Event(type="fill", payload=result.fill))
            with contextlib.suppress(Exception):
                await audit.append(
                    actor="oms.paper",
                    event_type="oms.fill",
                    payload=result.fill.model_dump(mode="json"),
                    correlation_id=result.fill.order_id,
                )
        log.info(
            "oms.processed",
            order_id=payload.order_id,
            final_status=result.final_status.value,
            filled=result.fill is not None,
        )

    return handler


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    if settings.TRADING_MODE != "paper":
        raise RuntimeError(
            f"OMS started with TRADING_MODE={settings.TRADING_MODE!r}; v1 is paper-only"
        )

    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    prices = LivePrices()
    filler = PaperFiller()

    # Two parallel consumers: one tailing the trade feed (price cache),
    # one tailing the order intents (the actual work).
    price_consumer = Consumer(redis)
    intent_consumer = Consumer(redis)

    price_task = asyncio.create_task(
        price_consumer.consume(
            streams=[STREAM_MD_TRADES],
            group=CONSUMER_GROUP,
            consumer_name="oms-prices",
            handler=_make_price_handler(prices),
        )
    )
    intent_task = asyncio.create_task(
        intent_consumer.consume(
            streams=[STREAM_ORDERS],
            group=CONSUMER_GROUP,
            consumer_name="oms-intents",
            handler=_make_intent_handler(producer, prices, filler),
        )
    )

    try:
        await stop.wait()
    finally:
        for task in (price_task, intent_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await redis.aclose()  # type: ignore[attr-defined]


async def _main() -> None:
    configure_logging()
    configure_tracing("oms.paper")
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("oms.start")
    try:
        await run(stop)
    finally:
        log.info("oms.stop")


def main() -> None:
    """Synchronous CLI entrypoint: ``python -m oms.main``."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
