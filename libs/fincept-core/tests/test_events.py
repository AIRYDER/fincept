from decimal import Decimal

from fincept_core.events import make_event, parse_event
from fincept_core.schemas import AssetClass, Side, TradeEvent, Venue


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
    assert isinstance(parsed, TradeEvent)
    assert parsed.price == Decimal("123.45")
    assert parsed.side == Side.BUY
