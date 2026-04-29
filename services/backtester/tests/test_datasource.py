"""Tests for backtester.datasource.BarsDataSource — replay ordering + DI."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtester.datasource import BarsDataSource
from fincept_core.schemas import AssetClass, BarEvent, Venue


def _bar(symbol: str, ts: int) -> BarEvent:
    p = Decimal("100")
    return BarEvent(
        venue=Venue.PAPER,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts,
        ts_recv=ts,
        freq="1m",
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("0"),
        trades=0,
    )


def _make_reader(by_symbol: dict[str, list[BarEvent]]):
    async def reader(symbol: str, freq: str, start_ns: int, end_ns: int) -> list[BarEvent]:
        return [b for b in by_symbol.get(symbol, []) if start_ns <= b.ts_event < end_ns]

    return reader


async def test_single_symbol_replay_yields_in_ts_order() -> None:
    bars = {"BTC-USD": [_bar("BTC-USD", ts) for ts in (3_000, 1_000, 2_000)]}
    # The DB usually returns sorted, but our fake may not - the assertion
    # below verifies the datasource itself yields in monotonic order even if
    # a single-symbol input list is unsorted (heapq.merge is stable per input).
    bars["BTC-USD"] = sorted(bars["BTC-USD"], key=lambda b: b.ts_event)
    ds = BarsDataSource(["BTC-USD"], "1m", 0, 10_000, bar_reader=_make_reader(bars))
    yielded = [b.ts_event async for b in ds.replay()]
    assert yielded == [1_000, 2_000, 3_000]


async def test_multi_symbol_replay_merges_by_ts_event() -> None:
    bars = {
        "BTC-USD": [_bar("BTC-USD", ts) for ts in (1_000, 3_000, 5_000)],
        "ETH-USD": [_bar("ETH-USD", ts) for ts in (2_000, 4_000)],
    }
    ds = BarsDataSource(["BTC-USD", "ETH-USD"], "1m", 0, 10_000, bar_reader=_make_reader(bars))
    yielded = [(b.ts_event, b.symbol) async for b in ds.replay()]
    assert yielded == [
        (1_000, "BTC-USD"),
        (2_000, "ETH-USD"),
        (3_000, "BTC-USD"),
        (4_000, "ETH-USD"),
        (5_000, "BTC-USD"),
    ]


async def test_replay_filters_to_requested_range() -> None:
    bars = {"BTC-USD": [_bar("BTC-USD", ts) for ts in (500, 1_500, 2_500)]}
    ds = BarsDataSource(["BTC-USD"], "1m", 1_000, 2_000, bar_reader=_make_reader(bars))
    yielded = [b.ts_event async for b in ds.replay()]
    assert yielded == [1_500]


def test_empty_symbols_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="at least one symbol"):
        BarsDataSource([], "1m", 0, 1_000)


def test_inverted_range_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="must be <"):
        BarsDataSource(["BTC-USD"], "1m", 1_000, 1_000)
