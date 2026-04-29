"""
Tests for fincept_core.portfolio.apply_fill_to_position.

Pins the four cases (open-fresh / open-more / exact-close / cross-flip)
that the backtester engine and the live portfolio service both depend
on.  If this drifts, every PnL number in the system is suspect.
"""

from __future__ import annotations

from decimal import Decimal

from fincept_core.portfolio import apply_fill_to_position, empty_position
from fincept_core.schemas import Fill, Side


def _fill(*, side: Side = Side.BUY, qty: str = "1", price: str = "100", ts: int = 1_000) -> Fill:
    return Fill(
        fill_id=f"f-{ts}",
        order_id="o-1",
        ts_event=ts,
        symbol="BTC-USD",
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        fee=Decimal("0.05"),
    )


# ---------------------------------------------------------------------------
# Case 1: open fresh
# ---------------------------------------------------------------------------


def test_first_fill_opens_position_with_signed_quantity() -> None:
    pos = apply_fill_to_position(None, _fill(side=Side.BUY), strategy_id="s1")
    assert pos.quantity == Decimal("1")
    assert pos.avg_cost == Decimal("100")
    assert pos.realized_pnl == Decimal(0)


def test_first_sell_opens_short_position() -> None:
    pos = apply_fill_to_position(None, _fill(side=Side.SELL), strategy_id="s1")
    assert pos.quantity == Decimal("-1")
    assert pos.avg_cost == Decimal("100")


def test_fresh_open_after_flat_preserves_realized_pnl() -> None:
    """Re-opening from a zero-quantity Position keeps the realized-PnL accumulator."""
    flat = empty_position(strategy_id="s1", symbol="BTC-USD").model_copy(
        update={"realized_pnl": Decimal("50")}
    )
    pos = apply_fill_to_position(flat, _fill(), strategy_id="s1")
    assert pos.quantity == Decimal("1")
    assert pos.realized_pnl == Decimal("50")  # carried forward


# ---------------------------------------------------------------------------
# Case 2: open more in same direction
# ---------------------------------------------------------------------------


def test_buy_then_buy_blends_avg_cost() -> None:
    pos = apply_fill_to_position(None, _fill(price="100"), strategy_id="s1")
    pos = apply_fill_to_position(pos, _fill(price="200"), strategy_id="s1")
    assert pos.quantity == Decimal("2")
    assert pos.avg_cost == Decimal("150")  # weighted: (100 + 200) / 2


def test_buy_three_times_with_different_quantities() -> None:
    pos = apply_fill_to_position(None, _fill(qty="1", price="100"), strategy_id="s1")
    pos = apply_fill_to_position(pos, _fill(qty="2", price="110"), strategy_id="s1")
    pos = apply_fill_to_position(pos, _fill(qty="1", price="120"), strategy_id="s1")
    assert pos.quantity == Decimal("4")
    expected = (Decimal("100") + Decimal("110") * 2 + Decimal("120")) / Decimal("4")
    assert pos.avg_cost == expected


# ---------------------------------------------------------------------------
# Case 3: exact close (back to flat)
# ---------------------------------------------------------------------------


def test_buy_then_equal_sell_closes_to_zero_with_realized_pnl() -> None:
    pos = apply_fill_to_position(None, _fill(side=Side.BUY, price="100"), strategy_id="s1")
    pos = apply_fill_to_position(pos, _fill(side=Side.SELL, price="120"), strategy_id="s1")
    assert pos.quantity == Decimal(0)
    assert pos.realized_pnl == Decimal("20")  # (120 - 100) * 1


def test_short_then_equal_buy_closes_with_realized_pnl() -> None:
    pos = apply_fill_to_position(None, _fill(side=Side.SELL, price="120"), strategy_id="s1")
    pos = apply_fill_to_position(pos, _fill(side=Side.BUY, price="100"), strategy_id="s1")
    assert pos.quantity == Decimal(0)
    assert pos.realized_pnl == Decimal("20")  # short profited as price fell


# ---------------------------------------------------------------------------
# Case 4: cross-flip (over-close, opening opposite)
# ---------------------------------------------------------------------------


def test_buy_one_sell_two_flips_to_short_one() -> None:
    pos = apply_fill_to_position(None, _fill(side=Side.BUY, price="100"), strategy_id="s1")
    pos = apply_fill_to_position(pos, _fill(side=Side.SELL, qty="2", price="120"), strategy_id="s1")
    # Closed 1 long at 120 (realized = +20), opened 1 short at 120.
    assert pos.quantity == Decimal("-1")
    assert pos.avg_cost == Decimal("120")
    assert pos.realized_pnl == Decimal("20")


def test_short_one_buy_two_flips_to_long_one() -> None:
    pos = apply_fill_to_position(None, _fill(side=Side.SELL, price="120"), strategy_id="s1")
    pos = apply_fill_to_position(pos, _fill(side=Side.BUY, qty="2", price="100"), strategy_id="s1")
    # Closed 1 short at 100 (realized = +20), opened 1 long at 100.
    assert pos.quantity == Decimal("1")
    assert pos.avg_cost == Decimal("100")
    assert pos.realized_pnl == Decimal("20")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_empty_position_is_zero_quantity_and_cost() -> None:
    pos = empty_position(strategy_id="s1", symbol="BTC-USD")
    assert pos.quantity == Decimal(0)
    assert pos.avg_cost == Decimal(0)
    assert pos.realized_pnl == Decimal(0)
    assert pos.symbol == "BTC-USD"
    assert pos.strategy_id == "s1"


def test_strategy_id_propagates_into_new_position() -> None:
    pos = apply_fill_to_position(None, _fill(), strategy_id="my_strategy")
    assert pos.strategy_id == "my_strategy"
