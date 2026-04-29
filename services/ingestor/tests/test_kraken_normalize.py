"""Tests for ingestor.kraken — Kraken v2 envelope → canonical event parsing."""

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
from ingestor.kraken import KrakenAdapter
from ingestor.normalizer import from_kraken_symbol, to_kraken_symbol

# ---------------------------------------------------------------------------
# symbol mapping (BTC ↔ XBT)
# ---------------------------------------------------------------------------


def test_xbt_to_btc_roundtrip() -> None:
    """Canonical → Kraken → Canonical must be lossless for the BTC pair."""
    assert to_kraken_symbol("BTC-USD") == "XBT/USD"
    assert from_kraken_symbol("XBT/USD") == "BTC-USD"
    assert from_kraken_symbol(to_kraken_symbol("BTC-USD")) == "BTC-USD"


def test_non_btc_pairs_pass_through_separator_only() -> None:
    """Pairs without BTC must only flip ``-`` ↔ ``/``."""
    assert to_kraken_symbol("ETH-USD") == "ETH/USD"
    assert from_kraken_symbol("ETH/USD") == "ETH-USD"


# ---------------------------------------------------------------------------
# trade
# ---------------------------------------------------------------------------


def test_parse_trade_basic_buy() -> None:
    msg = {
        "channel": "trade",
        "type": "update",
        "data": [
            {
                "symbol": "XBT/USD",
                "side": "buy",
                "price": 100000.5,  # Kraken sends a JSON number, not a string.
                "qty": 0.123,
                "ord_type": "market",
                "trade_id": 12345,
                "timestamp": "2024-12-01T12:00:00.123456Z",
            }
        ],
    }
    events = KrakenAdapter._parse_envelope(msg, ts_recv=1_700_000_000_001_000_000)
    assert len(events) == 1
    trade = events[0]
    assert isinstance(trade, TradeEvent)
    assert trade.venue == Venue.KRAKEN
    assert trade.symbol == "BTC-USD"  # XBT → BTC
    assert trade.asset_class == AssetClass.CRYPTO_SPOT
    assert trade.seq == 12345
    # Critical: Decimal must not have float drift artefacts.
    assert trade.price == Decimal("100000.5")
    assert trade.size == Decimal("0.123")
    assert trade.side == Side.BUY
    # ISO timestamp 2024-12-01T12:00:00.123456Z → 1_733_054_400_123_456_000 ns.
    assert trade.ts_event == 1_733_054_400_123_456_000


def test_parse_trade_sell_and_string_prices() -> None:
    """Tolerate uppercase 'SELL' and string-typed price/qty (defensive)."""
    msg = {
        "channel": "trade",
        "type": "snapshot",
        "data": [
            {
                "symbol": "ETH/USD",
                "side": "SELL",
                "price": "1800.00",
                "qty": "0.5",
                "trade_id": 1,
                "timestamp": "2024-12-01T12:00:00Z",
            }
        ],
    }
    events = KrakenAdapter._parse_envelope(msg, ts_recv=0)
    assert len(events) == 1
    trade = events[0]
    assert isinstance(trade, TradeEvent)
    assert trade.side == Side.SELL
    assert trade.symbol == "ETH-USD"
    assert trade.price == Decimal("1800.00")


def test_parse_trade_missing_trade_id_yields_none_seq() -> None:
    msg = {
        "channel": "trade",
        "type": "update",
        "data": [
            {
                "symbol": "XBT/USD",
                "side": "buy",
                "price": 100,
                "qty": 1,
                "timestamp": "2024-12-01T12:00:00Z",
            }
        ],
    }
    events = KrakenAdapter._parse_envelope(msg, ts_recv=0)
    assert len(events) == 1
    trade = events[0]
    assert isinstance(trade, TradeEvent)
    assert trade.seq is None


def test_parse_trade_avoids_float_drift_on_decimals() -> None:
    """Decimal(str(0.1)) is exact; Decimal(0.1) is not.  Pin the contract."""
    msg = {
        "channel": "trade",
        "type": "update",
        "data": [
            {
                "symbol": "XBT/USD",
                "side": "buy",
                "price": 0.1,
                "qty": 0.2,
                "trade_id": 1,
                "timestamp": "2024-12-01T12:00:00Z",
            }
        ],
    }
    events = KrakenAdapter._parse_envelope(msg, ts_recv=0)
    trade = events[0]
    assert isinstance(trade, TradeEvent)
    assert trade.price == Decimal("0.1")
    assert trade.size == Decimal("0.2")


# ---------------------------------------------------------------------------
# book snapshot
# ---------------------------------------------------------------------------


def test_parse_book_snapshot() -> None:
    msg = {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "XBT/USD",
                "bids": [
                    {"price": 100000, "qty": 1.0},
                    {"price": 99999, "qty": 2.0},
                ],
                "asks": [
                    {"price": 100100, "qty": 0.5},
                ],
                "timestamp": "2024-12-01T12:00:00Z",
            }
        ],
    }
    events = KrakenAdapter._parse_envelope(msg, ts_recv=1_700_000_000_000_000_000)
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
# book update (delta)
# ---------------------------------------------------------------------------


def test_parse_book_update_splits_upserts_and_removals() -> None:
    msg = {
        "channel": "book",
        "type": "update",
        "data": [
            {
                "symbol": "XBT/USD",
                "bids": [
                    {"price": 100000, "qty": 2.0},
                    {"price": 99999, "qty": 0},  # removal
                ],
                "asks": [
                    {"price": 100100, "qty": 1.0},
                    {"price": 100200, "qty": 0},  # removal
                ],
                "timestamp": "2024-12-01T12:00:00Z",
            }
        ],
    }
    events = KrakenAdapter._parse_envelope(msg, ts_recv=0)
    assert len(events) == 1
    delta = events[0]
    assert isinstance(delta, BookDeltaEvent)
    assert len(delta.bids_add) == 1
    assert delta.bids_add[0].price == Decimal("100000")
    assert delta.bids_remove == [Decimal("99999")]
    assert len(delta.asks_add) == 1
    assert delta.asks_add[0].price == Decimal("100100")
    assert delta.asks_remove == [Decimal("100200")]


def test_parse_book_update_falls_back_to_ts_recv_when_no_timestamp() -> None:
    msg = {
        "channel": "book",
        "type": "update",
        "data": [
            {
                "symbol": "XBT/USD",
                "bids": [{"price": 100000, "qty": 1}],
                "asks": [],
            }
        ],
    }
    events = KrakenAdapter._parse_envelope(msg, ts_recv=42)
    assert len(events) == 1
    delta = events[0]
    assert isinstance(delta, BookDeltaEvent)
    assert delta.ts_event == 42


# ---------------------------------------------------------------------------
# unknown / heartbeat / subscription ack
# ---------------------------------------------------------------------------


def test_parse_envelope_returns_empty_for_subscription_ack() -> None:
    """Kraken subscribe acks have ``method`` instead of ``type`` in (snapshot|update)."""
    ack = {"method": "subscribe", "result": {"channel": "trade"}, "success": True}
    assert KrakenAdapter._parse_envelope(ack, ts_recv=0) == []


def test_parse_envelope_returns_empty_for_heartbeat() -> None:
    hb = {"channel": "heartbeat", "type": "heartbeat"}
    assert KrakenAdapter._parse_envelope(hb, ts_recv=0) == []


def test_parse_envelope_returns_empty_for_unknown_channel() -> None:
    msg = {"channel": "ohlc", "type": "snapshot", "data": []}
    assert KrakenAdapter._parse_envelope(msg, ts_recv=0) == []


# ---------------------------------------------------------------------------
# adapter lifecycle
# ---------------------------------------------------------------------------


def test_adapter_requires_at_least_one_symbol() -> None:
    adapter = KrakenAdapter([])
    with pytest.raises(ValueError, match="at least one symbol"):
        asyncio.run(adapter.connect())


def test_stream_before_connect_raises() -> None:
    adapter = KrakenAdapter(["BTC-USD"])

    async def consume() -> None:
        async for _ in adapter.stream():
            break

    with pytest.raises(RuntimeError, match="connect"):
        asyncio.run(consume())
