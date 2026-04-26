from decimal import Decimal

from fincept_core import schemas


def test_trade_event_round_trip():
    event = schemas.TradeEvent(
        venue=schemas.Venue.BINANCE,
        symbol="BTC-USD",
        asset_class=schemas.AssetClass.CRYPTO_SPOT,
        ts_event=1,
        ts_recv=2,
        seq=3,
        price=Decimal("123.45"),
        size=Decimal("0.5"),
        side=schemas.Side.BUY,
    )

    assert event.model_dump()["price"] == Decimal("123.45")
    assert event.model_dump()["ts_event"] == 1


def test_order_defaults_and_decimals():
    order = schemas.Order(
        order_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        decision_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        ts_event=10,
        strategy_id="strat",
        symbol="AAPL",
        venue=schemas.Venue.NASDAQ,
        side=schemas.Side.SELL,
        order_type=schemas.OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("100.01"),
        created_at=11,
        updated_at=12,
    )

    assert order.status == schemas.OrderStatus.PENDING_NEW
    assert order.filled_qty == Decimal(0)
    assert order.time_in_force == schemas.TimeInForce.GTC


def test_position_allows_signed_decimal_quantity():
    position = schemas.Position(
        strategy_id="s",
        symbol="AAPL",
        quantity=Decimal("-2.5"),
        avg_cost=Decimal("123.00"),
    )

    assert position.quantity == Decimal("-2.5")
