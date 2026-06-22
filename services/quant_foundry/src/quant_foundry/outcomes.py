"""
quant_foundry.outcomes — settlement records and the versioned cost model.

This module owns the *settled* side of a prediction: the record that the
tournament (TASK-0404) consumes. It deliberately does NOT touch
``schemas.PredictionOutcome`` (owned by the contract track) — the settlement
ledger needs a richer record (gross/net, pending states, cost-model version,
decision window) that is internal to the evidence loop, not a cross-boundary
contract.

Key invariants (cross-cutting quant rigor §3 reproducibility + §4 cost
governance):
- ``CostModel`` is versioned. The version is stored on every settled record so
  a later cost-model change does not silently rewrite history.
- ``SettlementRecord`` is frozen (Pydantic-style immutability via dataclass) so
  an audit trail cannot be mutated after the fact.
- ``pending_time`` (horizon not elapsed) and ``pending_data`` (market data
  missing) are distinct states — a stuck provider must not be confused with a
  not-yet-due prediction.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum


class SettlementStatus(StrEnum):
    """Lifecycle of a settlement record.

    - ``PENDING_TIME``: the horizon has not fully elapsed (now < t + h). The
      prediction is not yet due; settling it now would use look-ahead.
    - ``PENDING_DATA``: the horizon has elapsed but the market data needed to
      compute a realized return is missing. A stuck provider is NOT the same
      as a not-yet-due prediction, so this is a separate state.
    - ``SETTLED``: the realized return (gross + net) and metrics are computed.
    """

    PENDING_TIME = "pending_time"
    PENDING_DATA = "pending_data"
    SETTLED = "settled"


@dataclass(frozen=True)
class CostModel:
    """Versioned cost / slippage assumptions used to gross->net a return.

    All cost figures are in basis points (1 bps = 0.01%). The ``version`` field
    is recorded on every settled outcome so a later cost-model change does not
    silently rewrite history; both gross and net are stored so the tournament
    can rank on net without losing the gross audit trail.

    Fields:
    - ``fee_bps``: round-trip exchange/broker fee.
    - ``spread_bps``: modeled bid-ask spread (round-trip).
    - ``slippage_bps``: modeled market-impact / slippage (round-trip).
    - ``borrow_bps_per_day``: financing/borrow cost per calendar day held
      (applied to short positions only; longs pay 0).
    """

    version: str
    fee_bps: float
    spread_bps: float
    slippage_bps: float
    borrow_bps_per_day: float


@dataclass(frozen=True)
class SettlementRecord:
    """One settled (or pending) outcome for a single prediction.

    Frozen so the audit trail is immutable. The ledger appends a new record
    (with a new cost-model version) rather than mutating an existing one —
    see ``SettlementLedger.settle`` for the idempotency rule.

    Tournament-relevant fields (consumed by TASK-0404):
    - ``prediction_id``, ``model_id``: join keys.
    - ``realized_return_net``: the net-of-cost edge the tournament ranks on.
    - ``brier``, ``calibration_bucket``: calibration signals.
    - ``abnormal_return``: edge vs benchmark (None where benchmark data missing).
    - ``cost_model_version``: which cost model produced this net figure.

    When ``status`` is ``PENDING_TIME`` or ``PENDING_DATA``, the return/metric
    fields are ``None`` and ``settled_at_ns`` is ``None``.
    """

    prediction_id: str
    model_id: str
    symbol: str
    ts_event: int  # decision time t (ns)
    horizon_ns: int
    status: SettlementStatus
    settled_at_ns: int | None
    realized_return_gross: float | None
    realized_return_net: float | None
    abnormal_return: float | None
    brier: float | None
    calibration_bucket: str | None
    cost_model_version: str
    decision_window_start: int  # t
    decision_window_end: int  # t + horizon_ns

    def to_json(self) -> str:
        """Render to a JSONL line. Status is serialized as its string value."""
        import json

        d = dataclasses.asdict(self)
        d["status"] = self.status.value
        return json.dumps(d, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> SettlementRecord:
        """Parse a JSONL line back into a SettlementRecord."""
        import json

        data = json.loads(line)
        data["status"] = SettlementStatus(data["status"])
        return cls(**data)
