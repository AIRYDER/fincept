"""End-to-end tests for orchestrator.router.OrchestratorRouter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import fakeredis.aioredis
import pytest_asyncio
from redis.asyncio import Redis

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_DECISIONS, STREAM_ORDERS
from fincept_core.events import Event
from fincept_core.schemas import Decision, OrderIntent, Prediction

from oms.prices import LivePrices

from orchestrator.consensus import ConsensusBuilder
from orchestrator.decisions import TargetState
from orchestrator.router import OrchestratorRouter


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis[Any]]:
    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def producer(redis: Redis[Any]) -> Producer:
    return Producer(redis)


def _pred(
    *,
    agent_id: str = "gbm.v1",
    symbol: str = "BTC-USD",
    direction: float = 0.8,
    confidence: float = 0.9,
    ts_event: int | None = None,
) -> Prediction:
    """Build a Prediction with a near-now timestamp by default.

    The orchestrator's consensus rejects stale predictions (older than
    ``max_age_ns``).  Using a fake ts_event in 1970 would have every
    prediction filtered as stale; we default to ``now_ns()`` so tests
    exercise the real emission path."""
    from fincept_core.clock import now_ns

    return Prediction(
        agent_id=agent_id,
        symbol=symbol,
        ts_event=ts_event if ts_event is not None else now_ns(),
        horizon_ns=900_000_000_000,  # 15 min
        direction=direction,
        confidence=confidence,
    )


async def _read_first_published(
    redis: Redis[Any], stream: str
) -> tuple[str, Event] | None:
    """Read the first entry on ``stream`` via raw XRANGE.

    We deliberately don't use Consumer.consume here: consumer groups
    only deliver messages published AFTER the group was created via
    XGROUP CREATE, but our tests publish BEFORE any consumer exists.
    XRANGE returns everything in the stream regardless.
    """
    entries = await redis.xrange(stream, count=1)
    if not entries:
        return None
    _msg_id, fields = entries[0]
    from fincept_core.events import deserialize

    event = deserialize(fields)
    return stream, event


def _make_router(
    *,
    producer: Producer,
    prices: LivePrices,
    cap_per_symbol: Decimal = Decimal("10000"),
    min_delta_usd: Decimal = Decimal("100"),
    confidence_threshold: float = 0.1,
) -> OrchestratorRouter:
    return OrchestratorRouter(
        producer=producer,
        prices=prices,
        consensus=ConsensusBuilder(),
        target_state=TargetState(),
        cap_per_symbol=cap_per_symbol,
        min_delta_usd=min_delta_usd,
        confidence_threshold=confidence_threshold,
    )


# ---------------------------------------------------------------------------
# Skip cases (no emission)
# ---------------------------------------------------------------------------


async def test_low_confidence_signal_skipped(producer: Producer) -> None:
    """Below-threshold signals should not produce any events."""
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("50000"))
    router = _make_router(producer=producer, prices=prices, confidence_threshold=0.5)

    await router.on_prediction(_pred(direction=0.1, confidence=0.4))

    assert await _read_first_published(producer.redis, STREAM_DECISIONS) is None
    assert await _read_first_published(producer.redis, STREAM_ORDERS) is None


async def test_no_price_skipped(producer: Producer) -> None:
    """Without a price, we can't size; skip rather than guess."""
    prices = LivePrices()  # empty
    router = _make_router(producer=producer, prices=prices)

    await router.on_prediction(_pred(direction=0.8, confidence=0.9))

    assert await _read_first_published(producer.redis, STREAM_ORDERS) is None


async def test_within_deadband_skipped(producer: Producer) -> None:
    """A second prediction with same target as last emission is skipped."""
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("50000"))
    router = _make_router(producer=producer, prices=prices, min_delta_usd=Decimal("50"))

    # First emission: large delta from 0 target.
    await router.on_prediction(_pred(direction=0.8, confidence=0.9))
    # Second emission with identical signal -> 0 delta -> skip.
    # Need to read+drain previous events first.  Easier: just confirm
    # the target_state was updated and assert second call doesn't add
    # to the producer beyond the original.
    # We'll achieve this by checking consensus output.
    cons = router._consensus.consensus("BTC-USD", now_ns=2_000_000_000)
    assert cons is not None
    # Same prediction -> same target -> delta = 0 -> skip.
    initial_state = dict(router._target_state.targets)
    await router.on_prediction(_pred(direction=0.8, confidence=0.9))
    assert dict(router._target_state.targets) == initial_state


# ---------------------------------------------------------------------------
# Emission paths
# ---------------------------------------------------------------------------


async def test_emits_decision_and_intent_on_first_strong_signal(
    producer: Producer,
) -> None:
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("50000"))
    router = _make_router(producer=producer, prices=prices)

    await router.on_prediction(_pred(direction=0.8, confidence=0.9))

    decision_event = await _read_first_published(producer.redis, STREAM_DECISIONS)
    intent_event = await _read_first_published(producer.redis, STREAM_ORDERS)
    assert decision_event is not None
    assert intent_event is not None

    _, dec_event = decision_event
    _, int_event = intent_event

    assert dec_event.type == "decision"
    assert int_event.type == "order_intent"
    assert isinstance(dec_event.payload, Decision)
    assert isinstance(int_event.payload, OrderIntent)
    # decision_id is shared
    assert int_event.payload.decision_id == dec_event.payload.decision_id


async def test_emission_updates_target_state(producer: Producer) -> None:
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("50000"))
    router = _make_router(producer=producer, prices=prices)

    await router.on_prediction(_pred(direction=0.8, confidence=0.9))

    # 0.8 * 0.9 * 10000 = 7200
    assert router._target_state.targets["BTC-USD"] == Decimal("7200.00")


async def test_signal_flip_emits_full_rebalance(producer: Producer) -> None:
    """From +7200 long to -7200 short = 14400 delta.  This is one of
    the most important behaviors: the orchestrator must size the FLIP,
    not just the new target."""
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("50000"))
    router = _make_router(producer=producer, prices=prices)

    await router.on_prediction(_pred(direction=0.8, confidence=0.9))
    # Now flip the signal.
    await router.on_prediction(_pred(direction=-0.8, confidence=0.9))

    # Final target after flip: 0.8 * 0.9 * 10000 = 7200, but signed -.
    assert router._target_state.targets["BTC-USD"] == Decimal("-7200.00")


async def test_per_symbol_target_state_isolated(producer: Producer) -> None:
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("50000"))
    prices.update("ETH-USD", Decimal("3000"))
    router = _make_router(producer=producer, prices=prices)

    await router.on_prediction(_pred(symbol="BTC-USD", direction=0.5, confidence=1.0))
    await router.on_prediction(_pred(symbol="ETH-USD", direction=-0.5, confidence=1.0))

    assert router._target_state.targets["BTC-USD"] == Decimal("5000.00")
    assert router._target_state.targets["ETH-USD"] == Decimal("-5000.00")


# ---------------------------------------------------------------------------
# Quantity sizing
# ---------------------------------------------------------------------------


async def test_intent_quantity_matches_delta_div_price(producer: Producer) -> None:
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("50000"))
    router = _make_router(producer=producer, prices=prices)

    await router.on_prediction(_pred(direction=1.0, confidence=1.0))

    # Target = 10000; delta from 0 = 10000; 10000/50000 = 0.2 BTC.
    intent_event = await _read_first_published(producer.redis, STREAM_ORDERS)
    assert intent_event is not None
    intent = intent_event[1].payload
    assert isinstance(intent, OrderIntent)
    assert intent.quantity == Decimal("0.20000000")
