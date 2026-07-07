"""Execution-aware cost model for training metrics (Tier 2.5).

The Sharpe-769 artifact demonstrated that frictionless training metrics
must never reach a promotion decision. This module provides the cost
model that converts gross (frictionless) training returns into net
(execution-aware) returns, so the dossier records both.

The model is intentionally simple (flat bps per trade) rather than the
backtester's sophisticated square-root impact model — the training
layer doesn't have bar-level volume or participation data. The
backtester's :class:`backtester.costs.CostModel` remains the
authoritative execution simulator for full backtests; this module
provides the **training-side** cost adjustment that ensures the
dossier's training Sharpe is net-of-cost, not frictionless.

The cost model is versioned (like the settlement cost model) so a
later cost-model change does not silently rewrite history. Both gross
and net metrics are stored in the dossier, preserving the audit trail.

Design:
  * Pure-Python, no numpy at module level (imported lazily by callers).
  * Pydantic v2 models for the cost config, consistent with the rest
    of ``quant_foundry``.
  * The cost model is frozen + ``extra='forbid'`` for audit integrity.
  * A default cost model is provided (matches the settlement default:
    5 bps fee, 3 bps spread, 0 bps slippage) so training and
    settlement share the same baseline cost assumptions.
"""

from __future__ import annotations

import math
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "TrainingCostModel",
    "CostAwareMetrics",
    "apply_training_costs",
    "compute_cost_aware_metrics",
    "DEFAULT_TRAINING_COST_MODEL",
]


# --------------------------------------------------------------------------- #
# Cost model                                                                  #
# --------------------------------------------------------------------------- #


class TrainingCostModel(BaseModel):
    """Execution-aware cost model for training metrics.

    All cost figures are in basis points (1 bps = 0.01%). The
    ``version`` field is recorded in the dossier so a later cost-model
    change does not silently rewrite history.

    Fields:
        version: cost model version string (e.g. ``"v1.default"``).
        fee_bps: round-trip exchange/broker fee per trade.
        spread_bps: modeled bid-ask spread (round-trip) per trade.
        slippage_bps: modeled market-impact / slippage (round-trip)
            per trade.
        borrow_bps_per_day: financing/borrow cost per calendar day
            held (applied to short positions only; longs pay 0).

    The default model matches the settlement default (5 bps fee,
    3 bps spread, 0 bps slippage) so training and settlement share
    the same baseline cost assumptions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "v1.default"
    fee_bps: float = Field(default=5.0, ge=0.0)
    spread_bps: float = Field(default=3.0, ge=0.0)
    slippage_bps: float = Field(default=0.0, ge=0.0)
    borrow_bps_per_day: float = Field(default=0.0, ge=0.0)

    @field_validator("version")
    @classmethod
    def _version_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("version must be non-empty")
        return v

    @property
    def round_trip_bps(self) -> float:
        """Total round-trip cost in bps (fee + spread + slippage)."""
        return self.fee_bps + self.spread_bps + self.slippage_bps

    @property
    def round_trip_fraction(self) -> float:
        """Total round-trip cost as a fraction of notional."""
        return self.round_trip_bps / 10_000.0


DEFAULT_TRAINING_COST_MODEL = TrainingCostModel()


# --------------------------------------------------------------------------- #
# Result type                                                                  #
# --------------------------------------------------------------------------- #


class CostAwareMetrics(BaseModel):
    """Training metrics with both gross and net-of-cost values.

    The dossier records both so the audit trail preserves the gross
    edge (for debugging) while the promotion gate uses the net edge
    (for decisions). The ``cost_model_version`` field ties the net
    metrics to a specific cost model version.

    Fields:
        sharpe_gross: frictionless Sharpe ratio (no costs).
        sharpe_net: Sharpe ratio after transaction costs.
        max_drawdown_gross: max drawdown without costs.
        max_drawdown_net: max drawdown after costs.
        win_rate_gross: win rate without costs.
        win_rate_net: win rate after costs.
        mean_return_gross: mean per-period return without costs.
        mean_return_net: mean per-period return after costs.
        turnover: fraction of periods where the position changed
            (drives cost adjustment).
        total_cost_bps: total cost applied (round-trip bps × turnover).
        cost_model_version: the cost model version used.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sharpe_gross: float
    sharpe_net: float
    max_drawdown_gross: float
    max_drawdown_net: float
    win_rate_gross: float
    win_rate_net: float
    mean_return_gross: float
    mean_return_net: float
    turnover: float
    total_cost_bps: float
    cost_model_version: str


# --------------------------------------------------------------------------- #
# Core cost application                                                        #
# --------------------------------------------------------------------------- #


def apply_training_costs(
    gross_returns: list[float],
    positions: list[float],
    cost_model: TrainingCostModel,
    *,
    holding_days: int = 1,
) -> list[float]:
    """Convert gross per-period returns to net-of-cost returns.

    A round-trip cost is applied each time the position changes sign
    or magnitude (a "turn"). The cost is the round-trip bps
    (fee + spread + slippage) plus borrow costs for short positions.

    Args:
        gross_returns: per-period gross returns (frictionless).
        positions: per-period positions (-1, 0, +1 or fractional).
            Must be the same length as ``gross_returns``.
        cost_model: the training cost model to apply.
        holding_days: holding period in days (for borrow cost). Only
            applies to short positions (position < 0).

    Returns:
        Per-period net returns (same length as input).

    Raises:
        ValueError: if lengths mismatch or are empty.
    """
    if len(gross_returns) != len(positions):
        raise ValueError(
            f"length mismatch: gross_returns has {len(gross_returns)}, "
            f"positions has {len(positions)}"
        )
    if not gross_returns:
        raise ValueError("gross_returns must be non-empty")

    round_trip_cost = cost_model.round_trip_fraction
    borrow_cost = (
        cost_model.borrow_bps_per_day * holding_days / 10_000.0
        if cost_model.borrow_bps_per_day > 0
        else 0.0
    )

    net_returns: list[float] = []
    prev_pos = 0.0
    for ret, pos in zip(gross_returns, positions, strict=True):
        # A turn is any change in position (including entry from flat).
        # The cost is proportional to the change in position magnitude.
        pos_change = abs(pos - prev_pos)
        if pos_change > 0:
            # Round-trip cost applies to the changed portion.
            # For a full entry/exit (|change| = 2), the full round-trip
            # cost applies. For a partial change, a proportional fraction.
            cost = round_trip_cost * (pos_change / 2.0)
        else:
            cost = 0.0

        # Borrow cost for short positions (per period held).
        if pos < 0 and borrow_cost > 0:
            cost += borrow_cost * abs(pos)

        net_returns.append(float(ret - cost))
        prev_pos = pos

    return net_returns


def compute_cost_aware_metrics(
    gross_returns: list[float],
    positions: list[float],
    cost_model: TrainingCostModel,
    *,
    ann_factor: float = 1.0,
    holding_days: int = 1,
) -> CostAwareMetrics:
    """Compute both gross and net-of-cost training metrics.

    This is the main entry point for making training metrics
    execution-aware. It takes the frictionless per-period returns and
    positions, applies the cost model, and returns a
    :class:`CostAwareMetrics` with both gross and net values.

    Args:
        gross_returns: per-period gross returns (frictionless).
        positions: per-period positions (-1, 0, +1 or fractional).
        cost_model: the training cost model to apply.
        ann_factor: annualization factor (e.g. sqrt(252) for daily,
            sqrt(525600) for 1-minute bars).
        holding_days: holding period in days (for borrow cost).

    Returns:
        A :class:`CostAwareMetrics` with gross and net Sharpe,
        drawdown, win rate, and mean return.
    """
    if not gross_returns:
        raise ValueError("gross_returns must be non-empty")
    if len(gross_returns) != len(positions):
        raise ValueError("length mismatch between gross_returns and positions")

    net_returns = apply_training_costs(
        gross_returns, positions, cost_model, holding_days=holding_days,
    )

    # --- gross metrics -------------------------------------------------
    n = len(gross_returns)
    mean_gross = sum(gross_returns) / n
    var_gross = sum((r - mean_gross) ** 2 for r in gross_returns) / n
    std_gross = math.sqrt(var_gross) if var_gross > 0 else 0.0
    sharpe_gross = (
        (mean_gross / std_gross) * ann_factor if std_gross > 0 else 0.0
    )

    # Cumulative drawdown (gross)
    cum_g = 0.0
    peak_g = 0.0
    max_dd_g = 0.0
    for r in gross_returns:
        cum_g += r
        if cum_g > peak_g:
            peak_g = cum_g
        dd = cum_g - peak_g
        if dd < max_dd_g:
            max_dd_g = dd

    win_rate_gross = sum(1 for r in gross_returns if r > 0) / n

    # --- net metrics ---------------------------------------------------
    mean_net = sum(net_returns) / n
    var_net = sum((r - mean_net) ** 2 for r in net_returns) / n
    std_net = math.sqrt(var_net) if var_net > 0 else 0.0
    sharpe_net = (
        (mean_net / std_net) * ann_factor if std_net > 0 else 0.0
    )

    # Cumulative drawdown (net)
    cum_n = 0.0
    peak_n = 0.0
    max_dd_n = 0.0
    for r in net_returns:
        cum_n += r
        if cum_n > peak_n:
            peak_n = cum_n
        dd = cum_n - peak_n
        if dd < max_dd_n:
            max_dd_n = dd

    win_rate_net = sum(1 for r in net_returns if r > 0) / n

    # --- turnover + total cost -----------------------------------------
    turns = 0
    prev_pos = 0.0
    for pos in positions:
        if abs(pos - prev_pos) > 0:
            turns += 1
        prev_pos = pos
    turnover = turns / n if n > 0 else 0.0
    total_cost_bps = cost_model.round_trip_bps * turns / n if n > 0 else 0.0

    return CostAwareMetrics(
        sharpe_gross=sharpe_gross,
        sharpe_net=sharpe_net,
        max_drawdown_gross=max_dd_g,
        max_drawdown_net=max_dd_n,
        win_rate_gross=win_rate_gross,
        win_rate_net=win_rate_net,
        mean_return_gross=mean_gross,
        mean_return_net=mean_net,
        turnover=turnover,
        total_cost_bps=total_cost_bps,
        cost_model_version=cost_model.version,
    )
