"""Tests for the borrow-cost holding-cost model.

Two layers of coverage:

* :class:`backtester.costs.CostModel` unit tests verifying the
  ``accrue_borrow`` formula and per-symbol override resolution.

* End-to-end :class:`backtester.engine.BacktestEngine` tests proving
  the accrual is plumbed correctly: the blotter records cumulative
  borrow, equity is reduced by exactly that amount, and the long-only
  default of zero borrow keeps every existing backtest bit-for-bit
  identical.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

import pytest
from pydantic import BaseModel

from backtester.blotter import Blotter
from backtester.broker import SimBroker
from backtester.costs import SECONDS_PER_YEAR, CostModel, SymbolCosts
from backtester.datasource import BarsDataSource
from backtester.engine import BacktestEngine
from backtester.runner import make_bar_reader
from fincept_core.ids import new_id
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    Fill,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    TradeEvent,
    Venue,
)
from fincept_sdk import Strategy, StrategyContext

SYMBOL = "SHORT-TEST"
# Use 1-day bars so per-bar charges land on exact Decimals (avoids the
# 1/365 repeating-decimal that 1-minute bars would introduce; the tests
# only care about the accrual contract, not the bar duration).
ONE_BAR_NS = 24 * 60 * 60 * 1_000_000_000  # 1 day in nanoseconds


def _decimal_close(a: Decimal, b: Decimal, *, tol: Decimal = Decimal("1e-20")) -> bool:
    """Compare two Decimals up to a small absolute tolerance.

    The engine's equity formula combines large operands ($100k cash) with
    sub-cent borrow charges, so a few least-significant digits get
    truncated by Decimal's default 28-digit context.  This helper makes
    that precision loss explicit at the assertion site instead of
    silently flaking.
    """
    return (a - b).copy_abs() <= tol


# --------------------------------------------------------------------------- #
# CostModel.accrue_borrow unit tests                                          #
# --------------------------------------------------------------------------- #


def test_accrue_borrow_zero_on_long_position() -> None:
    """quantity > 0 (long) never incurs borrow."""
    model = CostModel(default_borrow_bps_annual=Decimal("100"))
    charge = model.accrue_borrow(
        quantity=Decimal("10"),
        mark_price=Decimal("100"),
        elapsed_seconds=Decimal("3600"),
    )
    assert charge == Decimal(0)


def test_accrue_borrow_zero_on_flat_position() -> None:
    """quantity == 0 (flat) never incurs borrow."""
    model = CostModel(default_borrow_bps_annual=Decimal("100"))
    charge = model.accrue_borrow(
        quantity=Decimal(0),
        mark_price=Decimal("100"),
        elapsed_seconds=Decimal("3600"),
    )
    assert charge == Decimal(0)


def test_accrue_borrow_zero_on_zero_elapsed() -> None:
    """elapsed_seconds <= 0 returns 0 even for shorts (no time to charge)."""
    model = CostModel(default_borrow_bps_annual=Decimal("100"))
    assert model.accrue_borrow(
        quantity=Decimal("-10"),
        mark_price=Decimal("100"),
        elapsed_seconds=Decimal(0),
    ) == Decimal(0)
    assert model.accrue_borrow(
        quantity=Decimal("-10"),
        mark_price=Decimal("100"),
        elapsed_seconds=Decimal("-5"),
    ) == Decimal(0)


def test_accrue_borrow_zero_on_zero_default_rate() -> None:
    """Default rate of 0 (back-compat) yields no charge even on shorts."""
    model = CostModel()  # default_borrow_bps_annual = 0
    charge = model.accrue_borrow(
        quantity=Decimal("-10"),
        mark_price=Decimal("100"),
        elapsed_seconds=Decimal("3600"),
    )
    assert charge == Decimal(0)


def test_accrue_borrow_zero_on_zero_mark() -> None:
    """A non-positive mark price (warmup edge case) returns 0."""
    model = CostModel(default_borrow_bps_annual=Decimal("100"))
    assert model.accrue_borrow(
        quantity=Decimal("-10"),
        mark_price=Decimal(0),
        elapsed_seconds=Decimal("3600"),
    ) == Decimal(0)


def test_accrue_borrow_basic_short_charge() -> None:
    """1% annual, $10k notional, 1 day => exactly $10000 * 0.01 * 1/365."""
    model = CostModel(default_borrow_bps_annual=Decimal("100"))
    one_day_seconds = Decimal(24 * 60 * 60)
    charge = model.accrue_borrow(
        quantity=Decimal("-100"),  # short 100 shares
        mark_price=Decimal("100"),  # @ $100 each = $10000 notional
        elapsed_seconds=one_day_seconds,
    )
    expected = (
        Decimal("10000") * Decimal("100") / Decimal(10000) * one_day_seconds / SECONDS_PER_YEAR
    )
    assert charge == expected
    # Sanity-check expected matches the layperson formula 1% / 365 of $10k
    # ≈ $0.27397/day.
    assert abs(charge - Decimal("0.27397260273972602739")) < Decimal("1e-15")


def test_accrue_borrow_scales_linearly_with_notional() -> None:
    """Doubling |quantity| (or price) doubles the charge.

    Use a full-year elapsed window so the bps/SECONDS_PER_YEAR factor
    collapses to bps/10000 and the linear-scaling assertion is exact.
    """
    # 250 bps = 2.5% annual; on $4k notional => $100/year exactly.
    model = CostModel(default_borrow_bps_annual=Decimal("250"))
    base = model.accrue_borrow(
        quantity=Decimal("-50"),
        mark_price=Decimal("80"),
        elapsed_seconds=SECONDS_PER_YEAR,
    )
    double_qty = model.accrue_borrow(
        quantity=Decimal("-100"),
        mark_price=Decimal("80"),
        elapsed_seconds=SECONDS_PER_YEAR,
    )
    double_price = model.accrue_borrow(
        quantity=Decimal("-50"),
        mark_price=Decimal("160"),
        elapsed_seconds=SECONDS_PER_YEAR,
    )
    assert base == Decimal("100")
    assert double_qty == Decimal("200") == base * 2
    assert double_price == Decimal("200") == base * 2


def test_accrue_borrow_scales_linearly_with_elapsed_time() -> None:
    """Doubling elapsed_seconds doubles the charge.

    Use full-year and half-year intervals so the bps/SECONDS_PER_YEAR
    ratio collapses to clean integers and the assertion is exact.
    """
    # 200 bps = 2% annual; on $10k notional => $200 / year, $100 / half.
    model = CostModel(default_borrow_bps_annual=Decimal("200"))
    one_year = model.accrue_borrow(
        quantity=Decimal("-100"),
        mark_price=Decimal("100"),
        elapsed_seconds=SECONDS_PER_YEAR,
    )
    half_year = model.accrue_borrow(
        quantity=Decimal("-100"),
        mark_price=Decimal("100"),
        elapsed_seconds=SECONDS_PER_YEAR / Decimal(2),
    )
    assert one_year == Decimal("200")
    assert half_year == Decimal("100")
    assert one_year == half_year * 2


def test_accrue_borrow_per_symbol_override_wins() -> None:
    """A per-symbol borrow rate overrides the global default for that symbol."""
    model = CostModel(
        default_borrow_bps_annual=Decimal("50"),
        per_symbol={
            "HTB": SymbolCosts(borrow_bps_annual=Decimal("500")),
        },
    )
    # HTB gets the 500 bps rate; SPY (no override) gets default 50.
    htb_charge = model.accrue_borrow(
        quantity=Decimal("-10"),
        mark_price=Decimal("100"),
        elapsed_seconds=Decimal("3600"),
        symbol="HTB",
    )
    spy_charge = model.accrue_borrow(
        quantity=Decimal("-10"),
        mark_price=Decimal("100"),
        elapsed_seconds=Decimal("3600"),
        symbol="SPY",
    )
    assert htb_charge == spy_charge * 10  # 500 / 50 = 10x


def test_accrue_borrow_per_symbol_zero_override_disables_default() -> None:
    """A per-symbol override of 0 wins over a non-zero default (silver names)."""
    model = CostModel(
        default_borrow_bps_annual=Decimal("100"),
        per_symbol={
            "ETF": SymbolCosts(borrow_bps_annual=Decimal(0)),
        },
    )
    charge = model.accrue_borrow(
        quantity=Decimal("-10"),
        mark_price=Decimal("100"),
        elapsed_seconds=Decimal("3600"),
        symbol="ETF",
    )
    assert charge == Decimal(0)


# --------------------------------------------------------------------------- #
# Engine-level integration tests                                              #
# --------------------------------------------------------------------------- #


def _bar(ts_ns: int, *, close: str = "100") -> BarEvent:
    """Constant-price bar at ``ts_ns`` (used to keep math predictable)."""
    return BarEvent(
        venue=Venue.PAPER,
        symbol=SYMBOL,
        asset_class=AssetClass.EQUITY,
        ts_event=ts_ns,
        ts_recv=ts_ns,
        freq="1m",
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1000000"),
        trades=10,
        vwap=None,
    )


def _datasource(bars: list[BarEvent]) -> BarsDataSource:
    by_symbol: dict[str, list[BarEvent]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    return BarsDataSource(
        symbols=list(by_symbol),
        freq="1m",
        start_ns=bars[0].ts_event,
        end_ns=bars[-1].ts_event + 1,
        bar_reader=make_bar_reader(by_symbol),
    )


class _OpenShortOnceStrategy(Strategy):
    """Submits one SELL on the first bar, then holds the resulting short.

    Used to seed a known short position for borrow-accrual tests.
    Quantity is fixed; the engine fills it on the next bar against
    ``bar.open``, so by bar 2 the position is short ``quantity``.
    """

    strategy_id: ClassVar[str] = "borrow.short.test"
    symbols: ClassVar[list[str]] = []  # populated per-instance

    def __init__(self, *, quantity: Decimal) -> None:
        self.quantity = quantity
        self._submitted = False

    def on_start(self, ctx: StrategyContext) -> None:
        return

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        if self._submitted:
            return
        self._submitted = True
        ctx.submit(
            OrderIntent(
                order_id=new_id(),
                decision_id=new_id(),
                ts_event=bar.ts_event,
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                venue=Venue.PAPER,
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=self.quantity,
                time_in_force=TimeInForce.GTC,
                tags={"source": "borrow-test"},
            )
        )

    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        return

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        return

    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        return

    def on_stop(self, ctx: StrategyContext) -> None:
        return


def _expected_per_bar_charge(
    *, quantity: Decimal, mark: Decimal, bps: Decimal, bar_ns: int
) -> Decimal:
    """Replicate accrue_borrow's math so tests assert against the exact value."""
    elapsed = Decimal(bar_ns) / Decimal(1_000_000_000)
    notional = quantity.copy_abs() * mark
    return notional * bps / Decimal(10000) * elapsed / SECONDS_PER_YEAR


async def test_engine_no_borrow_charge_when_long_only() -> None:
    """Default cost model + no shorts => blotter.borrow_paid stays at 0."""
    bars = [_bar(ONE_BAR_NS), _bar(2 * ONE_BAR_NS), _bar(3 * ONE_BAR_NS)]

    class _DoNothing(Strategy):
        strategy_id: ClassVar[str] = "noop"
        symbols: ClassVar[list[str]] = [SYMBOL]

        def on_start(self, ctx: StrategyContext) -> None:
            return

        def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
            return

        def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
            return

        def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
            return

        def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
            return

        def on_stop(self, ctx: StrategyContext) -> None:
            return

    blotter = Blotter()
    engine = BacktestEngine(
        strategy=_DoNothing(),
        datasource=_datasource(bars),
        broker=SimBroker(cost_model=CostModel(default_borrow_bps_annual=Decimal("100"))),
        blotter=blotter,
    )
    await engine.run()
    # Even though the rate is non-zero, no short positions => no charge.
    assert blotter.borrow_paid == Decimal(0)


async def test_engine_charges_borrow_per_bar_on_short() -> None:
    """Short held over N bars => borrow_paid is sum of N-1 per-bar charges.

    With 1-day bars and bps=365, the per-bar charge on a $10k short is
    exactly $1.00 — ``10000 * 0.0365 * 86400 / 31_536_000``.  The first
    bar carries no elapsed interval (no prev timestamp) and the SELL
    submitted on bar 1 doesn't fill until bar 2, so the position is
    -100 at the end of bars 2..5 — 4 accruing bars.
    """
    bars = [_bar(i * ONE_BAR_NS) for i in range(1, 6)]  # bars 1..5
    bps = Decimal("365")  # 3.65% annual; per-day charge on $10k is $1.00
    blotter = Blotter()
    engine = BacktestEngine(
        strategy=_OpenShortOnceStrategy(quantity=Decimal("100")),
        datasource=_datasource(bars),
        broker=SimBroker(cost_model=CostModel(default_borrow_bps_annual=bps)),
        blotter=blotter,
    )
    await engine.run()

    # Short fills on bar 2 against open price 100.  Position -100 at
    # end of bars 2..5 — 4 accruing bars at exactly $1.00 each.
    per_bar = _expected_per_bar_charge(
        quantity=Decimal("-100"),
        mark=Decimal("100"),
        bps=bps,
        bar_ns=ONE_BAR_NS,
    )
    assert per_bar == Decimal("1"), "sanity: 1-day bar at 365 bps on $10k = $1"
    expected_total = per_bar * 4
    assert blotter.borrow_paid == expected_total


async def test_engine_first_bar_charges_no_borrow() -> None:
    """First bar always has prev_bar_ts=None => zero accrual that bar."""
    bars = [_bar(ONE_BAR_NS)]  # single bar
    blotter = Blotter()
    engine = BacktestEngine(
        strategy=_OpenShortOnceStrategy(quantity=Decimal("100")),
        datasource=_datasource(bars),
        broker=SimBroker(cost_model=CostModel(default_borrow_bps_annual=Decimal("365"))),
        blotter=blotter,
    )
    await engine.run()
    # Only one bar => no second prev_bar_ts to compute elapsed against.
    # Also the SELL submitted on bar 1 doesn't fill on bar 1.
    assert blotter.borrow_paid == Decimal(0)


async def test_engine_borrow_reduces_final_equity_by_exact_amount() -> None:
    """Run identical strategies with borrow=0 and borrow=N; verify equity
    delta matches blotter.borrow_paid up to Decimal precision.

    The equity formula combines $100k cash with sub-cent borrow charges,
    so the subtraction loses a few least-significant digits past the
    28-digit Decimal context window.  We assert with a 1e-20 tolerance
    — well below any realistic accounting boundary.
    """
    bars = [_bar(i * ONE_BAR_NS) for i in range(1, 6)]

    # Baseline: borrow rate 0.
    blotter_base = Blotter()
    engine_base = BacktestEngine(
        strategy=_OpenShortOnceStrategy(quantity=Decimal("100")),
        datasource=_datasource(bars),
        broker=SimBroker(cost_model=CostModel()),  # default 0 bps borrow
        blotter=blotter_base,
    )
    await engine_base.run()
    assert blotter_base.borrow_paid == Decimal(0)

    # With borrow rate.
    blotter_borrow = Blotter()
    engine_borrow = BacktestEngine(
        strategy=_OpenShortOnceStrategy(quantity=Decimal("100")),
        datasource=_datasource(bars),
        broker=SimBroker(cost_model=CostModel(default_borrow_bps_annual=Decimal("365"))),
        blotter=blotter_borrow,
    )
    await engine_borrow.run()
    assert blotter_borrow.borrow_paid > 0

    delta = blotter_base.final_equity - blotter_borrow.final_equity
    assert _decimal_close(delta, blotter_borrow.borrow_paid)


async def test_engine_run_resets_prev_bar_ts() -> None:
    """Re-using an engine across runs must not leak elapsed time from a
    previous run (which would over-charge the new run's first bar)."""
    bars = [_bar(i * ONE_BAR_NS) for i in range(1, 4)]
    engine = BacktestEngine(
        strategy=_OpenShortOnceStrategy(quantity=Decimal("100")),
        datasource=_datasource(bars),
        broker=SimBroker(cost_model=CostModel(default_borrow_bps_annual=Decimal("365"))),
        blotter=Blotter(),
    )
    await engine.run()
    first_run_total = engine.blotter.borrow_paid
    assert first_run_total > 0

    # Second run with a fresh blotter; expect identical accrual.
    fresh_blotter = Blotter()
    engine.blotter = fresh_blotter
    engine.strategy = _OpenShortOnceStrategy(quantity=Decimal("100"))
    engine.datasource = _datasource(bars)
    await engine.run()
    assert fresh_blotter.borrow_paid == first_run_total


def test_report_surfaces_borrow_paid_total() -> None:
    """compute_metrics copies blotter.borrow_paid to BacktestReport."""
    from backtester.report import compute_metrics

    blotter = Blotter()
    blotter.add_borrow(Decimal("12.34"))
    # Need at least one equity sample so the report doesn't degenerate.
    blotter.mark_equity(0, Decimal("100000"))
    report = compute_metrics(blotter, bars_per_year=525_600)
    assert report.borrow_paid_total == pytest.approx(12.34)
