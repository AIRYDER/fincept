"""Tests for portfolio.store.PositionStore using fakeredis."""

from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
import pytest

from fincept_core.schemas import Position
from portfolio.store import PositionStore


def _position(
    *,
    strategy_id: str = "ma_crossover.v1",
    symbol: str = "BTC-USD",
    qty: str = "1",
    avg_cost: str = "100",
    realized: str = "0",
    updated_at: int = 1_000,
) -> Position:
    return Position(
        strategy_id=strategy_id,
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal(avg_cost),
        realized_pnl=Decimal(realized),
        updated_at=updated_at,
    )


@pytest.fixture
def store() -> PositionStore:
    return PositionStore(fakeredis.aioredis.FakeRedis())


async def test_get_returns_none_for_unknown_position(store: PositionStore) -> None:
    assert await store.get("nope", "BTC-USD") is None


async def test_put_then_get_round_trips(store: PositionStore) -> None:
    pos = _position(qty="2", avg_cost="105")
    await store.put(pos)
    out = await store.get(pos.strategy_id, pos.symbol)
    assert out is not None
    assert out == pos


async def test_get_all_returns_every_symbol_for_strategy(store: PositionStore) -> None:
    await store.put(_position(symbol="BTC-USD", qty="1"))
    await store.put(_position(symbol="ETH-USD", qty="2"))
    out = await store.get_all("ma_crossover.v1")
    assert set(out.keys()) == {"BTC-USD", "ETH-USD"}
    assert out["BTC-USD"].quantity == Decimal("1")
    assert out["ETH-USD"].quantity == Decimal("2")


async def test_strategies_are_isolated(store: PositionStore) -> None:
    """Same symbol, different strategies -> independent cache slots."""
    await store.put(_position(strategy_id="strat_a", symbol="BTC-USD", qty="1"))
    await store.put(_position(strategy_id="strat_b", symbol="BTC-USD", qty="-3"))

    a = await store.get("strat_a", "BTC-USD")
    b = await store.get("strat_b", "BTC-USD")
    assert a is not None and a.quantity == Decimal("1")
    assert b is not None and b.quantity == Decimal("-3")


async def test_put_overwrites_prior_value(store: PositionStore) -> None:
    await store.put(_position(qty="1", avg_cost="100"))
    await store.put(_position(qty="2", avg_cost="105"))
    out = await store.get("ma_crossover.v1", "BTC-USD")
    assert out is not None
    assert out.quantity == Decimal("2")
    assert out.avg_cost == Decimal("105")


async def test_known_strategies_grows_with_writes(store: PositionStore) -> None:
    assert await store.known_strategies() == set()
    await store.put(_position(strategy_id="strat_a"))
    await store.put(_position(strategy_id="strat_b"))
    await store.put(_position(strategy_id="strat_a"))  # idempotent
    known = await store.known_strategies()
    assert known == {"strat_a", "strat_b"}
