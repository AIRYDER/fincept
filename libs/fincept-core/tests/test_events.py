from decimal import Decimal

import pytest

from fincept_core.errors import ContractError
from fincept_core.events import Event, make_event, parse_event
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    BookDeltaEvent,
    BookSnapshotEvent,
    Side,
    TradeEvent,
    Venue,
)


def test_make_and_parse_trade_event_round_trip():
    event = make_event(
        "trade",
        {
            "venue": Venue.BINANCE,
            "symbol": "BTC-USD",
            "asset_class": AssetClass.CRYPTO_SPOT,
            "ts_event": 1,
            "ts_recv": 2,
            "seq": 3,
            "price": Decimal("123.45"),
            "size": Decimal("0.5"),
            "side": Side.BUY,
        },
    )

    parsed = parse_event(event.model_dump())
    assert isinstance(parsed, Event)
    assert isinstance(parsed.payload, TradeEvent)
    assert parsed.payload.price == Decimal("123.45")
    assert parsed.payload.side == Side.BUY


@pytest.mark.parametrize(
    ("event_type", "payload", "payload_cls"),
    [
        (
            "trade",
            {
                "venue": Venue.BINANCE,
                "symbol": "BTC-USD",
                "asset_class": AssetClass.CRYPTO_SPOT,
                "ts_event": 1,
                "ts_recv": 2,
                "price": Decimal("100.01"),
                "size": Decimal("0.25"),
            },
            TradeEvent,
        ),
        (
            "book_delta",
            {
                "venue": Venue.COINBASE,
                "symbol": "ETH-USD",
                "asset_class": AssetClass.CRYPTO_SPOT,
                "ts_event": 3,
                "ts_recv": 4,
                "bids_add": [{"price": Decimal("2000.01"), "size": Decimal("1.5")}],
                "bids_remove": [Decimal("1999.99")],
                "asks_add": [{"price": Decimal("2000.02"), "size": Decimal("2.5")}],
                "asks_remove": [Decimal("2001.00")],
            },
            BookDeltaEvent,
        ),
        (
            "book_snapshot",
            {
                "venue": Venue.KRAKEN,
                "symbol": "SOL-USD",
                "asset_class": AssetClass.CRYPTO_SPOT,
                "ts_event": 5,
                "ts_recv": 6,
                "bids": [{"price": Decimal("150.00"), "size": Decimal("3")}],
                "asks": [{"price": Decimal("150.01"), "size": Decimal("4")}],
            },
            BookSnapshotEvent,
        ),
        (
            "bar",
            {
                "venue": Venue.NASDAQ,
                "symbol": "AAPL",
                "asset_class": AssetClass.EQUITY,
                "ts_event": 7,
                "ts_recv": 8,
                "freq": "1m",
                "open": Decimal("190.00"),
                "high": Decimal("191.00"),
                "low": Decimal("189.50"),
                "close": Decimal("190.50"),
                "volume": Decimal("1000"),
                "trades": 10,
                "vwap": Decimal("190.25"),
            },
            BarEvent,
        ),
    ],
)
def test_every_market_event_round_trips(event_type, payload, payload_cls):
    event = make_event(event_type, payload)
    parsed = parse_event(event.model_dump())
    assert parsed == event
    assert parsed.type == event_type
    assert isinstance(parsed.payload, payload_cls)


def test_parse_event_rejects_unknown_type():
    with pytest.raises(ContractError):
        parse_event({"type": "unknown", "payload": {}})
