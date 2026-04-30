"""
backtester.blotter — append-only run log.

Holds:

  - ``fills``           every Fill produced by the broker, in chronological order
  - ``rejections``      every OrderIntent the risk gate rejected, with reasons
  - ``equity_curve``    one ``(ts_ns, equity_usd)`` sample per bar
  - ``starting_cash``   initial portfolio NAV; the engine seeds this once
  - ``borrow_paid``     running total of USD borrow charges accrued on
                        short positions; the engine bumps this each bar
                        via :meth:`add_borrow` and the equity formula
                        subtracts it (parallel to per-fill ``fee``)

This is intentionally unopinionated about reporting — TASK-023 wraps a
``Blotter`` to compute Sharpe, drawdown, hit rate, etc.  Keeping the
blotter dumb keeps it serializable and reusable across walk-forward
windows.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from fincept_core.schemas import Fill, Side


class RejectedIntent(BaseModel):
    """Compact record of a risk-gate rejection.

    We don't store the full ``OrderIntent`` here because intent fields
    that matter for postmortem (symbol, side, quantity, the reasons) are
    flattened for cheap aggregation in :func:`compute_metrics`.
    """

    model_config = ConfigDict(frozen=True)

    ts_ns: int
    order_id: str
    strategy_id: str
    symbol: str
    side: Side
    quantity: Decimal
    reasons: list[str] = Field(default_factory=list)


class Blotter(BaseModel):
    """Append-only fills + equity curve + rejected intents."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    fills: list[Fill] = Field(default_factory=list)
    rejections: list[RejectedIntent] = Field(default_factory=list)
    equity_curve: list[tuple[int, Decimal]] = Field(default_factory=list)
    starting_cash: Decimal = Decimal("100000")
    # Running total of borrow charges paid on short positions across the
    # whole run.  Kept aggregate (not per-symbol) because the engine
    # subtracts it from equity in one shot — parallels how per-fill
    # ``fee`` is summed.  Zero unless ``CostModel.default_borrow_bps_annual``
    # or a per-symbol override is set.
    borrow_paid: Decimal = Decimal(0)

    def add_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    def add_rejection(self, rejection: RejectedIntent) -> None:
        self.rejections.append(rejection)

    def add_borrow(self, amount: Decimal) -> None:
        """Accrue ``amount`` USD of borrow cost.  Caller is responsible
        for skipping no-op zero/negative charges; this method takes the
        amount at face value to keep the accounting auditable."""
        self.borrow_paid += amount

    def mark_equity(self, ts_ns: int, equity_usd: Decimal) -> None:
        self.equity_curve.append((ts_ns, equity_usd))

    @property
    def final_equity(self) -> Decimal:
        if not self.equity_curve:
            return self.starting_cash
        return self.equity_curve[-1][1]
