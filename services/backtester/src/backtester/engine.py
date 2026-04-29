"""
backtester.engine — main event loop.

Sequencing per bar (the order is the contract):

  1. ``ctx.now_ns`` is set to ``bar.ts_event``.
  2. ``strategy.on_bar(ctx, bar)`` runs — it may submit / cancel orders
     for execution starting at the *next* bar (PIT-correct: no peeking).
     Newly-submitted orders enter the broker's open book but do NOT fill
     against the current bar.
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

Position math is split out in ``_update_positions`` so the four cases
(open more / close some / cross flat / cross flip) are individually
testable.  Spec landmines around partial-flip realized P&L caught one
real bug — see ``tests/test_engine.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from backtester.blotter import Blotter
from backtester.broker import SimBroker
from backtester.datasource import BarsDataSource
from fincept_core.logging import get_logger
from fincept_core.portfolio import apply_fill_to_position
from fincept_core.schemas import Fill, OrderIntent, Position
from fincept_sdk import Strategy, StrategyContext

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
    ) -> None:
        self.strategy = strategy
        self.datasource = datasource
        self.broker = broker if broker is not None else SimBroker()
        self.blotter = blotter if blotter is not None else Blotter()
        self.features = features if features is not None else {}
        self._last_close: dict[str, Decimal] = {}

    async def run(self) -> Blotter:
        ctx = _Context(self)
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

                # 3. Mark-to-market: update last-close, then snapshot equity.
                self._last_close[bar.symbol] = bar.close
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
        return cash + realized + unrealized - fees
