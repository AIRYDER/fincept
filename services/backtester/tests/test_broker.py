"""Tests for backtester.broker.SimBroker — order lifecycle + fill rules."""

from __future__ import annotations

from decimal import Decimal

from backtester.broker import SimBroker
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)


def _intent(
    *,
    order_id: str = "o1",
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
    quantity: str = "1",
    symbol: str = "BTC-USD",
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="d1",
        ts_event=0,
        strategy_id="s",
        symbol=symbol,
        venue=Venue.PAPER,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        limit_price=limit_price,
        time_in_force=TimeInForce.GTC,
    )


def _bar(
    *,
    symbol: str = "BTC-USD",
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    ts_event: int = 1_000,
) -> BarEvent:
    return BarEvent(
        venue=Venue.PAPER,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_event,
        freq="1m",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        trades=1,
    )


# ---------------------------------------------------------------------------
# Submit + cancel
# ---------------------------------------------------------------------------


def test_submit_adds_to_open_orders() -> None:
    broker = SimBroker()
    order = broker.submit(_intent())
    assert order.order_id in broker.open_orders


def test_cancel_removes_from_open_orders() -> None:
    broker = SimBroker()
    broker.submit(_intent(order_id="o1"))
    assert broker.cancel("o1") is True
    assert "o1" not in broker.open_orders


def test_cancel_returns_false_for_unknown_order() -> None:
    broker = SimBroker()
    assert broker.cancel("missing") is False


# ---------------------------------------------------------------------------
# Market orders
# ---------------------------------------------------------------------------


def test_market_buy_fills_at_bar_open_with_costs() -> None:
    """MARKET buys reference bar.open; costs add half-spread + slippage + fee."""
    broker = SimBroker()
    broker.submit(_intent(side=Side.BUY))
    fills = broker.on_bar(_bar(open_="100"))
    assert len(fills) == 1
    fill = fills[0]
    assert fill.symbol == "BTC-USD"
    assert fill.side == Side.BUY
    assert fill.price > Decimal("100")  # spread + slippage push above mid
    assert fill.is_maker is False  # market = taker
    # Order should be removed after full fill.
    assert "o1" not in broker.open_orders


def test_market_sell_fills_below_bar_open() -> None:
    broker = SimBroker()
    broker.submit(_intent(side=Side.SELL))
    [fill] = broker.on_bar(_bar(open_="100"))
    assert fill.price < Decimal("100")


# ---------------------------------------------------------------------------
# Limit orders
# ---------------------------------------------------------------------------


def test_limit_buy_fills_when_bar_low_touches_limit() -> None:
    broker = SimBroker()
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.LIMIT, limit_price=Decimal("99.5")))
    [fill] = broker.on_bar(_bar(low="99", high="101", open_="100.5"))
    assert fill.is_maker is True
    # Reference price = min(limit, open) = 99.5; final price = 99.5 + half-spread.
    assert fill.price > Decimal("99.5")


def test_limit_sell_fills_when_bar_high_touches_limit() -> None:
    broker = SimBroker()
    broker.submit(_intent(side=Side.SELL, order_type=OrderType.LIMIT, limit_price=Decimal("100.5")))
    [fill] = broker.on_bar(_bar(low="99", high="101", open_="100"))
    assert fill.is_maker is True


def test_limit_does_not_fill_when_bar_misses_price() -> None:
    """Limit buy at 95, bar range [99, 101] -> no fill."""
    broker = SimBroker()
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.LIMIT, limit_price=Decimal("95")))
    fills = broker.on_bar(_bar(low="99", high="101"))
    assert fills == []
    assert "o1" in broker.open_orders  # still open


def test_open_orders_for_other_symbols_are_skipped() -> None:
    """Bar for ETH-USD should not fill BTC-USD orders."""
    broker = SimBroker()
    broker.submit(_intent(symbol="BTC-USD"))
    fills = broker.on_bar(_bar(symbol="ETH-USD"))
    assert fills == []
    assert "o1" in broker.open_orders
