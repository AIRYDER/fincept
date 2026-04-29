"""Tests for ingestor.coinbase — Coinbase envelope → canonical event parsing."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from fincept_core.schemas import (
    AssetClass,
    BookDeltaEvent,
    BookSnapshotEvent,
    Side,
    TradeEvent,
    Venue,
)
from ingestor.coinbase import CoinbaseAdapter

# ---------------------------------------------------------------------------
# market_trades
# ---------------------------------------------------------------------------


def test_parse_trades_basic() -> None:
    msg = {
        "channel": "market_trades",
        "events": [
            {
                "trades": [
                    {
                        "trade_id": "12345",
                        "product_id": "BTC-USD",
                        "price": "100000.50",
                        "size": "0.123",
                        "side": "BUY",
                        "time": "2024-12-01T12:00:00.123456Z",
                    }
                ]
            }
        ],
    }
    events = CoinbaseAdapter._parse_envelope(msg, ts_recv=1_700_000_000_001_000_000)
    assert len(events) == 1
    trade = events[0]
    assert isinstance(trade, TradeEvent)
    assert trade.venue == Venue.COINBASE
    assert trade.symbol == "BTC-USD"
    assert trade.asset_class == AssetClass.CRYPTO_SPOT
    assert trade.seq == 12345
    assert trade.price == Decimal("100000.50")
    assert trade.size == Decimal("0.123")
    assert trade.side == Side.BUY
    # ISO timestamp: 2024-12-01T12:00:00.123456Z → 1733054400123456000 ns.
    assert trade.ts_event == 1_733_054_400_123_456_000
    assert trade.ts_recv == 1_700_000_000_001_000_000


def test_parse_trades_sell_side_is_lowercase_tolerant() -> None:
    msg = {
        "channel": "market_trades",
        "events": [
            {
                "trades": [
                    {
                        "trade_id": "1",
                        "product_id": "ETH-USD",
                        "price": "1800",
                        "size": "0.5",
                        "side": "sell",
                        "time": "2024-12-01T12:00:00Z",
                    }
                ]
            }
        ],
    }
    events = CoinbaseAdapter._parse_envelope(msg, ts_recv=0)
    assert len(events) == 1
    trade = events[0]
    assert isinstance(trade, TradeEvent)
    assert trade.side == Side.SELL


def test_parse_trades_multiple_in_one_event() -> None:
    msg = {
        "channel": "market_trades",
        "events": [
            {
                "trades": [
                    {
                        "trade_id": "1",
                        "product_id": "BTC-USD",
                        "price": "100",
                        "size": "1",
                        "side": "BUY",
                        "time": "2024-12-01T12:00:00Z",
                    },
                    {
                        "trade_id": "2",
                        "product_id": "BTC-USD",
                        "price": "101",
                        "size": "1",
                        "side": "SELL",
                        "time": "2024-12-01T12:00:01Z",
                    },
                ]
            }
        ],
    }
    events = CoinbaseAdapter._parse_envelope(msg, ts_recv=0)
    assert len(events) == 2


# ---------------------------------------------------------------------------
# l2_data snapshot
# ---------------------------------------------------------------------------


def test_parse_l2_snapshot() -> None:
    msg = {
        "channel": "l2_data",
        "timestamp": "2024-12-01T12:00:00Z",
        "events": [
            {
                "type": "snapshot",
                "product_id": "BTC-USD",
                "updates": [
                    {"side": "bid", "price_level": "100000", "new_quantity": "1.0"},
                    {"side": "bid", "price_level": "99999", "new_quantity": "2.0"},
                    {"side": "offer", "price_level": "100100", "new_quantity": "0.5"},
                ],
            }
        ],
    }
    events = CoinbaseAdapter._parse_envelope(msg, ts_recv=1_700_000_000_000_000_000)
    assert len(events) == 1
    snap = events[0]
    assert isinstance(snap, BookSnapshotEvent)
    assert snap.symbol == "BTC-USD"
    assert len(snap.bids) == 2
    assert len(snap.asks) == 1
    assert snap.bids[0].price == Decimal("100000")
    assert snap.bids[0].size == Decimal("1.0")
    assert snap.asks[0].price == Decimal("100100")


# ---------------------------------------------------------------------------
# l2_data update (delta)
# ---------------------------------------------------------------------------


def test_parse_l2_update_splits_upserts_and_removals() -> None:
    msg = {
        "channel": "l2_data",
        "timestamp": "2024-12-01T12:00:00Z",
        "events": [
            {
                "type": "update",
                "product_id": "BTC-USD",
                "updates": [
                    {"side": "bid", "price_level": "100000", "new_quantity": "2.0"},
                    {"side": "bid", "price_level": "99999", "new_quantity": "0"},
                    {"side": "offer", "price_level": "100100", "new_quantity": "1.0"},
                    {"side": "offer", "price_level": "100200", "new_quantity": "0"},
                ],
            }
        ],
    }
    events = CoinbaseAdapter._parse_envelope(msg, ts_recv=0)
    assert len(events) == 1
    delta = events[0]
    assert isinstance(delta, BookDeltaEvent)
    assert len(delta.bids_add) == 1 and delta.bids_add[0].price == Decimal("100000")
    assert delta.bids_remove == [Decimal("99999")]
    assert len(delta.asks_add) == 1 and delta.asks_add[0].price == Decimal("100100")
    assert delta.asks_remove == [Decimal("100200")]


def test_parse_l2_falls_back_to_envelope_timestamp() -> None:
    """When the inner event has no ``time`` field, use the outer ``timestamp``."""
    msg = {
        "channel": "l2_data",
        "timestamp": "2024-12-01T12:00:00Z",
        "events": [
            {
                "type": "update",
                "product_id": "BTC-USD",
                "updates": [{"side": "bid", "price_level": "100", "new_quantity": "1"}],
            }
        ],
    }
    events = CoinbaseAdapter._parse_envelope(msg, ts_recv=0)
    assert len(events) == 1
    delta = events[0]
    assert isinstance(delta, BookDeltaEvent)
    # 2024-12-01T12:00:00Z → 1733054400000000000 ns.
    assert delta.ts_event == 1_733_054_400_000_000_000


# ---------------------------------------------------------------------------
# unknown / heartbeat
# ---------------------------------------------------------------------------


def test_parse_envelope_unknown_channel_returns_empty() -> None:
    assert CoinbaseAdapter._parse_envelope({"channel": "heartbeats"}, ts_recv=0) == []
    assert CoinbaseAdapter._parse_envelope({}, ts_recv=0) == []


def test_parse_l2_ignores_unknown_event_type() -> None:
    msg = {
        "channel": "l2_data",
        "timestamp": "2024-12-01T12:00:00Z",
        "events": [{"type": "mystery", "product_id": "BTC-USD", "updates": []}],
    }
    assert CoinbaseAdapter._parse_envelope(msg, ts_recv=0) == []


# ---------------------------------------------------------------------------
# adapter lifecycle
# ---------------------------------------------------------------------------


def test_adapter_requires_at_least_one_symbol() -> None:
    adapter = CoinbaseAdapter([])
    with pytest.raises(ValueError, match="at least one symbol"):
        asyncio.run(adapter.connect())


def test_stream_before_connect_raises() -> None:
    adapter = CoinbaseAdapter(["BTC-USD"])

    async def consume() -> None:
        async for _ in adapter.stream():
            break

    with pytest.raises(RuntimeError, match="connect"):
        asyncio.run(consume())
