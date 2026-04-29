"""Tests for backtester.costs.CostModel — spread, slippage, fees."""

from __future__ import annotations

from decimal import Decimal

from backtester.costs import CostModel
from fincept_core.schemas import Side


def test_buy_fill_pays_half_spread_above_mid() -> None:
    """A taker buy at mid 100 with 3 bps spread pays mid + half-spread = 100.015."""
    model = CostModel()
    exec_price, _fee = model.apply(
        side=Side.BUY,
        price=Decimal("100"),
        quantity=Decimal("1"),
        is_maker=False,
        adv_pct=0.0,  # no slippage so half-spread is the only adjustment
    )
    assert exec_price == Decimal("100.015")  # 100 + (3 / 10000 * 100) / 2


def test_sell_fill_receives_half_spread_below_mid() -> None:
    model = CostModel()
    exec_price, _fee = model.apply(
        side=Side.SELL,
        price=Decimal("100"),
        quantity=Decimal("1"),
        is_maker=False,
        adv_pct=0.0,
    )
    assert exec_price == Decimal("99.985")


def test_taker_pays_higher_fee_than_maker() -> None:
    """Taker fee (5 bps) > maker fee (1 bp) by default."""
    model = CostModel()
    _, taker_fee = model.apply(
        side=Side.BUY,
        price=Decimal("100"),
        quantity=Decimal("1"),
        is_maker=False,
        adv_pct=0.0,
    )
    _, maker_fee = model.apply(
        side=Side.BUY,
        price=Decimal("100"),
        quantity=Decimal("1"),
        is_maker=True,
        adv_pct=0.0,
    )
    assert taker_fee > maker_fee
    # Taker: 100.015 * (5 / 10000) = 0.0500075
    # Maker: 100.015 * (1 / 10000) = 0.01000150
    assert taker_fee == Decimal("100.015") * Decimal("5") / Decimal(10000)
    assert maker_fee == Decimal("100.015") * Decimal("1") / Decimal(10000)


def test_slippage_scales_with_adv_pct() -> None:
    """Larger order -> more slippage drift against the trader."""
    model = CostModel()
    small_price, _ = model.apply(
        side=Side.BUY,
        price=Decimal("100"),
        quantity=Decimal("1"),
        is_maker=False,
        adv_pct=0.001,
    )
    large_price, _ = model.apply(
        side=Side.BUY,
        price=Decimal("100"),
        quantity=Decimal("1"),
        is_maker=False,
        adv_pct=0.05,
    )
    assert large_price > small_price


def test_fee_equals_bps_times_notional() -> None:
    """Fee = exec_price * quantity * fee_bps / 10000."""
    model = CostModel()
    exec_price, fee = model.apply(
        side=Side.BUY,
        price=Decimal("200"),
        quantity=Decimal("3"),
        is_maker=False,
        adv_pct=0.0,
    )
    expected_fee = exec_price * Decimal("3") * model.taker_fee_bps / Decimal(10000)
    assert fee == expected_fee
