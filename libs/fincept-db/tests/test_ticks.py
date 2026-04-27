from __future__ import annotations

from decimal import Decimal

import pytest

from fincept_core.schemas import (
    AssetClass,
    BookDeltaEvent,
    BookLevel,
    Side,
    TradeEvent,
    Venue,
)
from fincept_db.ticks import (
    read_book_deltas,
    read_trades,
    write_book_deltas,
    write_trades,
)


def _trade(ts: int, seq: int = 0, price: str = "100.5", size: str = "0.1") -> TradeEvent:
    return TradeEvent(
        venue=Venue.BINANCE,
        symbol="BTC-USD",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts,
        ts_recv=ts + 1,
        seq=seq,
        price=Decimal(price),
        size=Decimal(size),
        side=Side.BUY,
    )


@pytest.mark.asyncio
async def test_write_and_read_trades_roundtrip() -> None:
    events = [_trade(ts) for ts in (1_000_000_000, 2_000_000_000, 3_000_000_000)]
    written = await write_trades(events)
    assert written == 3

    out = await read_trades("BTC-USD", 0, 4_000_000_000)
    assert len(out) == 3
    assert [event.ts_event for event in out] == [1_000_000_000, 2_000_000_000, 3_000_000_000]
    assert out[0].price == Decimal("100.5")
    assert out[0].size == Decimal("0.1")
    assert out[0].side == Side.BUY


@pytest.mark.asyncio
async def test_write_trades_idempotent_on_primary_key() -> None:
    event = _trade(ts=10, seq=1, price="2000", size="0.5")
    first = await write_trades([event])
    second = await write_trades([event])
    assert first == 1
    assert second == 0


@pytest.mark.asyncio
async def test_read_trades_filters_by_venue_and_window() -> None:
    binance = _trade(ts=100)
    coinbase = TradeEvent(
        venue=Venue.COINBASE,
        symbol="BTC-USD",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=200,
        ts_recv=201,
        seq=0,
        price=Decimal("99"),
        size=Decimal("0.2"),
    )
    await write_trades([binance, coinbase])

    binance_only = await read_trades("BTC-USD", 0, 1_000, venue="binance")
    assert {event.venue for event in binance_only} == {Venue.BINANCE}

    windowed = await read_trades("BTC-USD", 150, 250)
    assert [event.ts_event for event in windowed] == [200]


@pytest.mark.asyncio
async def test_book_delta_roundtrip_preserves_decimal_precision() -> None:
    event = BookDeltaEvent(
        venue=Venue.BINANCE,
        symbol="BTC-USD",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=500,
        ts_recv=501,
        seq=1,
        bids_add=[BookLevel(price=Decimal("100.000000000001"), size=Decimal("0.5"))],
        bids_remove=[Decimal("99.123456789012")],
        asks_add=[BookLevel(price=Decimal("101.5"), size=Decimal("0.25"))],
        asks_remove=[],
    )
    await write_book_deltas([event])

    out = await read_book_deltas("BTC-USD", 0, 1_000)
    assert len(out) == 1
    restored = out[0]
    assert restored.bids_add[0].price == Decimal("100.000000000001")
    assert restored.bids_remove[0] == Decimal("99.123456789012")
    assert restored.asks_add[0].size == Decimal("0.25")
