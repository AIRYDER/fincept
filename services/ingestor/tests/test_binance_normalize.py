"""Tests for ingestor.binance — message → canonical event normalization.

These tests exercise ``BinanceAdapter._parse_event`` directly so they
require neither a live WebSocket nor the ``websockets`` runtime.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fincept_core.schemas import (
    AssetClass,
    BookDeltaEvent,
    Side,
    TradeEvent,
    Venue,
)
from ingestor.binance import BinanceAdapter

# ---------------------------------------------------------------------------
# trade
# ---------------------------------------------------------------------------


def test_parse_trade_buy_side_when_buyer_is_taker() -> None:
    payload = {
        "e": "trade",
        "s": "BTCUSDT",
        "T": 1_700_000_000_000,  # ms
        "t": 12345,
        "p": "30000.50",
        "q": "0.01",
        "m": False,  # buyer is the TAKER → BUY
    }
    event = BinanceAdapter._parse_event(payload, ts_recv=1_700_000_000_001_000_000)
    assert isinstance(event, TradeEvent)
    assert event.venue == Venue.BINANCE
    assert event.symbol == "BTC-USDT"
    assert event.asset_class == AssetClass.CRYPTO_SPOT
    assert event.ts_event == 1_700_000_000_000_000_000  # ms → ns
    assert event.ts_recv == 1_700_000_000_001_000_000
    assert event.seq == 12345
    assert event.price == Decimal("30000.50")
    assert event.size == Decimal("0.01")
    assert event.side == Side.BUY


def test_parse_trade_sell_side_when_buyer_is_maker() -> None:
    payload = {
        "e": "trade",
        "s": "ETHUSDT",
        "T": 1_700_000_001_000,
        "t": 1,
        "p": "1800",
        "q": "0.5",
        "m": True,  # buyer is the MAKER → taker SOLD
    }
    event = BinanceAdapter._parse_event(payload, ts_recv=0)
    assert isinstance(event, TradeEvent)
    assert event.side == Side.SELL


def test_parse_trade_seq_zero_becomes_none() -> None:
    """Binance occasionally publishes ``t=0`` as a placeholder; normalise to None."""
    payload = {
        "e": "trade",
        "s": "BTCUSDT",
        "T": 1_700_000_000_000,
        "t": 0,
        "p": "30000",
        "q": "0.01",
        "m": False,
    }
    event = BinanceAdapter._parse_event(payload, ts_recv=0)
    assert isinstance(event, TradeEvent)
    assert event.seq is None


# ---------------------------------------------------------------------------
# depth update
# ---------------------------------------------------------------------------


def test_parse_depth_update_splits_adds_and_removes() -> None:
    payload = {
        "e": "depthUpdate",
        "s": "BTCUSDT",
        "E": 1_700_000_000_000,
        "u": 4242,
        "b": [
            ["30000", "0.5"],
            ["29999", "0"],  # removal
            ["29998", "1.2"],
        ],
        "a": [
            ["30001", "0.3"],
            ["30002", "0"],  # removal
        ],
    }
    event = BinanceAdapter._parse_event(payload, ts_recv=1_700_000_000_001_000_000)
    assert isinstance(event, BookDeltaEvent)
    assert event.symbol == "BTC-USDT"
    assert event.seq == 4242
    assert len(event.bids_add) == 2
    assert event.bids_add[0].price == Decimal("30000")
    assert event.bids_add[0].size == Decimal("0.5")
    assert event.bids_remove == [Decimal("29999")]
    assert len(event.asks_add) == 1
    assert event.asks_remove == [Decimal("30002")]


def test_parse_depth_update_empty_book_levels() -> None:
    payload = {
        "e": "depthUpdate",
        "s": "BTCUSDT",
        "E": 1_700_000_000_000,
        "u": 1,
        "b": [],
        "a": [],
    }
    event = BinanceAdapter._parse_event(payload, ts_recv=0)
    assert isinstance(event, BookDeltaEvent)
    assert event.bids_add == []
    assert event.bids_remove == []
    assert event.asks_add == []
    assert event.asks_remove == []


# ---------------------------------------------------------------------------
# unknown / heartbeat
# ---------------------------------------------------------------------------


def test_parse_event_returns_none_for_unknown_type() -> None:
    """Heartbeats and unrecognised event types must be silently skipped."""
    assert BinanceAdapter._parse_event({"e": "kline"}, ts_recv=0) is None
    assert BinanceAdapter._parse_event({}, ts_recv=0) is None


# ---------------------------------------------------------------------------
# adapter lifecycle
# ---------------------------------------------------------------------------


def test_adapter_requires_at_least_one_symbol() -> None:
    adapter = BinanceAdapter([])
    import asyncio

    with pytest.raises(ValueError, match="at least one symbol"):
        asyncio.run(adapter.connect())


def test_stream_before_connect_raises() -> None:
    adapter = BinanceAdapter(["BTC-USDT"])
    import asyncio

    async def consume() -> None:
        async for _ in adapter.stream():
            break

    with pytest.raises(RuntimeError, match="connect"):
        asyncio.run(consume())
