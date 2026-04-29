"""Tests for /strategies endpoint."""

from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
from httpx import AsyncClient

from fincept_core.schemas import Position
from portfolio.store import PositionStore


def _pos(strategy_id: str, symbol: str, qty: str) -> Position:
    return Position(
        strategy_id=strategy_id,
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal("100"),
        updated_at=1_000,
    )


async def _seed(redis: fakeredis.aioredis.FakeRedis) -> None:
    store = PositionStore(redis)
    await store.put(_pos("strat_a", "BTC-USD", "1"))
    await store.put(_pos("strat_a", "ETH-USD", "0"))
    await store.put(_pos("strat_b", "BTC-USD", "-1"))


async def test_strategies_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/strategies")
    assert response.status_code == 401


async def test_strategies_returns_known_with_counts(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await _seed(fake_redis)
    response = await client.get("/strategies", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    by_id = {s["strategy_id"]: s for s in body}
    assert set(by_id) == {"strat_a", "strat_b"}
    assert by_id["strat_a"]["position_count"] == 2  # BTC + ETH (incl flat)
    assert by_id["strat_a"]["open_positions"] == 1  # only BTC is non-zero
    assert by_id["strat_b"]["position_count"] == 1
    assert by_id["strat_b"]["open_positions"] == 1


async def test_strategies_empty_when_no_state(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/strategies", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []
