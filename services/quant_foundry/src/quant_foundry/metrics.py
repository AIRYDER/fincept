"""
quant_foundry.metrics — settlement math primitives.

Pure functions used by the settlement ledger (settlement.py) to judge a
prediction after its horizon expires. All functions are deterministic and
side-effect free so reruns produce identical results.

Design notes (cross-cutting quant rigor §1, point-in-time correctness):
- ``realized_return`` uses ONLY prices observed at or after the decision time
  ``t``. A price strictly before ``t`` is never used as the entry print —
  vendors revise/backfill, so using a pre-decision print would be look-ahead.
  The entry is the first price with ``ts >= t``; the exit is the first price
  with ``ts >= t + horizon_ns``.
- Cost application is versioned via ``CostModel.version`` (see outcomes.py) so
  a later cost-model change does not silently rewrite history.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quant_foundry.outcomes import CostModel


@dataclass(frozen=True)
class PriceTick:
    """A price observed at a known wall-clock time (nanoseconds since epoch).

    Kept here as a shared primitive so tests and the ledger agree on shape
    without coupling to a vendor schema. ``ts`` is the observation (print)
    time, NOT the event time — vendors revise prints, which is exactly why
    settlement must use the first print at-or-after the decision time.
    """

    ts: int
    price: float


def _first_price_at_or_after(prices: Sequence[PriceTick], threshold_ts: int) -> float | None:
    """Return the price of the earliest tick with ts >= threshold_ts, or None.

    Iterates in a single pass; ties broken by file order (first observed wins,
    which models "first print available at the decision time").
    """
    best_ts: int | None = None
    best_price: float | None = None
    for tick in prices:
        if tick.ts < threshold_ts:
            continue
        if best_ts is None or tick.ts < best_ts:
            best_ts = tick.ts
            best_price = tick.price
    return best_price


def realized_return(
    *,
    prices: Sequence[PriceTick],
    decision_ts: int,
    horizon_ns: int,
    direction: float,
) -> float | None:
    """Realized return on the post-decision window (t, t+h].

    Entry = first price with ts >= decision_ts (the decision-time print).
    Exit  = first price with ts >= decision_ts + horizon_ns (the horizon print).

    Returns None if either print is missing (caller treats as pending_data).

    For ``direction > 0`` (long):  ret = (exit - entry) / entry.
    For ``direction < 0`` (short): ret = (entry - exit) / entry.
    For ``direction == 0`` (flat): ret = 0.0 (no position, no return).
    """
    entry = _first_price_at_or_after(prices, decision_ts)
    if entry is None:
        return None
    exit_price = _first_price_at_or_after(prices, decision_ts + horizon_ns)
    if exit_price is None:
        return None
    if entry == 0:
        return None
    if direction > 0:
        return (exit_price - entry) / entry
    if direction < 0:
        return (entry - exit_price) / entry
    return 0.0


def brier_score(*, p_up: float, actual_up: bool) -> float:
    """Brier score for a binary up/down prediction.

    Lower is better. 0 = perfect, 1 = perfectly wrong.
    ``p_up`` is the predicted probability of an up move; ``actual_up`` is the
    realized outcome (True if the realized return > 0, else False).
    """
    actual = 1.0 if actual_up else 0.0
    return float((p_up - actual) ** 2)


_CALIBRATION_EDGES = (0.2, 0.4, 0.6, 0.8)


def calibration_bucket(confidence: float) -> str:
    """Bucket a confidence value into a named range for reliability curves.

    Buckets: 0.0-0.2, 0.2-0.4, 0.4-0.6, 0.6-0.8, 0.8-1.0.
    A value on a boundary lands in the higher bucket's lower edge label
    (e.g. 0.2 -> "0.0-0.2", 0.2000001 -> "0.2-0.4").
    """
    lo = 0.0
    for edge in _CALIBRATION_EDGES:
        if confidence <= edge:
            return f"{lo:.1f}-{edge:.1f}"
        lo = edge
    return f"{_CALIBRATION_EDGES[-1]:.1f}-1.0"


def abnormal_return(*, realized: float, benchmark: float | None) -> float | None:
    """Abnormal return = realized - benchmark over the same window.

    Returns None when the benchmark is missing (a stuck provider is not the
    same as zero abnormal return).
    """
    if benchmark is None:
        return None
    return float(realized - benchmark)


def apply_costs(
    *,
    gross_return: float,
    cost_model: CostModel,
    direction: float,
    holding_days: int,
) -> float:
    """Convert a gross realized return to a net return by subtracting modeled costs.

    Round-trip cost = fee_bps + spread_bps + slippage_bps (in bps, -> fraction
    by /1e4). Short positions additionally pay borrow_bps_per_day * holding_days.

    Costs apply symmetrically to winning and losing trades (a loser gets worse
    net, never better). This is the settlement-side guard that lets the
    tournament rank on net edge, not gross.
    """
    round_trip_bps = cost_model.fee_bps + cost_model.spread_bps + cost_model.slippage_bps
    borrow_bps = 0.0
    if direction < 0 and holding_days > 0:
        borrow_bps = cost_model.borrow_bps_per_day * float(holding_days)
    total_cost_fraction = (round_trip_bps + borrow_bps) / 10_000.0
    return float(gross_return - total_cost_fraction)
