from __future__ import annotations

from decimal import Decimal

import pytest

from fincept_core.schemas import AssetClass, BarEvent, Venue
from fincept_db.bars import read_bars, write_bars


def _bar(ts: int, freq: str = "1m", close: str = "101") -> BarEvent:
    return BarEvent(
        venue=Venue.BINANCE,
        symbol="BTC-USD",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts,
        ts_recv=ts,
        freq=freq,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=Decimal("10.5"),
        trades=42,
        vwap=Decimal("100.7"),
    )


@pytest.mark.asyncio
async def test_write_and_read_bars_roundtrip() -> None:
    bars = [_bar(ts) for ts in (1_000, 2_000, 3_000)]
    written = await write_bars(bars)
    assert written == 3

    out = await read_bars("BTC-USD", "1m", 0, 4_000)
    assert [bar.ts_event for bar in out] == [1_000, 2_000, 3_000]
    assert out[0].close == Decimal("101")
    assert out[0].vwap == Decimal("100.7")
    assert out[0].trades == 42


@pytest.mark.asyncio
async def test_write_bars_upserts_on_primary_key() -> None:
    original = _bar(ts=500, close="100")
    await write_bars([original])

    revised = _bar(ts=500, close="105")
    await write_bars([revised])

    out = await read_bars("BTC-USD", "1m", 0, 1_000)
    assert len(out) == 1
    assert out[0].close == Decimal("105")


@pytest.mark.asyncio
async def test_read_bars_filters_by_freq() -> None:
    await write_bars([_bar(ts=100, freq="1m"), _bar(ts=200, freq="1h")])

    minute = await read_bars("BTC-USD", "1m", 0, 1_000)
    hour = await read_bars("BTC-USD", "1h", 0, 1_000)

    assert [bar.ts_event for bar in minute] == [100]
    assert [bar.ts_event for bar in hour] == [200]
