"""
oms.main — OMS entrypoint with sim + Alpaca routing.

Routing is selected at startup via ``Settings.OMS_ROUTER``:

  - ``"sim"`` (default)
        Bus -> Consumer(md.trades)   -> LivePrices.update            (price feed)
        Bus -> Consumer(ord.orders)  -> process_intent (PaperFiller) ->
                                        Producer.publish(ord.orders) (state events)
                                        Producer.publish(ord.fills)  (fill events)
                                        fincept_db.audit.append      (audit trail)

  - ``"alpaca"``
        Bus -> Consumer(ord.orders)  -> alpaca.submit_intent ->
                                        publish state events + (optional) Fill
                                        fincept_db.audit.append
        + background task tailing pending Alpaca orders, emitting
          Fill / terminal-unfilled events when they finally land.

Both branches publish identical event shapes to ``ord.orders`` and
``ord.fills``, so downstream services (portfolio, API, dashboard)
don't know or care which router fed the events.

Out of scope for this commit:
  - HA / leadership election (deferred to Phase H).
  - Cancel / replace / partial fills (Phase H refinements).
  - Direct exchange routing (TASK-075 - distinct from Alpaca paper).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

import httpx
from redis.asyncio import Redis

from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_FILLS, STREAM_MD_TRADES, STREAM_ORDERS
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import Fill, Order, OrderIntent, TradeEvent
from fincept_core.tracing import configure_tracing
from fincept_db import audit
from oms.alpaca import AlpacaClient, poll_pending_orders, submit_intent
from oms.alpaca.runtime import PendingOrder
from oms.paper import PaperFiller
from oms.prices import LivePrices
from oms.processor import IntentResult, process_intent

log = get_logger(__name__)

CONSUMER_GROUP = "oms"


# ---------------------------------------------------------------------------
# Audit + publish helpers shared by both routers
# ---------------------------------------------------------------------------


async def _audit_intent(intent: OrderIntent, *, actor: str) -> None:
    with contextlib.suppress(Exception):
        await audit.append(
            actor=actor,
            event_type="oms.intent",
            payload=intent.model_dump(mode="json"),
            correlation_id=intent.order_id,
        )


async def _publish_result(
    result: IntentResult,
    *,
    producer: Producer,
    actor: str,
) -> None:
    """Publish each Order state to ord.orders + Fill to ord.fills + audit."""
    for order in result.order_states:
        await producer.publish(STREAM_ORDERS, Event(type="order", payload=order))
        with contextlib.suppress(Exception):
            await audit.append(
                actor=actor,
                event_type="oms.state",
                payload={
                    "status": order.status.value,
                    "order": order.model_dump(mode="json"),
                },
                correlation_id=order.order_id,
            )
    if result.fill is not None:
        await producer.publish(STREAM_FILLS, Event(type="fill", payload=result.fill))
        with contextlib.suppress(Exception):
            await audit.append(
                actor=actor,
                event_type="oms.fill",
                payload=result.fill.model_dump(mode="json"),
                correlation_id=result.fill.order_id,
            )


# ---------------------------------------------------------------------------
# Sim router (the original PaperFiller path)
# ---------------------------------------------------------------------------


def _make_price_handler(prices: LivePrices) -> Any:
    async def handler(event: Event) -> None:
        payload = event.payload
        if isinstance(payload, TradeEvent):
            prices.update(payload.symbol, payload.price)

    return handler


def _make_sim_intent_handler(producer: Producer, prices: LivePrices, filler: PaperFiller) -> Any:
    async def handler(event: Event) -> None:
        payload = event.payload
        if not isinstance(payload, OrderIntent):
            return
        result = process_intent(payload, prices=prices, filler=filler)
        await _audit_intent(payload, actor="oms.sim")
        await _publish_result(result, producer=producer, actor="oms.sim")
        log.info(
            "oms.sim.processed",
            order_id=payload.order_id,
            final_status=result.final_status.value,
            filled=result.fill is not None,
        )

    return handler


async def _run_sim(stop: asyncio.Event, redis: Redis[Any], producer: Producer) -> None:
    prices = LivePrices()
    filler = PaperFiller()
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
            handler=_make_sim_intent_handler(producer, prices, filler),
        )
    )
    try:
        await stop.wait()
    finally:
        for task in (price_task, intent_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# ---------------------------------------------------------------------------
# Alpaca router (real paper-broker path)
# ---------------------------------------------------------------------------


def _make_alpaca_intent_handler(
    producer: Producer,
    client: AlpacaClient,
    pending: dict[str, PendingOrder],
) -> Any:
    async def handler(event: Event) -> None:
        payload = event.payload
        if not isinstance(payload, OrderIntent):
            return
        await _audit_intent(payload, actor="oms.alpaca")
        result = await submit_intent(payload, client=client, pending=pending)
        await _publish_result(result, producer=producer, actor="oms.alpaca")
        log.info(
            "oms.alpaca.processed",
            order_id=payload.order_id,
            final_status=result.final_status.value,
            filled=result.fill is not None,
            still_pending=payload.order_id in pending,
        )

    return handler


async def _run_alpaca(stop: asyncio.Event, redis: Redis[Any], producer: Producer) -> None:
    settings = get_settings()
    if not settings.ALPACA_API_KEY or not settings.ALPACA_API_SECRET:
        raise RuntimeError("OMS_ROUTER=alpaca but ALPACA_API_KEY / ALPACA_API_SECRET not set")

    pending: dict[str, PendingOrder] = {}

    async with httpx.AsyncClient(
        base_url=settings.ALPACA_BASE_URL,
        timeout=httpx.Timeout(10.0, connect=5.0),
    ) as http:
        client = AlpacaClient(
            http=http,
            api_key=settings.ALPACA_API_KEY,
            api_secret=settings.ALPACA_API_SECRET,
        )

        async def on_filled(order: Order, fill: Fill) -> None:
            await _publish_result(
                IntentResult(order_states=[order], fill=fill),
                producer=producer,
                actor="oms.alpaca.poll",
            )

        async def on_terminal(order: Order) -> None:
            await _publish_result(
                IntentResult(order_states=[order], fill=None),
                producer=producer,
                actor="oms.alpaca.poll",
            )

        intent_consumer = Consumer(redis)
        intent_task = asyncio.create_task(
            intent_consumer.consume(
                streams=[STREAM_ORDERS],
                group=CONSUMER_GROUP,
                consumer_name="oms-alpaca-intents",
                handler=_make_alpaca_intent_handler(producer, client, pending),
            )
        )
        poll_task = asyncio.create_task(
            poll_pending_orders(
                client=client,
                pending=pending,
                on_filled=on_filled,
                on_terminal=on_terminal,
                stop=stop,
            )
        )
        try:
            await stop.wait()
        finally:
            for task in (intent_task, poll_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    if settings.TRADING_MODE != "paper":
        raise RuntimeError(
            f"OMS started with TRADING_MODE={settings.TRADING_MODE!r}; v1 is paper-only"
        )

    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)

    log.info("oms.start", router=settings.OMS_ROUTER)
    try:
        if settings.OMS_ROUTER == "alpaca":
            await _run_alpaca(stop, redis, producer)
        elif settings.OMS_ROUTER == "sim":
            await _run_sim(stop, redis, producer)
        else:
            raise RuntimeError(
                f"unknown OMS_ROUTER={settings.OMS_ROUTER!r}; expected 'sim' or 'alpaca'"
            )
    finally:
        await redis.aclose()  # type: ignore[attr-defined]


async def _main() -> None:
    configure_logging()
    configure_tracing("oms")
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await run(stop)
    finally:
        log.info("oms.stop")


def main() -> None:
    """Synchronous CLI entrypoint: ``python -m oms.main``."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
