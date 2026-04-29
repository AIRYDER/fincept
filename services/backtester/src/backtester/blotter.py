"""
backtester.blotter — append-only run log.

Holds:

  - ``fills``           every Fill produced by the broker, in chronological order
  - ``equity_curve``    one ``(ts_ns, equity_usd)`` sample per bar
  - ``starting_cash``   initial portfolio NAV; the engine seeds this once

This is intentionally unopinionated about reporting — TASK-023 wraps a
``Blotter`` to compute Sharpe, drawdown, hit rate, etc.  Keeping the
blotter dumb keeps it serializable and reusable across walk-forward
windows.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from fincept_core.schemas import Fill


class Blotter(BaseModel):
    """Append-only fills + equity curve."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    fills: list[Fill] = Field(default_factory=list)
    equity_curve: list[tuple[int, Decimal]] = Field(default_factory=list)
    starting_cash: Decimal = Decimal("100000")

    def add_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    def mark_equity(self, ts_ns: int, equity_usd: Decimal) -> None:
        self.equity_curve.append((ts_ns, equity_usd))

    @property
    def final_equity(self) -> Decimal:
        if not self.equity_curve:
            return self.starting_cash
        return self.equity_curve[-1][1]
