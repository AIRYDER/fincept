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
from fincept_bus.streams import (
    STREAM_MD_TRADES,
    STREAM_SIG_PREDICT,
    STREAM_SIG_REGIME,
    STREAM_SIG_SENT,
)
from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import Prediction, RegimeSignal, SentimentSignal, TradeEvent
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


def _make_prediction_handler(
    router: OrchestratorRouter, *, consensus: ConsensusBuilder
) -> Any:
    async def handler(event: Event) -> None:
        # Exact event-type match, like the OMS - guards against future
        # signal types accidentally landing on STREAM_SIG_PREDICT.
        if event.type != "prediction":
            return
        payload = event.payload
        if not isinstance(payload, Prediction):
            return
        await router.on_prediction(payload)
        # Evict stale predictions to prevent unbounded cache growth.
        consensus.evict_stale(now_ns=payload.ts_event)

    return handler


# News sentiment has a longer half-life than microstructure; default
# horizon is 30 minutes vs the 15-minute GBM horizon.  Predictions
# decay against this horizon inside ConsensusBuilder, so a sentiment
# signal continues to contribute for ~30 min after publication.
SENTIMENT_HORIZON_NS = 30 * 60 * 1_000_000_000


def _sentiment_to_prediction(signal: SentimentSignal) -> Prediction:
    """Adapt a SentimentSignal into the Prediction shape ConsensusBuilder consumes.

    Mapping:
      direction = signal.score (already in [-1, 1])
      confidence = signal.confidence (already in [0, 1])
      horizon_ns = SENTIMENT_HORIZON_NS

    The agent_id passes through, so consensus correctly tracks a
    distinct sentiment source per (symbol, agent_id) cache slot
    rather than overwriting GBM predictions.
    """
    return Prediction(
        agent_id=signal.agent_id,
        symbol=signal.symbol,
        ts_event=signal.ts_event,
        horizon_ns=SENTIMENT_HORIZON_NS,
        direction=signal.score,
        confidence=signal.confidence,
    )


def _make_sentiment_handler(router: OrchestratorRouter) -> Any:
    async def handler(event: Event) -> None:
        if event.type != "sentiment":
            return
        payload = event.payload
        if not isinstance(payload, SentimentSignal):
            return
        # Re-route through the prediction pipeline.  Same deadband,
        # same allocator, same audit trail - cross-source consistency.
        await router.on_prediction(_sentiment_to_prediction(payload))

    return handler


# Regime signals don't carry a symbol - they're market-wide.  We
# translate one RegimeSignal into N synthetic Predictions, one per
# universe symbol, so the existing per-symbol consensus naturally
# picks up the macro tilt without a second aggregator path.
REGIME_HORIZON_NS = 4 * 60 * 60 * 1_000_000_000  # 4 hours


# Lazy import keeps the orchestrator from depending on the agents
# package at module import time (the agents pyproject is not always
# installed in production deploys; only the regime_agent service uses it).
def _regime_to_direction(regime: str) -> float:
    from agents.regime_agent.rules import REGIME_DIRECTION

    return REGIME_DIRECTION.get(regime, 0.0)


def _regime_to_predictions(
    signal: RegimeSignal, *, universe: list[str]
) -> list[Prediction]:
    """Fan-out a market-wide regime into per-symbol Predictions.

    Each symbol gets the SAME direction and confidence; consensus
    will weight it together with the per-symbol GBM and sentiment
    signals.  Horizon is longer than sentiment (4h vs 30min) because
    macro regimes shift on hours-to-days, not minutes.
    """
    direction = _regime_to_direction(signal.regime)
    return [
        Prediction(
            agent_id=signal.agent_id,
            symbol=symbol,
            ts_event=signal.ts_event,
            horizon_ns=REGIME_HORIZON_NS,
            direction=direction,
            confidence=signal.confidence,
        )
        for symbol in universe
    ]


def _make_regime_handler(router: OrchestratorRouter, *, universe: list[str]) -> Any:
    async def handler(event: Event) -> None:
        if event.type != "regime":
            return
        payload = event.payload
        if not isinstance(payload, RegimeSignal):
            return
        for prediction in _regime_to_predictions(payload, universe=universe):
            await router.on_prediction(prediction)

    return handler


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    assert_safe_for_runtime(settings)
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    prices = LivePrices()
    consensus = ConsensusBuilder()
    target_state = TargetState(redis=redis)
    await target_state.hydrate()
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
    sent_consumer = Consumer(redis)
    regime_consumer = Consumer(redis)
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
            handler=_make_prediction_handler(router, consensus=consensus),
        )
    )
    sent_task = asyncio.create_task(
        sent_consumer.consume(
            streams=[STREAM_SIG_SENT],
            group=CONSUMER_GROUP,
            consumer_name="orchestrator-sentiment",
            handler=_make_sentiment_handler(router),
        )
    )
    regime_task = asyncio.create_task(
        regime_consumer.consume(
            streams=[STREAM_SIG_REGIME],
            group=CONSUMER_GROUP,
            consumer_name="orchestrator-regime",
            handler=_make_regime_handler(router, universe=list(settings.UNIVERSE)),
        )
    )

    def orchestrator_stats() -> dict[str, Any]:
        """Collect orchestrator metrics for heartbeat."""
        return {
            "consensus": {
                "cached_symbols": consensus.cached_symbols,
                "cached_entries": consensus.cached_entries,
                "total_evicted": consensus.total_evicted,
            },
            "target_state": {
                "known_symbols": len(target_state.known_symbols()),
            },
        }

    heartbeat_task = asyncio.create_task(
        beat_periodically(redis, "orchestrator", stats_callback=orchestrator_stats)
    )
    try:
        await stop.wait()
    finally:
        for task in (heartbeat_task, price_task, pred_task, sent_task, regime_task):
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
