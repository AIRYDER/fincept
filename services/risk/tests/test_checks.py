"""Tests for risk.checks.check_intent."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fincept_core.config import Settings
from fincept_core.schemas import (
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)
from risk.checks import RiskContext, check_intent


def _intent(
    *,
    order_id: str = "o1",
    symbol: str = "BTC-USD",
    quantity: str = "1",
    limit_price: str | None = None,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="d1",
        ts_event=1_000,
        strategy_id="s",
        symbol=symbol,
        venue=Venue.BINANCE,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit_price) if limit_price is not None else None,
        time_in_force=TimeInForce.GTC,
    )


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    """Clear the Settings singleton so each test gets a fresh instance
    seeded from its own kwargs (the singleton ignores constructor args
    after first instantiation)."""
    Settings.clear_cache()


@pytest.fixture
def settings() -> Settings:
    # Tight caps to make breach tests easy to express.
    Settings.clear_cache()
    return Settings(
        MAX_NOTIONAL_USD_PER_SYMBOL=10_000,
        MAX_GROSS_NOTIONAL_USD=20_000,
        MAX_DAILY_LOSS_USD=2_000,
    )


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_engaged_rejects_any_intent(settings: Settings) -> None:
    ctx = RiskContext(kill_switch_engaged=True)
    result = check_intent(
        _intent(limit_price="100"), ctx=ctx, settings=settings, last_price=None
    )
    assert result.approved is False
    assert result.reasons == ["kill_switch_engaged"]


def test_kill_switch_short_circuits_other_checks(settings: Settings) -> None:
    """When kill switch is on, we don't bother computing notional - the
    only reason returned is kill_switch_engaged."""
    ctx = RiskContext(
        notional_by_symbol={"BTC-USD": Decimal("999_999_999")},
        gross_notional=Decimal("999_999_999"),
        kill_switch_engaged=True,
    )
    # Even with absurd existing notional, only one reason should appear.
    result = check_intent(
        _intent(limit_price="100"), ctx=ctx, settings=settings, last_price=None
    )
    assert result.approved is False
    assert result.reasons == ["kill_switch_engaged"]


# ---------------------------------------------------------------------------
# Reference price
# ---------------------------------------------------------------------------


def test_market_intent_with_no_last_price_is_rejected(settings: Settings) -> None:
    """Without a price reference we can't compute notional - reject."""
    ctx = RiskContext()
    result = check_intent(
        _intent(quantity="1", limit_price=None),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    assert result.approved is False
    assert any("no_reference_price" in r for r in result.reasons)


def test_market_intent_uses_last_price_for_notional(settings: Settings) -> None:
    ctx = RiskContext()
    result = check_intent(
        _intent(quantity="1", limit_price=None),
        ctx=ctx,
        settings=settings,
        last_price=Decimal("5_000"),  # 5k notional, well within 10k cap
    )
    assert result.approved is True
    assert result.reasons == []


def test_limit_price_takes_precedence_over_last_price(settings: Settings) -> None:
    """If both are present, use limit_price (it's what we'd execute at)."""
    ctx = RiskContext()
    # limit_price=12_000 * qty=1 = 12_000 > 10_000 cap.  last_price below cap.
    result = check_intent(
        _intent(quantity="1", limit_price="12_000"),
        ctx=ctx,
        settings=settings,
        last_price=Decimal("5_000"),
    )
    assert result.approved is False
    assert any("per_symbol_notional_breach" in r for r in result.reasons)


def test_zero_or_negative_price_rejected(settings: Settings) -> None:
    ctx = RiskContext()
    result = check_intent(
        _intent(quantity="1", limit_price=None),
        ctx=ctx,
        settings=settings,
        last_price=Decimal("0"),
    )
    assert result.approved is False
    assert any("non_positive_reference_price" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Per-symbol notional cap
# ---------------------------------------------------------------------------


def test_within_per_symbol_cap_approved(settings: Settings) -> None:
    """Existing 5k + new 4k = 9k <= 10k."""
    ctx = RiskContext(
        notional_by_symbol={"BTC-USD": Decimal("5000")},
        gross_notional=Decimal("5000"),
    )
    result = check_intent(
        _intent(quantity="1", limit_price="4_000"),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    assert result.approved is True


def test_per_symbol_breach_rejected(settings: Settings) -> None:
    """Existing 8k + new 4k = 12k > 10k cap."""
    ctx = RiskContext(
        notional_by_symbol={"BTC-USD": Decimal("8000")},
        gross_notional=Decimal("8000"),
    )
    result = check_intent(
        _intent(quantity="1", limit_price="4_000"),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    assert result.approved is False
    assert any("per_symbol_notional_breach:BTC-USD" in r for r in result.reasons)


def test_other_symbols_existing_notional_does_not_count_per_symbol(
    settings: Settings,
) -> None:
    """ETH-USD existing notional is unrelated to a BTC-USD intent."""
    ctx = RiskContext(
        notional_by_symbol={"ETH-USD": Decimal("8000")},
        gross_notional=Decimal("8000"),
    )
    result = check_intent(
        _intent(symbol="BTC-USD", quantity="1", limit_price="5_000"),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    # 5k <= 10k per-symbol; 8k+5k=13k <= 20k gross.
    assert result.approved is True


# ---------------------------------------------------------------------------
# Gross notional cap
# ---------------------------------------------------------------------------


def test_gross_breach_rejected_even_when_per_symbol_ok(settings: Settings) -> None:
    """3 symbols at 8k each + new 4k = 28k > 20k gross cap."""
    ctx = RiskContext(
        notional_by_symbol={
            "BTC-USD": Decimal("8000"),
            "ETH-USD": Decimal("8000"),
            "SOL-USD": Decimal("8000"),
        },
        gross_notional=Decimal("24_000"),  # already over without us
    )
    result = check_intent(
        _intent(symbol="LINK-USD", quantity="1", limit_price="4_000"),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    assert result.approved is False
    assert any("gross_notional_breach" in r for r in result.reasons)


def test_per_symbol_and_gross_both_breached_returns_both_reasons(
    settings: Settings,
) -> None:
    ctx = RiskContext(
        notional_by_symbol={"BTC-USD": Decimal("9000")},
        gross_notional=Decimal("19_000"),
    )
    result = check_intent(
        _intent(quantity="1", limit_price="5_000"),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    assert result.approved is False
    assert any("per_symbol_notional_breach" in r for r in result.reasons)
    assert any("gross_notional_breach" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Side / sign handling
# ---------------------------------------------------------------------------


def test_sell_intent_uses_absolute_notional(settings: Settings) -> None:
    """A sell of 1 BTC at 12k is still 12k of risk."""
    ctx = RiskContext()
    result = check_intent(
        _intent(side=Side.SELL, quantity="1", limit_price="12_000"),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    assert result.approved is False
    assert any("per_symbol_notional_breach" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


def test_intent_at_exactly_per_symbol_cap_approved(settings: Settings) -> None:
    """Caps are inclusive: x <= cap."""
    ctx = RiskContext()
    result = check_intent(
        _intent(quantity="1", limit_price="10_000"),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    assert result.approved is True


def test_intent_at_exactly_gross_cap_approved(settings: Settings) -> None:
    ctx = RiskContext(
        notional_by_symbol={"ETH-USD": Decimal("10_000")},
        gross_notional=Decimal("10_000"),
    )
    result = check_intent(
        _intent(quantity="1", limit_price="10_000"),
        ctx=ctx,
        settings=settings,
        last_price=None,
    )
    assert result.approved is True
