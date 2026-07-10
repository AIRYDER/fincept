"""Tests for /positions and /positions/{strategy_id}."""

from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
from fincept_core.schemas import Position
from httpx import AsyncClient
from portfolio.store import PositionStore


def _position(
    *,
    strategy_id: str = "ma_crossover.v1",
    symbol: str = "BTC-USD",
    qty: str = "1",
) -> Position:
    return Position(
        strategy_id=strategy_id,
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal("100"),
        updated_at=1_000,
    )


async def _seed_positions(redis: fakeredis.aioredis.FakeRedis) -> None:
    """Pre-populate the fake redis with two strategies + three positions."""
    store = PositionStore(redis)
    await store.put(_position(strategy_id="strat_a", symbol="BTC-USD", qty="1"))
    await store.put(_position(strategy_id="strat_a", symbol="ETH-USD", qty="0"))
    await store.put(_position(strategy_id="strat_b", symbol="BTC-USD", qty="-2"))


async def test_positions_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/positions")
    assert response.status_code == 401


async def test_positions_lists_open_positions_across_strategies(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed_positions(fake_redis)
    response = await client.get("/positions", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    # Default include_flat=False excludes the ETH-USD zero position.
    pairs = sorted({(p["strategy_id"], p["symbol"]) for p in body})
    assert pairs == [("strat_a", "BTC-USD"), ("strat_b", "BTC-USD")]


async def test_positions_include_flat_returns_all(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed_positions(fake_redis)
    response = await client.get(
        "/positions", headers=auth_headers, params={"include_flat": "true"}
    )
    assert response.status_code == 200
    assert len(response.json()) == 3


async def test_positions_filtered_by_strategy(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed_positions(fake_redis)
    response = await client.get("/positions/strat_a", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert {p["symbol"] for p in body} == {"BTC-USD"}  # ETH excluded (flat)


async def test_positions_for_unknown_strategy_returns_empty(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed_positions(fake_redis)
    response = await client.get("/positions/never_existed", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []
