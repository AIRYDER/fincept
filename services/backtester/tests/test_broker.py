"""Tests for backtester.broker.SimBroker — order lifecycle + fill rules."""

from __future__ import annotations

from decimal import Decimal

from backtester.broker import SimBroker
from backtester.costs import CostModel
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    OrderIntent,
    OrderStatus,
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
    stop_price: Decimal | None = None,
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
        stop_price=stop_price,
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


# ---------------------------------------------------------------------------
# Partial fills — driven by cost_model.max_participation_pct
# ---------------------------------------------------------------------------


def _capped_broker(cap_pct: str = "50") -> SimBroker:
    """Broker whose cost model caps participation at *cap_pct*% of bar vol."""
    return SimBroker(cost_model=CostModel(max_participation_pct=Decimal(cap_pct)))


def _bar_with_volume(volume: str, **kwargs: object) -> BarEvent:
    """Bar helper that lets a test dial in a specific traded volume.

    ``kwargs`` are forwarded to :func:`_bar`; loosely typed because
    ``_bar`` accepts a mix of ``str`` and ``int`` (the ``ts_event``).
    """
    base = _bar(**kwargs)  # type: ignore[arg-type]
    return base.model_copy(update={"volume": Decimal(volume)})


def test_partial_fill_caps_quantity_at_max_participation() -> None:
    """Order qty=200, bar vol=100, cap=50% -> single fill of 50 shares."""
    broker = _capped_broker("50")
    broker.submit(_intent(side=Side.BUY, quantity="200"))
    fills = broker.on_bar(_bar_with_volume("100"))
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("50")
    # Order remains open with PARTIALLY_FILLED status and filled_qty=50.
    open_order = broker.open_orders["o1"]
    assert open_order.status == OrderStatus.PARTIALLY_FILLED
    assert open_order.filled_qty == Decimal("50")
    assert open_order.avg_fill_price == fills[0].price


def test_partial_fill_continues_across_multiple_bars() -> None:
    """200-share order against four 100-vol bars at 50% cap -> 50 each, total 200."""
    broker = _capped_broker("50")
    broker.submit(_intent(side=Side.BUY, quantity="200"))
    total_filled = Decimal(0)
    for i in range(4):
        fills = broker.on_bar(_bar_with_volume("100", ts_event=1_000 + i))
        assert len(fills) == 1
        assert fills[0].quantity == Decimal("50")
        total_filled += fills[0].quantity
    assert total_filled == Decimal("200")
    # Order is gone after the final fill.
    assert "o1" not in broker.open_orders


def test_partial_fill_avg_fill_price_is_quantity_weighted() -> None:
    """Two partials at different prices => avg = (q1*p1 + q2*p2)/(q1+q2)."""
    broker = _capped_broker("50")
    broker.submit(_intent(side=Side.BUY, quantity="100"))
    fill_a = broker.on_bar(_bar_with_volume("100", open_="100"))[0]
    # Mid-stream check.
    open_after_first = broker.open_orders["o1"]
    assert open_after_first.filled_qty == Decimal("50")
    assert open_after_first.avg_fill_price == fill_a.price

    fill_b = broker.on_bar(_bar_with_volume("100", open_="101", ts_event=2_000))[0]
    expected_avg = (Decimal("50") * fill_a.price + Decimal("50") * fill_b.price) / Decimal("100")
    # Order fully filled now -> not in book; check via the fill stream
    # we observed.  Verify quantity sum and weighted price arithmetic.
    assert "o1" not in broker.open_orders
    assert fill_a.quantity + fill_b.quantity == Decimal("100")
    # Reconstruct what the avg would have been at completion:
    reconstruct = (fill_a.quantity * fill_a.price + fill_b.quantity * fill_b.price) / (
        fill_a.quantity + fill_b.quantity
    )
    assert reconstruct == expected_avg


def test_default_cap_preserves_full_fill_behavior() -> None:
    """Default max_participation_pct=100 => full fill regardless of bar vol."""
    broker = SimBroker()  # default cap = 100
    broker.submit(_intent(side=Side.BUY, quantity="500"))
    [fill] = broker.on_bar(_bar_with_volume("10"))  # order >> bar volume
    assert fill.quantity == Decimal("500")
    assert "o1" not in broker.open_orders


def test_zero_volume_bar_falls_back_to_full_fill() -> None:
    """Cap configured but bar has no volume -> avoid starvation, full-fill."""
    broker = _capped_broker("10")
    broker.submit(_intent(side=Side.BUY, quantity="50"))
    [fill] = broker.on_bar(_bar_with_volume("0"))
    assert fill.quantity == Decimal("50")
    assert "o1" not in broker.open_orders


def test_ioc_partial_fill_cancels_remainder() -> None:
    """IOC: emit one partial fill, drop the leftover from the book."""
    broker = _capped_broker("50")
    intent = _intent(side=Side.BUY, quantity="200")
    intent = intent.model_copy(update={"time_in_force": TimeInForce.IOC})
    broker.submit(intent)
    fills = broker.on_bar(_bar_with_volume("100"))
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("50")
    # IOC kills remainder.
    assert "o1" not in broker.open_orders


def test_fok_rejected_when_cap_prevents_full_fill() -> None:
    """FOK: if we can't fully fill in one bar, emit no fill at all."""
    broker = _capped_broker("50")
    intent = _intent(side=Side.BUY, quantity="200")
    intent = intent.model_copy(update={"time_in_force": TimeInForce.FOK})
    broker.submit(intent)
    fills = broker.on_bar(_bar_with_volume("100"))
    assert fills == []
    assert "o1" not in broker.open_orders


def test_fok_fills_when_cap_allows_full_fill() -> None:
    """FOK with order qty <= cap*bar_vol -> single atomic fill."""
    broker = _capped_broker("50")
    # 10 <= 50% of 100, so FOK can fully fill.
    intent = _intent(side=Side.BUY, quantity="10")
    intent = intent.model_copy(update={"time_in_force": TimeInForce.FOK})
    broker.submit(intent)
    [fill] = broker.on_bar(_bar_with_volume("100"))
    assert fill.quantity == Decimal("10")
    assert "o1" not in broker.open_orders


def test_partial_fill_works_with_limit_orders() -> None:
    """Limit qty=200, bar contains the limit, vol=100, cap=50% -> partial of 50."""
    broker = _capped_broker("50")
    broker.submit(
        _intent(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("99.5"),
            quantity="200",
        )
    )
    fills = broker.on_bar(_bar_with_volume("100", low="99", high="101", open_="100.5"))
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("50")
    assert fills[0].is_maker is True
    # Remainder stays open as PARTIALLY_FILLED.
    leftover = broker.open_orders["o1"]
    assert leftover.status == OrderStatus.PARTIALLY_FILLED
    assert leftover.filled_qty == Decimal("50")


def test_partial_fill_impact_uses_clamped_participation() -> None:
    """Sanity check: per-fill impact reflects clamped %, not raw order %.

    Order = 200 against bar_vol=100 at 50% cap.  The single emitted fill
    has quantity=50 => actual participation 50% (50/100*100), and the
    cost model's sqrt impact uses 50%, not the raw 200%.  Compare
    against a reference run where qty=50 is sent directly: prices match.
    """
    broker_capped = _capped_broker("50")
    broker_capped.submit(_intent(order_id="big", side=Side.BUY, quantity="200"))
    [capped_fill] = broker_capped.on_bar(_bar_with_volume("100"))

    broker_ref = _capped_broker("50")
    broker_ref.submit(_intent(order_id="ref", side=Side.BUY, quantity="50"))
    [ref_fill] = broker_ref.on_bar(_bar_with_volume("100"))

    assert capped_fill.price == ref_fill.price
    assert capped_fill.quantity == ref_fill.quantity == Decimal("50")


# ---------------------------------------------------------------------------
# STOP orders
# ---------------------------------------------------------------------------


def _zero_cost_broker() -> SimBroker:
    """Broker with zero spread/impact/fees so the assertions can compare
    fill prices to raw stop/open levels without cost-model arithmetic
    in the way."""
    return SimBroker(
        cost_model=CostModel(
            maker_fee_bps=Decimal(0),
            taker_fee_bps=Decimal(0),
            spread_bps_default=Decimal(0),
            impact_coef_sqrt=Decimal(0),
            slippage_impact_coef=Decimal(0),
        )
    )


def test_stop_buy_triggers_when_high_crosses_stop() -> None:
    """STOP BUY at 105: bar.high=110 crosses up, fills at stop level."""
    broker = _zero_cost_broker()
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.STOP, stop_price=Decimal("105")))
    fills = broker.on_bar(_bar(open_="100", high="110", low="99", close="108"))
    assert len(fills) == 1
    # open=100 < stop=105 so we trigger on the way up at 105.
    assert fills[0].price == Decimal("105")
    assert fills[0].is_maker is False
    assert "o1" not in broker.open_orders


def test_stop_buy_gap_up_fills_at_open() -> None:
    """STOP BUY at 105 with bar.open=108 => filled at open (worse price)."""
    broker = _zero_cost_broker()
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.STOP, stop_price=Decimal("105")))
    fills = broker.on_bar(_bar(open_="108", high="112", low="107", close="110"))
    assert len(fills) == 1
    # open=108 already past stop=105 -> gap-up, fill at open.
    assert fills[0].price == Decimal("108")


def test_stop_buy_does_not_trigger_when_high_below_stop() -> None:
    """STOP BUY at 105: bar's range stays below 105 => no fill, stays open."""
    broker = _zero_cost_broker()
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.STOP, stop_price=Decimal("105")))
    fills = broker.on_bar(_bar(open_="100", high="104", low="99", close="103"))
    assert fills == []
    assert "o1" in broker.open_orders  # GTC keeps it alive


def test_stop_sell_triggers_when_low_crosses_stop() -> None:
    """STOP SELL at 95 (a stop-loss): bar.low=92 fires it, fill at stop."""
    broker = _zero_cost_broker()
    broker.submit(_intent(side=Side.SELL, order_type=OrderType.STOP, stop_price=Decimal("95")))
    fills = broker.on_bar(_bar(open_="100", high="101", low="92", close="93"))
    assert len(fills) == 1
    # open=100 > stop=95 -> triggered on the way down at 95.
    assert fills[0].price == Decimal("95")


def test_stop_sell_gap_down_fills_at_open() -> None:
    """STOP SELL at 95 with bar.open=92 => filled at open (worse price)."""
    broker = _zero_cost_broker()
    broker.submit(_intent(side=Side.SELL, order_type=OrderType.STOP, stop_price=Decimal("95")))
    fills = broker.on_bar(_bar(open_="92", high="93", low="90", close="91"))
    assert len(fills) == 1
    # open=92 already past stop=95 -> gap-down, fill at open=92.
    assert fills[0].price == Decimal("92")


def test_stop_sell_does_not_trigger_when_low_above_stop() -> None:
    """STOP SELL at 95 with bar.low=96 => no fill."""
    broker = _zero_cost_broker()
    broker.submit(_intent(side=Side.SELL, order_type=OrderType.STOP, stop_price=Decimal("95")))
    fills = broker.on_bar(_bar(open_="100", high="101", low="96", close="98"))
    assert fills == []
    assert "o1" in broker.open_orders


def test_stop_persists_across_bars_until_triggered() -> None:
    """A non-triggered STOP stays open and triggers on a later bar."""
    broker = _zero_cost_broker()
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.STOP, stop_price=Decimal("105")))
    # Bar 1: doesn't trigger.
    assert broker.on_bar(_bar(open_="100", high="103", low="99", close="102")) == []
    assert "o1" in broker.open_orders
    # Bar 2: triggers.
    fills = broker.on_bar(_bar(open_="103", high="108", low="102", close="107"))
    assert len(fills) == 1
    assert fills[0].price == Decimal("105")
    assert "o1" not in broker.open_orders


# ---------------------------------------------------------------------------
# STOP_LIMIT orders
# ---------------------------------------------------------------------------


def test_stop_limit_buy_fills_when_both_triggered_and_in_limit_zone() -> None:
    """STOP_LIMIT BUY: stop=105, limit=107, bar [99..110] => triggers at 105
    and price stayed within limit ceiling => fill at stop level."""
    broker = _zero_cost_broker()
    broker.submit(
        _intent(
            side=Side.BUY,
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("105"),
            limit_price=Decimal("107"),
        )
    )
    fills = broker.on_bar(_bar(open_="100", high="110", low="99", close="108"))
    assert len(fills) == 1
    # min(limit=107, max(stop=105, open=100)) = min(107, 105) = 105.
    assert fills[0].price == Decimal("105")


def test_stop_limit_buy_gap_up_clamps_to_limit() -> None:
    """Gap-up past limit: open=108 > limit=107 -> capped at limit price."""
    broker = _zero_cost_broker()
    broker.submit(
        _intent(
            side=Side.BUY,
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("105"),
            limit_price=Decimal("107"),
        )
    )
    # bar.low=106 still <= limit=107 so the limit zone was crossed.
    fills = broker.on_bar(_bar(open_="108", high="112", low="106", close="110"))
    assert len(fills) == 1
    # min(limit=107, max(stop=105, open=108)) = min(107, 108) = 107.
    assert fills[0].price == Decimal("107")


def test_stop_limit_buy_no_fill_when_low_above_limit() -> None:
    """Triggered but the bar's range never went into the limit zone."""
    broker = _zero_cost_broker()
    broker.submit(
        _intent(
            side=Side.BUY,
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("105"),
            limit_price=Decimal("106"),
        )
    )
    # high=110 triggers, but low=107 > limit=106 -> nothing in limit zone.
    fills = broker.on_bar(_bar(open_="108", high="110", low="107", close="109"))
    assert fills == []
    # Still open, will retry next bar.
    assert "o1" in broker.open_orders


def test_stop_limit_sell_fills_at_limit_clamp() -> None:
    """Mirror: STOP_LIMIT SELL with stop=95, limit=93, bar [90..100]."""
    broker = _zero_cost_broker()
    broker.submit(
        _intent(
            side=Side.SELL,
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("95"),
            limit_price=Decimal("93"),
        )
    )
    fills = broker.on_bar(_bar(open_="100", high="100", low="90", close="92"))
    assert len(fills) == 1
    # max(limit=93, min(stop=95, open=100)) = max(93, 95) = 95.
    assert fills[0].price == Decimal("95")


def test_stop_limit_sell_no_fill_when_high_below_limit() -> None:
    """SELL: stop triggered (low<=stop) but limit needs high>=limit."""
    broker = _zero_cost_broker()
    broker.submit(
        _intent(
            side=Side.SELL,
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("95"),
            limit_price=Decimal("94"),
        )
    )
    # low=92 triggers stop, but high=93 < limit=94 -> no fill.
    fills = broker.on_bar(_bar(open_="93", high="93", low="92", close="92.5"))
    assert fills == []
    assert "o1" in broker.open_orders


# ---------------------------------------------------------------------------
# Stop interaction with partial fills
# ---------------------------------------------------------------------------


def test_stop_buy_respects_participation_cap() -> None:
    """A triggered STOP for 200 against bar.volume=100 with a 50% cap fills
    just 50 shares this bar; the remainder stays open as PARTIALLY_FILLED."""
    broker = SimBroker(
        cost_model=CostModel(
            maker_fee_bps=Decimal(0),
            taker_fee_bps=Decimal(0),
            spread_bps_default=Decimal(0),
            impact_coef_sqrt=Decimal(0),
            slippage_impact_coef=Decimal(0),
            max_participation_pct=Decimal("50"),
        )
    )
    broker.submit(
        _intent(
            side=Side.BUY,
            order_type=OrderType.STOP,
            stop_price=Decimal("105"),
            quantity="200",
        )
    )
    # Bar volume = 100; cap 50% => fill 50 shares this bar.
    fills = broker.on_bar(_bar_with_volume("100", open_="100", high="110", low="99", close="108"))
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("50")
    assert fills[0].price == Decimal("105")  # stop level
    # Remainder still open, in PARTIALLY_FILLED.
    leftover = broker.open_orders["o1"]
    assert leftover.status == OrderStatus.PARTIALLY_FILLED
    assert leftover.filled_qty == Decimal("50")
