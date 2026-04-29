"""Tests for oms.paper.PaperFiller."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fincept_core.schemas import (
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)
from oms.paper import PaperFiller


def _zero_rng(_mu: float, _sigma: float) -> float:
    """Deterministic Gaussian: always returns the mean (mu)."""
    return _mu


def _frozen_clock(ts: int = 1_000_000_000) -> int:
    return ts


def _order(
    *,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
    quantity: str = "1",
) -> Order:
    return Order(
        order_id="o1",
        decision_id="d1",
        ts_event=0,
        strategy_id="s",
        symbol="BTC-USD",
        venue=Venue.PAPER,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        limit_price=limit_price,
        time_in_force=TimeInForce.IOC,
        status=OrderStatus.NEW,
        created_at=0,
        updated_at=0,
    )


def _filler(spread_bps: str = "10") -> PaperFiller:
    return PaperFiller(
        mean_latency_ms=0.0,
        std_latency_ms=0.0,
        spread_bps=Decimal(spread_bps),
        rng=_zero_rng,
        clock=_frozen_clock,
    )


# ---------------------------------------------------------------------------
# Market fills
# ---------------------------------------------------------------------------


def test_market_buy_pays_half_spread_above_mid() -> None:
    """10 bps spread, half = 5 bps -> exec = mid + mid * 5 / 10000."""
    fill = _filler(spread_bps="10").fill(_order(side=Side.BUY), mid=Decimal("100"))
    expected = Decimal("100") + Decimal("100") * Decimal("5") / Decimal(10000)
    assert fill.price == expected
    assert fill.is_maker is False


def test_market_sell_receives_half_spread_below_mid() -> None:
    fill = _filler(spread_bps="10").fill(_order(side=Side.SELL), mid=Decimal("100"))
    expected = Decimal("100") - Decimal("100") * Decimal("5") / Decimal(10000)
    assert fill.price == expected


def test_market_fee_uses_taker_rate() -> None:
    """5 bps default taker fee on the executed notional."""
    f = _filler()
    fill = f.fill(_order(side=Side.BUY), mid=Decimal("100"))
    expected_fee = fill.price * fill.quantity * Decimal("5") / Decimal(10000)
    assert fill.fee == expected_fee


# ---------------------------------------------------------------------------
# Limit fills
# ---------------------------------------------------------------------------


def test_limit_fills_at_limit_price() -> None:
    fill = _filler().fill(
        _order(order_type=OrderType.LIMIT, limit_price=Decimal("99.5"), side=Side.BUY),
        mid=Decimal("100"),
    )
    assert fill.price == Decimal("99.5")
    assert fill.is_maker is True


def test_limit_fee_uses_maker_rate() -> None:
    """Maker rate is 1 bp by default — much smaller than taker 5 bps."""
    fill = _filler().fill(
        _order(order_type=OrderType.LIMIT, limit_price=Decimal("99.5"), side=Side.BUY),
        mid=Decimal("100"),
    )
    expected_fee = Decimal("99.5") * Decimal("1") * Decimal("1") / Decimal(10000)
    assert fill.fee == expected_fee


def test_limit_without_limit_price_raises() -> None:
    """Defensive: a LIMIT order with no limit_price is malformed; raise loudly."""
    with pytest.raises(ValueError, match="limit_price"):
        _filler().fill(_order(order_type=OrderType.LIMIT, limit_price=None), mid=Decimal("100"))


# ---------------------------------------------------------------------------
# Latency injection
# ---------------------------------------------------------------------------


def test_latency_zero_when_rng_returns_zero() -> None:
    f = PaperFiller(mean_latency_ms=0.0, std_latency_ms=0.0, rng=_zero_rng, clock=_frozen_clock)
    fill = f.fill(_order(), mid=Decimal("100"))
    assert fill.ts_event == _frozen_clock()  # no latency added


def test_latency_clamped_to_non_negative() -> None:
    """Negative Gaussian samples (rare but possible) must not produce a
    negative latency — clamp to 0."""
    f = PaperFiller(
        mean_latency_ms=10.0,
        std_latency_ms=5.0,
        rng=lambda _mu, _sigma: -100.0,  # below zero
        clock=_frozen_clock,
    )
    fill = f.fill(_order(), mid=Decimal("100"))
    assert fill.ts_event == _frozen_clock()  # clamped to 0


def test_latency_uses_rng_value_when_positive() -> None:
    """Latency = rng(mu, sigma) ms -> ms * 1e6 ns."""
    f = PaperFiller(
        mean_latency_ms=50.0,
        std_latency_ms=10.0,
        rng=lambda _mu, _sigma: 50.0,  # exactly the mean
        clock=_frozen_clock,
    )
    fill = f.fill(_order(), mid=Decimal("100"))
    expected_ts = _frozen_clock() + 50_000_000  # 50ms = 50_000_000 ns
    assert fill.ts_event == expected_ts
