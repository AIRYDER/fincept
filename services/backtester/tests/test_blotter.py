"""Tests for backtester.blotter.Blotter — append-only fills + equity log."""

from __future__ import annotations

from decimal import Decimal

from backtester.blotter import Blotter
from fincept_core.schemas import Fill, Side


def _fill(ts: int = 1_000, price: str = "100") -> Fill:
    return Fill(
        fill_id=f"f-{ts}",
        order_id="o1",
        ts_event=ts,
        symbol="BTC-USD",
        side=Side.BUY,
        price=Decimal(price),
        quantity=Decimal("1"),
        fee=Decimal("0.05"),
        is_maker=False,
    )


def test_blotter_starts_empty() -> None:
    b = Blotter()
    assert b.fills == []
    assert b.equity_curve == []
    assert b.starting_cash == Decimal("100000")


def test_add_fill_appends_in_order() -> None:
    b = Blotter()
    b.add_fill(_fill(ts=1_000))
    b.add_fill(_fill(ts=2_000))
    assert [f.ts_event for f in b.fills] == [1_000, 2_000]


def test_mark_equity_appends_in_order() -> None:
    b = Blotter()
    b.mark_equity(1_000, Decimal("100100"))
    b.mark_equity(2_000, Decimal("100200"))
    assert b.equity_curve == [(1_000, Decimal("100100")), (2_000, Decimal("100200"))]


def test_final_equity_returns_starting_cash_when_empty() -> None:
    b = Blotter(starting_cash=Decimal("50000"))
    assert b.final_equity == Decimal("50000")


def test_final_equity_returns_last_curve_value() -> None:
    b = Blotter()
    b.mark_equity(1_000, Decimal("100100"))
    b.mark_equity(2_000, Decimal("99950"))
    assert b.final_equity == Decimal("99950")
