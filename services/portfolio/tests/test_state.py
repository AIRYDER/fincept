"""Tests for portfolio.state — in-memory state + apply_fill helper."""

from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
import pytest

from fincept_core.schemas import Fill, Position, Side
from portfolio.state import PortfolioState, apply_fill
from portfolio.store import PositionStore


def _fill(
    *,
    side: Side = Side.BUY,
    qty: str = "1",
    price: str = "100",
    order_id: str = "o1",
    ts: int = 1_000,
    symbol: str = "BTC-USD",
) -> Fill:
    return Fill(
        fill_id=f"f-{ts}",
        order_id=order_id,
        ts_event=ts,
        symbol=symbol,
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        fee=Decimal("0.05"),
    )


def _const_resolver(strategy_id: str | None) -> object:
    async def resolver(_fill: Fill) -> str | None:
        return strategy_id

    return resolver


@pytest.fixture
def store() -> PositionStore:
    return PositionStore(fakeredis.aioredis.FakeRedis())


# ---------------------------------------------------------------------------
# PortfolioState basic API
# ---------------------------------------------------------------------------


def test_state_returns_none_for_unknown_position() -> None:
    state = PortfolioState()
    assert state.get("strat", "BTC-USD") is None


def test_state_record_then_get_round_trips() -> None:
    state = PortfolioState()
    pos = Position(
        strategy_id="strat",
        symbol="BTC-USD",
        quantity=Decimal("1"),
        avg_cost=Decimal("100"),
        updated_at=1_000,
    )
    state.record(pos)
    assert state.get("strat", "BTC-USD") == pos


def test_state_known_strategies_starts_empty() -> None:
    assert PortfolioState().known_strategies() == set()


# ---------------------------------------------------------------------------
# apply_fill
# ---------------------------------------------------------------------------


async def test_apply_fill_creates_first_position(store: PositionStore) -> None:
    state = PortfolioState()
    pos = await apply_fill(
        _fill(side=Side.BUY),
        state=state,
        store=store,
        resolve_strategy=_const_resolver("strat"),
    )
    assert pos is not None
    assert pos.strategy_id == "strat"
    assert pos.quantity == Decimal("1")
    # Mirrored to the store.
    assert await store.get("strat", "BTC-USD") == pos


async def test_apply_fill_evolves_state_across_two_fills(store: PositionStore) -> None:
    state = PortfolioState()
    await apply_fill(
        _fill(side=Side.BUY, price="100"),
        state=state,
        store=store,
        resolve_strategy=_const_resolver("strat"),
    )
    pos = await apply_fill(
        _fill(side=Side.BUY, price="200", order_id="o2"),
        state=state,
        store=store,
        resolve_strategy=_const_resolver("strat"),
    )
    assert pos is not None
    assert pos.quantity == Decimal("2")
    assert pos.avg_cost == Decimal("150")  # weighted: (100 + 200) / 2


async def test_apply_fill_returns_none_when_resolver_cannot_attribute(
    store: PositionStore,
) -> None:
    state = PortfolioState()
    pos = await apply_fill(
        _fill(),
        state=state,
        store=store,
        resolve_strategy=_const_resolver(None),
    )
    assert pos is None
    # Nothing should have been written to the store.
    assert await store.get_all("strat") == {}


async def test_apply_fill_isolates_strategies(store: PositionStore) -> None:
    """Same Fill values, different strategies -> two independent positions."""
    state = PortfolioState()
    await apply_fill(
        _fill(side=Side.BUY),
        state=state,
        store=store,
        resolve_strategy=_const_resolver("strat_a"),
    )
    await apply_fill(
        _fill(side=Side.SELL, order_id="o2"),
        state=state,
        store=store,
        resolve_strategy=_const_resolver("strat_b"),
    )
    a = state.get("strat_a", "BTC-USD")
    b = state.get("strat_b", "BTC-USD")
    assert a is not None and a.quantity == Decimal("1")  # buy
    assert b is not None and b.quantity == Decimal("-1")  # sell


# ---------------------------------------------------------------------------
# Hydration on restart
# ---------------------------------------------------------------------------


async def test_hydrate_loads_existing_positions_from_store(store: PositionStore) -> None:
    """A restarted PortfolioState picks up where the previous instance left off."""
    pos = Position(
        strategy_id="strat",
        symbol="BTC-USD",
        quantity=Decimal("3"),
        avg_cost=Decimal("110"),
        updated_at=1_000,
    )
    await store.put(pos)

    fresh_state = PortfolioState()
    await fresh_state.hydrate(store)
    assert fresh_state.get("strat", "BTC-USD") == pos
    assert fresh_state.known_strategies() == {"strat"}
