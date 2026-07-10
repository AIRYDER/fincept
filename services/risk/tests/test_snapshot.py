"""Tests for risk.snapshot.build_context."""

from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
import pytest_asyncio
from fincept_core.schemas import Position
from portfolio.store import PositionStore
from redis.asyncio import Redis

from risk.snapshot import build_context
from risk.state import KillSwitchState


@pytest_asyncio.fixture
async def redis() -> Redis:  # type: ignore[type-arg]
    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def store(redis: Redis) -> PositionStore:  # type: ignore[type-arg]
    return PositionStore(redis)


def _position(
    *, strategy_id: str, symbol: str, qty: str, avg_cost: str = "100"
) -> Position:
    return Position(
        strategy_id=strategy_id,
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal(avg_cost),
        updated_at=1_000,
    )


# ---------------------------------------------------------------------------
# Empty store
# ---------------------------------------------------------------------------


async def test_empty_store_returns_zero_context(store: PositionStore) -> None:
    ctx = await build_context(
        store=store,
        get_price=lambda _s: Decimal("100"),
        kill_switch=KillSwitchState(),
    )
    assert ctx.notional_by_symbol == {}
    assert ctx.gross_notional == Decimal(0)
    assert ctx.kill_switch_engaged is False


# ---------------------------------------------------------------------------
# Single strategy
# ---------------------------------------------------------------------------


async def test_single_strategy_single_symbol(store: PositionStore) -> None:
    await store.put(_position(strategy_id="s1", symbol="BTC-USD", qty="2"))

    prices = {"BTC-USD": Decimal("50_000")}
    ctx = await build_context(
        store=store,
        get_price=prices.get,
        kill_switch=KillSwitchState(),
    )
    # 2 BTC * 50k = 100k notional
    assert ctx.notional_by_symbol == {"BTC-USD": Decimal("100_000")}
    assert ctx.gross_notional == Decimal("100_000")


async def test_single_strategy_multiple_symbols_sums_per_symbol(
    store: PositionStore,
) -> None:
    await store.put(_position(strategy_id="s1", symbol="BTC-USD", qty="1"))
    await store.put(_position(strategy_id="s1", symbol="ETH-USD", qty="10"))

    prices = {"BTC-USD": Decimal("50_000"), "ETH-USD": Decimal("3_000")}
    ctx = await build_context(
        store=store,
        get_price=prices.get,
        kill_switch=KillSwitchState(),
    )
    assert ctx.notional_by_symbol == {
        "BTC-USD": Decimal("50_000"),
        "ETH-USD": Decimal("30_000"),
    }
    assert ctx.gross_notional == Decimal("80_000")


# ---------------------------------------------------------------------------
# Multi-strategy aggregation
# ---------------------------------------------------------------------------


async def test_multi_strategy_per_symbol_notional_aggregates(
    store: PositionStore,
) -> None:
    """Two strategies both holding BTC-USD: per-symbol notional is the sum."""
    await store.put(_position(strategy_id="s1", symbol="BTC-USD", qty="1"))
    await store.put(_position(strategy_id="s2", symbol="BTC-USD", qty="2"))

    prices = {"BTC-USD": Decimal("50_000")}
    ctx = await build_context(
        store=store,
        get_price=prices.get,
        kill_switch=KillSwitchState(),
    )
    # Total |qty| = 3 BTC; 3 * 50k = 150k.
    assert ctx.notional_by_symbol == {"BTC-USD": Decimal("150_000")}
    assert ctx.gross_notional == Decimal("150_000")


# ---------------------------------------------------------------------------
# Sign handling
# ---------------------------------------------------------------------------


async def test_negative_position_uses_absolute_notional(store: PositionStore) -> None:
    """A short of 2 BTC at 50k is still 100k of risk."""
    await store.put(_position(strategy_id="s1", symbol="BTC-USD", qty="-2"))

    prices = {"BTC-USD": Decimal("50_000")}
    ctx = await build_context(
        store=store,
        get_price=prices.get,
        kill_switch=KillSwitchState(),
    )
    assert ctx.notional_by_symbol == {"BTC-USD": Decimal("100_000")}


async def test_zero_position_excluded(store: PositionStore) -> None:
    """A flat position contributes nothing."""
    await store.put(_position(strategy_id="s1", symbol="BTC-USD", qty="0"))

    prices = {"BTC-USD": Decimal("50_000")}
    ctx = await build_context(
        store=store,
        get_price=prices.get,
        kill_switch=KillSwitchState(),
    )
    assert ctx.notional_by_symbol == {}
    assert ctx.gross_notional == Decimal(0)


# ---------------------------------------------------------------------------
# Missing prices
# ---------------------------------------------------------------------------


async def test_missing_price_skips_position_silently(store: PositionStore) -> None:
    """Symbol with no price observation is dropped from gross / per-symbol
    totals.  Conservative choice (under-report exposure rather than use
    stale prices); intents on such symbols still get rejected by
    check_intent for lacking a reference price."""
    await store.put(_position(strategy_id="s1", symbol="BTC-USD", qty="1"))
    await store.put(_position(strategy_id="s1", symbol="DOGE-USD", qty="100"))

    prices = {"BTC-USD": Decimal("50_000")}  # DOGE-USD missing
    ctx = await build_context(
        store=store,
        get_price=prices.get,
        kill_switch=KillSwitchState(),
    )
    assert ctx.notional_by_symbol == {"BTC-USD": Decimal("50_000")}
    assert ctx.gross_notional == Decimal("50_000")


# ---------------------------------------------------------------------------
# Kill switch propagation
# ---------------------------------------------------------------------------


async def test_kill_switch_engaged_propagates_to_context(store: PositionStore) -> None:
    kill = KillSwitchState()
    from fincept_core.schemas import AlertEvent

    kill.apply(
        AlertEvent(
            alert_id="a1",
            ts_event=1_000,
            severity="critical",
            source="api.control",
            code="kill_switch_engaged",
            message="test",
        )
    )

    ctx = await build_context(
        store=store,
        get_price=lambda _s: Decimal("100"),
        kill_switch=kill,
    )
    assert ctx.kill_switch_engaged is True


# ---------------------------------------------------------------------------
# Strategy filter
# ---------------------------------------------------------------------------


async def test_strategies_filter_scopes_snapshot(store: PositionStore) -> None:
    """Pass an explicit strategy list to scope the context."""
    await store.put(_position(strategy_id="s1", symbol="BTC-USD", qty="1"))
    await store.put(_position(strategy_id="s2", symbol="BTC-USD", qty="10"))

    prices = {"BTC-USD": Decimal("50_000")}
    ctx = await build_context(
        store=store,
        get_price=prices.get,
        kill_switch=KillSwitchState(),
        strategies=["s1"],  # only s1
    )
    assert ctx.notional_by_symbol == {"BTC-USD": Decimal("50_000")}  # not 550k
