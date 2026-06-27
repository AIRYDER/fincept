"""Tests for strategy_host.outstanding_store — Redis-backed ledger persistence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis
from strategy_host.outstanding_store import (
    OUTSTANDING_KEY_TEMPLATE,
    OutstandingOrderStore,
)

from fincept_core.schemas import OrderIntent, OrderType, Side, TimeInForce, Venue


def _intent(*, order_id: str = "ord-1", strategy_id: str = "strat-1") -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="dec-1",
        ts_event=1_000,
        strategy_id=strategy_id,
        symbol="BTC-USD",
        venue=Venue.PAPER,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.5"),
        time_in_force=TimeInForce.GTC,
    )


@pytest.fixture
async def redis() -> Any:
    r = FakeRedis()
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_hydrate_returns_empty_when_no_data(redis: Any) -> None:
    store = OutstandingOrderStore(redis, "strat-1")
    result = await store.hydrate()
    assert result == {}


@pytest.mark.asyncio
async def test_put_then_hydrate_restores_order(redis: Any) -> None:
    store = OutstandingOrderStore(redis, "strat-1")
    intent = _intent(order_id="ord-1")
    await store.put("ord-1", intent)

    # Simulate restart: create a new store instance and hydrate.
    store2 = OutstandingOrderStore(redis, "strat-1")
    result = await store2.hydrate()

    assert "ord-1" in result
    assert result["ord-1"].order_id == "ord-1"
    assert result["ord-1"].strategy_id == "strat-1"
    assert result["ord-1"].symbol == "BTC-USD"


@pytest.mark.asyncio
async def test_put_multiple_orders_then_hydrate(redis: Any) -> None:
    store = OutstandingOrderStore(redis, "strat-1")
    await store.put("ord-1", _intent(order_id="ord-1"))
    await store.put("ord-2", _intent(order_id="ord-2"))
    await store.put("ord-3", _intent(order_id="ord-3"))

    store2 = OutstandingOrderStore(redis, "strat-1")
    result = await store2.hydrate()

    assert len(result) == 3
    assert set(result.keys()) == {"ord-1", "ord-2", "ord-3"}


@pytest.mark.asyncio
async def test_per_strategy_isolation(redis: Any) -> None:
    store_a = OutstandingOrderStore(redis, "strat-a")
    store_b = OutstandingOrderStore(redis, "strat-b")

    await store_a.put("ord-1", _intent(order_id="ord-1", strategy_id="strat-a"))
    await store_b.put("ord-2", _intent(order_id="ord-2", strategy_id="strat-b"))

    # Hydrate strat-a: should only see ord-1
    result_a = await OutstandingOrderStore(redis, "strat-a").hydrate()
    assert set(result_a.keys()) == {"ord-1"}

    # Hydrate strat-b: should only see ord-2
    result_b = await OutstandingOrderStore(redis, "strat-b").hydrate()
    assert set(result_b.keys()) == {"ord-2"}


@pytest.mark.asyncio
async def test_remove_deletes_order(redis: Any) -> None:
    store = OutstandingOrderStore(redis, "strat-1")
    await store.put("ord-1", _intent(order_id="ord-1"))
    await store.put("ord-2", _intent(order_id="ord-2"))

    await store.remove("ord-1")

    result = await OutstandingOrderStore(redis, "strat-1").hydrate()
    assert "ord-1" not in result
    assert "ord-2" in result


@pytest.mark.asyncio
async def test_hydrate_skips_corrupt_entries(redis: Any) -> None:
    """If an entry in Redis is corrupt (not valid OrderIntent JSON),
    hydrate should skip it and log a warning, not crash."""
    key = OUTSTANDING_KEY_TEMPLATE.format(strategy_id="strat-1")
    await redis.hset(key, "good-order", _intent(order_id="good-order").model_dump_json())
    await redis.hset(key, "bad-order", "not-valid-json")

    store = OutstandingOrderStore(redis, "strat-1")
    result = await store.hydrate()

    assert "good-order" in result
    assert "bad-order" not in result


@pytest.mark.asyncio
async def test_hydrate_handles_redis_failure(redis: Any) -> None:
    """If Redis fails during hydrate, return empty dict (not crash)."""
    store = OutstandingOrderStore(redis, "strat-1")

    # Patch hgetall to raise
    original_hgetall = redis.hgetall

    async def failing_hgetall(*args, **kwargs):
        raise ConnectionError("redis down")

    redis.hgetall = failing_hgetall
    try:
        result = await store.hydrate()
    finally:
        redis.hgetall = original_hgetall

    assert result == {}


@pytest.mark.asyncio
async def test_put_handles_redis_failure(redis: Any) -> None:
    """If Redis fails during put, don't crash (best-effort)."""
    store = OutstandingOrderStore(redis, "strat-1")

    original_hset = redis.hset

    async def failing_hset(*args, **kwargs):
        raise ConnectionError("redis down")

    redis.hset = failing_hset
    try:
        await store.put("ord-1", _intent(order_id="ord-1"))
    finally:
        redis.hset = original_hset

    # Should not have raised


@pytest.mark.asyncio
async def test_restart_simulation(redis: Any) -> None:
    """Simulate: submit order → restart → hydrate → fill can be attributed."""
    # Phase 1: "first runner instance" puts an order
    store1 = OutstandingOrderStore(redis, "strat-1")
    intent = _intent(order_id="pre-restart-order")
    await store1.put("pre-restart-order", intent)

    # Phase 2: "restart" — new store instance hydrates from Redis
    store2 = OutstandingOrderStore(redis, "strat-1")
    outstanding = await store2.hydrate()

    # The pre-restart order should be in the ledger
    assert "pre-restart-order" in outstanding
    assert outstanding["pre-restart-order"].order_id == "pre-restart-order"
    assert outstanding["pre-restart-order"].strategy_id == "strat-1"
