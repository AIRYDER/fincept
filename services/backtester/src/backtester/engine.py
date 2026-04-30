"""
backtester.engine — main event loop.

Sequencing per bar (the order is the contract):

  1. ``ctx.now_ns`` is set to ``bar.ts_event``.
  2. ``strategy.on_bar(ctx, bar)`` runs — it may submit / cancel orders
     for execution starting at the *next* bar (PIT-correct: no peeking).
     Newly-submitted orders that pass the optional risk gate enter the
     broker's open book; rejections are appended to ``blotter.rejections``
     and never fill.  Newly-accepted orders do NOT fill against the
     current bar (PIT integrity, see ``submitted_in_bar``).
  3. ``broker.on_bar(bar)`` runs — open orders submitted on prior bars
     are evaluated against this bar's OHLC.  Any fills are appended to
     the blotter and dispatched to ``strategy.on_fill``.
  4. ``_update_positions`` updates the position book with each fill.
  5. The engine marks last-close per symbol and snapshots equity.

The "submit before fill, but fill against this bar" ordering is wrong —
that's instant-fill cheating.  We deliberately fill orders submitted on
bar T-1 against bar T (the bar in step 3) so a strategy that reacts to
bar T-1 is paying bar-T execution costs, not bar T-1 prices.  The
``submitted_in_bar`` set tracks orders new this tick and excludes them
from the broker's first-bar fill scan.

Risk gate (optional): pass ``risk_settings`` (a :class:`Settings`
instance carrying ``MAX_NOTIONAL_USD_PER_SYMBOL`` and
``MAX_GROSS_NOTIONAL_USD``) to mirror the live OMS gating in
``services/oms/src/oms/main.py``.  The same ``risk.check_intent``
function gates both surfaces, so a backtest result reflects exactly
what the live OMS would let through.  Without ``risk_settings``, the
engine behaves as before (no gate).

Position math is split out in ``_update_positions`` so the four cases
(open more / close some / cross flat / cross flip) are individually
testable.  Spec landmines around partial-flip realized P&L caught one
real bug — see ``tests/test_engine.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from backtester.blotter import Blotter, RejectedIntent
from backtester.broker import SimBroker
from backtester.datasource import BarsDataSource
from fincept_core.config import Settings
from fincept_core.logging import get_logger
from fincept_core.portfolio import apply_fill_to_position
from fincept_core.schemas import BarEvent, Fill, OrderIntent, Position
from fincept_sdk import Strategy, StrategyContext
from risk.checks import RiskContext, check_intent

log = get_logger(__name__)


class _Context(StrategyContext):
    """Concrete StrategyContext for in-process simulation.

    Stays a thin pass-through to the engine — the engine owns positions,
    fills, and the broker; the context just exposes them via the SDK
    Protocol so strategies don't depend on backtester internals.
    """

    def __init__(self, engine: BacktestEngine) -> None:
        self._engine = engine
        self.now_ns: int = 0
        self.positions: dict[str, Position] = {}
        # Orders submitted on the current bar; held back from the broker
        # fill scan so a strategy can't fill its own just-submitted order
        # in the same bar (PIT integrity).
        self._submitted_this_bar: set[str] = set()

    def submit(self, intent: OrderIntent) -> str:
        # Optional pre-trade risk gate.  When ``risk_settings`` is None
        # the engine behaves like the original ungated version.  When
        # set, every intent runs through ``risk.check_intent`` with the
        # same logic the live OMS uses (per-symbol cap, gross cap,
        # kill-switch — kill-switch is always False in backtest because
        # there's no alert stream).  Rejections are recorded on the
        # blotter and the intent never reaches the broker's open book.
        if self._engine.risk_settings is not None:
            decision = self._engine._gate_intent(intent, ctx=self)
            if not decision.approved:
                self._engine.blotter.add_rejection(
                    RejectedIntent(
                        ts_ns=self.now_ns,
                        order_id=intent.order_id,
                        strategy_id=intent.strategy_id,
                        symbol=intent.symbol,
                        side=intent.side,
                        quantity=intent.quantity,
                        reasons=list(decision.reasons),
                    )
                )
                log.warning(
                    "backtest.risk.rejected",
                    order_id=intent.order_id,
                    symbol=intent.symbol,
                    reasons=list(decision.reasons),
                )
                return intent.order_id
        order = self._engine.broker.submit(intent)
        self._submitted_this_bar.add(order.order_id)
        return order.order_id

    def cancel(self, order_id: str) -> None:
        self._engine.broker.cancel(order_id)
        self._submitted_this_bar.discard(order_id)

    def get_feature(self, name: str, symbol: str) -> float | None:
        return self._engine.features.get((name, symbol))

    def log(self, msg: str, **kwargs: Any) -> None:
        log.info(msg, strategy_id=self._engine.strategy.strategy_id, **kwargs)


class BacktestEngine:
    """Drive a strategy against a historical bar stream."""

    def __init__(
        self,
        strategy: Strategy,
        datasource: BarsDataSource,
        *,
        broker: SimBroker | None = None,
        blotter: Blotter | None = None,
        features: dict[tuple[str, str], float] | None = None,
        risk_settings: Settings | None = None,
    ) -> None:
        self.strategy = strategy
        self.datasource = datasource
        self.broker = broker if broker is not None else SimBroker()
        self.blotter = blotter if blotter is not None else Blotter()
        self.features = features if features is not None else {}
        self.risk_settings = risk_settings
        self._last_close: dict[str, Decimal] = {}
        # Timestamp of the most recently processed bar; ``None`` until the
        # first bar lands, so the first bar charges no borrow (no
        # elapsed interval yet).  Reset on every ``run()`` call.
        self._prev_bar_ts: int | None = None

    # ------------------------------------------------------------------
    # Risk gate plumbing (active only when ``risk_settings`` is set).
    # ------------------------------------------------------------------

    def _build_risk_context(self, ctx: _Context) -> RiskContext:
        """Snapshot per-symbol notionals + gross from current positions.

        Mirrors what :func:`risk.snapshot.build_context` does in the
        live OMS but reads from the in-process engine state instead of
        Redis.  Stale-price protection: if a symbol has no observed
        last-close yet (warmup), it's omitted from notional rather than
        priced at zero — same fail-safe the live snapshot uses.
        """
        notional_by_symbol: dict[str, Decimal] = {}
        gross = Decimal(0)
        for symbol, pos in ctx.positions.items():
            if pos.quantity == 0:
                continue
            price = self._last_close.get(symbol)
            if price is None:
                continue
            notional = (Decimal(pos.quantity) * price).copy_abs()
            notional_by_symbol[symbol] = (
                notional_by_symbol.get(symbol, Decimal(0)) + notional
            )
            gross += notional
        return RiskContext(
            notional_by_symbol=notional_by_symbol,
            gross_notional=gross,
            kill_switch_engaged=False,
        )

    def _gate_intent(self, intent: OrderIntent, *, ctx: _Context) -> Any:
        """Run the live ``risk.check_intent`` gate with backtest state.

        Last-price preference order matches the live OMS:
          1. ``intent.limit_price`` if it's a LIMIT order
          2. last observed close for the symbol
          3. None (will be rejected as ``no_reference_price``)
        """
        risk_ctx = self._build_risk_context(ctx)
        last_price = self._last_close.get(intent.symbol)
        # ``risk_settings`` is guaranteed non-None by the caller.
        assert self.risk_settings is not None
        return check_intent(
            intent,
            ctx=risk_ctx,
            settings=self.risk_settings,
            last_price=last_price,
        )

    async def run(self) -> Blotter:
        ctx = _Context(self)
        # Reset borrow-tracking state so a single engine instance can be
        # re-used across walk-forward windows without leaking elapsed
        # time from the previous run.
        self._prev_bar_ts = None
        self.strategy.on_start(ctx)
        try:
            async for bar in self.datasource.replay():
                ctx.now_ns = bar.ts_event
                ctx._submitted_this_bar.clear()

                # 1. Strategy decision phase — may submit orders.
                self.strategy.on_bar(ctx, bar)

                # 2. Fill phase — only orders that existed BEFORE the
                # strategy ran this bar are eligible.  Newly-submitted
                # orders fill on the next bar.
                eligible = {
                    oid: order
                    for oid, order in self.broker.open_orders.items()
                    if oid not in ctx._submitted_this_bar
                }
                # Temporarily swap the open book so on_bar only sees eligible.
                fresh = {
                    oid: self.broker.open_orders[oid]
                    for oid in ctx._submitted_this_bar
                    if oid in self.broker.open_orders
                }
                self.broker.open_orders = eligible
                fills = self.broker.on_bar(bar)
                # Re-merge fresh orders with whatever the broker left open.
                for oid, order in fresh.items():
                    self.broker.open_orders[oid] = order

                for fill in fills:
                    self.blotter.add_fill(fill)
                    self._update_positions(ctx, fill)
                    self.strategy.on_fill(ctx, fill)

                # 3. Mark-to-market: update last-close, accrue borrow on
                #    any short positions for the elapsed interval, then
                #    snapshot equity.
                self._last_close[bar.symbol] = bar.close
                self._accrue_borrow_for_bar(ctx, bar)
                self._prev_bar_ts = bar.ts_event
                self.blotter.mark_equity(bar.ts_event, self._compute_equity(ctx))
        finally:
            self.strategy.on_stop(ctx)
        return self.blotter

    # ------------------------------------------------------------------
    # Position book — delegates to the shared apply_fill_to_position so the
    # backtester and the live portfolio service evolve identically.
    # ------------------------------------------------------------------

    def _update_positions(self, ctx: _Context, fill: Fill) -> None:
        prev = ctx.positions.get(fill.symbol)
        ctx.positions[fill.symbol] = apply_fill_to_position(
            prev, fill, strategy_id=self.strategy.strategy_id
        )

    # ------------------------------------------------------------------
    # Borrow cost accrual — short positions only, prorated per bar.
    # ------------------------------------------------------------------

    def _accrue_borrow_for_bar(self, ctx: _Context, bar: BarEvent) -> None:
        """Charge per-bar borrow on any short position in the book.

        Skipped on the first bar (no previous timestamp).  Elapsed time
        is the wall-clock delta from the last processed bar in *any*
        symbol — chronologically increasing in the engine's bar feed —
        so a short held across a multi-symbol bar stream still gets
        charged exactly once per elapsed interval.

        Marks: uses ``_last_close[symbol]`` for the symbol's most recent
        observed close.  If the symbol hasn't published a bar yet (warmup
        for a paired ticker) the position is skipped — same
        stale-price fail-safe used by ``_build_risk_context``.
        """
        if self._prev_bar_ts is None:
            return
        elapsed_ns = bar.ts_event - self._prev_bar_ts
        if elapsed_ns <= 0:
            return
        elapsed_seconds = Decimal(elapsed_ns) / Decimal(1_000_000_000)
        cost_model = self.broker.cost_model
        for symbol, position in ctx.positions.items():
            if position.quantity >= 0:
                continue
            mark = self._last_close.get(symbol)
            if mark is None:
                continue
            charge = cost_model.accrue_borrow(
                quantity=position.quantity,
                mark_price=mark,
                elapsed_seconds=elapsed_seconds,
                symbol=symbol,
            )
            if charge > 0:
                self.blotter.add_borrow(charge)

    # ------------------------------------------------------------------
    # Equity snapshot — full mark-to-market across all positions.
    # ------------------------------------------------------------------

    def _compute_equity(self, ctx: _Context) -> Decimal:
        cash: Decimal = self.blotter.starting_cash
        realized: Decimal = Decimal(0)
        for pos in ctx.positions.values():
            realized += pos.realized_pnl
        unrealized: Decimal = Decimal(0)
        for pos in ctx.positions.values():
            last = self._last_close.get(pos.symbol)
            if last is None or pos.quantity == 0:
                continue
            unrealized += (last - pos.avg_cost) * pos.quantity
        fees: Decimal = Decimal(0)
        for fill in self.blotter.fills:
            fees += fill.fee
        # Borrow accrual is tracked aggregate on the blotter (parallel
        # to fees) so we can subtract it in one shot here.
        return (
            cash + realized + unrealized - fees - self.blotter.borrow_paid
        )
