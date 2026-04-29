"""
Tests for oms.processor.process_intent — the IntentResult pipeline.

Pure-function tests: no Redis, no DB.  Inject a deterministic PaperFiller
and a pre-loaded LivePrices.
"""

from __future__ import annotations

from decimal import Decimal

from fincept_core.schemas import (
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)
from oms.paper import PaperFiller
from oms.prices import LivePrices
from oms.processor import process_intent


def _intent(
    *,
    order_id: str = "o1",
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
    symbol: str = "BTC-USD",
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="d1",
        ts_event=1_000,
        strategy_id="s",
        symbol=symbol,
        venue=Venue.PAPER,
        side=side,
        order_type=order_type,
        quantity=Decimal("1"),
        limit_price=limit_price,
        time_in_force=TimeInForce.IOC,
    )


def _filler() -> PaperFiller:
    return PaperFiller(
        mean_latency_ms=0.0,
        std_latency_ms=0.0,
        spread_bps=Decimal("10"),
        rng=lambda mu, _sigma: mu,
        clock=lambda: 2_000,
    )


# ---------------------------------------------------------------------------
# Happy path: intent -> PENDING_NEW -> NEW -> FILLED
# ---------------------------------------------------------------------------


def test_intent_with_known_price_emits_three_states_and_fill() -> None:
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("100"))
    result = process_intent(_intent(), prices=prices, filler=_filler())

    assert [o.status for o in result.order_states] == [
        OrderStatus.PENDING_NEW,
        OrderStatus.NEW,
        OrderStatus.FILLED,
    ]
    assert result.fill is not None
    assert result.fill.symbol == "BTC-USD"
    assert result.fill.side == Side.BUY
    assert result.final_status == OrderStatus.FILLED


def test_filled_order_carries_avg_fill_price_and_filled_qty() -> None:
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("100"))
    result = process_intent(_intent(), prices=prices, filler=_filler())

    final = result.order_states[-1]
    assert final.filled_qty == Decimal("1")
    assert final.avg_fill_price == result.fill.price  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Reject: no live price for symbol
# ---------------------------------------------------------------------------


def test_intent_rejected_when_no_price_available() -> None:
    """Without a mid price the OMS would have to invent one - fail loudly instead."""
    prices = LivePrices()  # empty
    result = process_intent(_intent(), prices=prices, filler=_filler())

    assert result.fill is None
    assert result.final_status == OrderStatus.REJECTED
    statuses = [o.status for o in result.order_states]
    assert statuses == [OrderStatus.PENDING_NEW, OrderStatus.NEW, OrderStatus.REJECTED]


# ---------------------------------------------------------------------------
# Limit orders fill at the limit price (delegated to PaperFiller)
# ---------------------------------------------------------------------------


def test_limit_order_fills_at_limit_price() -> None:
    prices = LivePrices()
    prices.update("BTC-USD", Decimal("100"))
    result = process_intent(
        _intent(order_type=OrderType.LIMIT, limit_price=Decimal("99.5")),
        prices=prices,
        filler=_filler(),
    )
    assert result.fill is not None
    assert result.fill.price == Decimal("99.5")
    assert result.fill.is_maker is True


# ---------------------------------------------------------------------------
# Different symbols are independent
# ---------------------------------------------------------------------------


def test_intent_for_unknown_symbol_rejects_even_when_other_symbols_have_prices() -> None:
    """ETH-USD price doesn't help an ADA-USD intent."""
    prices = LivePrices()
    prices.update("ETH-USD", Decimal("3000"))
    result = process_intent(
        _intent(order_id="o2", symbol="ADA-USD"), prices=prices, filler=_filler()
    )
    assert result.final_status == OrderStatus.REJECTED
    assert result.fill is None
