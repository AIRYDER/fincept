"""
orchestrator.main - long-running entrypoint.

  python -m orchestrator.main

Wires three consumer tasks against a single Redis client:

  - md.trades         -> LivePrices.update      (price cache for sizing)
  - sig.predict       -> OrchestratorRouter.on_prediction (the actual work)

Graceful shutdown on SIGINT / SIGTERM cancels both tasks and closes
the Redis connection.

The orchestrator does NOT run a kill-switch alert consumer.  Risk
gating - including kill-switch enforcement - lives in the OMS, where
it has access to actual position state.  The orchestrator can publish
all the OrderIntents it wants; the gate downstream is the source of
truth for acceptance.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_TRADES, STREAM_SIG_PREDICT
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import Prediction, TradeEvent
from fincept_core.tracing import configure_tracing

from oms.prices import LivePrices

from orchestrator.consensus import ConsensusBuilder
from orchestrator.decisions import TargetState
from orchestrator.router import OrchestratorRouter

log = get_logger(__name__)

CONSUMER_GROUP = "orchestrator"


def _make_price_handler(prices: LivePrices) -> Any:
    async def handler(event: Event) -> None:
        payload = event.payload
        if isinstance(payload, TradeEvent):
            prices.update(payload.symbol, payload.price)

    return handler


def _make_prediction_handler(router: OrchestratorRouter) -> Any:
    async def handler(event: Event) -> None:
        # Exact event-type match, like the OMS - guards against future
        # signal types accidentally landing on STREAM_SIG_PREDICT.
        if event.type != "prediction":
            return
        payload = event.payload
        if not isinstance(payload, Prediction):
            return
        await router.on_prediction(payload)

    return handler


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    prices = LivePrices()
    consensus = ConsensusBuilder()
    target_state = TargetState()
    router = OrchestratorRouter(
        producer=producer,
        prices=prices,
        consensus=consensus,
        target_state=target_state,
        cap_per_symbol=Decimal(settings.MAX_NOTIONAL_USD_PER_SYMBOL),
    )

    log.info(
        "orchestrator.start",
        cap_per_symbol=settings.MAX_NOTIONAL_USD_PER_SYMBOL,
        universe=list(settings.UNIVERSE),
    )

    price_consumer = Consumer(redis)
    pred_consumer = Consumer(redis)
    price_task = asyncio.create_task(
        price_consumer.consume(
            streams=[STREAM_MD_TRADES],
            group=CONSUMER_GROUP,
            consumer_name="orchestrator-prices",
            handler=_make_price_handler(prices),
        )
    )
    pred_task = asyncio.create_task(
        pred_consumer.consume(
            streams=[STREAM_SIG_PREDICT],
            group=CONSUMER_GROUP,
            consumer_name="orchestrator-predictions",
            handler=_make_prediction_handler(router),
        )
    )
    try:
        await stop.wait()
    finally:
        for task in (price_task, pred_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await redis.aclose()  # type: ignore[attr-defined]


async def _main() -> None:
    configure_logging()
    configure_tracing("orchestrator")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run(stop)
    finally:
        log.info("orchestrator.stop")


def main() -> None:
    """Synchronous CLI entrypoint: ``python -m orchestrator.main``."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
