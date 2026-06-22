"""
quant_foundry.retirement — retirement and edge-decay flags (TASK-0703).

Automatically flags models that stop working. Detects:
- **Calibration degradation** (Brier score above threshold).
- **Net edge below baseline** (negative baseline delta).
- **Feature availability degradation** (decay score above threshold).
- **Stale** (days since last settlement above threshold).

Emits retirement recommendations:
- **RETIRE**: severe decay (decay_score >= retire_threshold).
- **RETRAIN**: moderate decay (decay_score >= retrain_threshold).
- **MONITOR**: mild decay (below retrain_threshold but flagged).

Key invariants:
- **A decayed fixture model is flagged.** The flagger detects decay and
  returns a ``RetirementFlag``.
- **Flag includes reason.** The flag carries a list of ``DecayReason`` values.
- **Retirement recommendation cannot delete artifacts.** The flag is a
  recommendation — it does not carry a delete operation. The dashboard
  shows the suggestion; the operator decides.
- **Dashboard shows retire/retrain suggestion.** The flag's ``action``
  field is ``RETIRE``, ``RETRAIN``, or ``MONITOR``.

File-disjoint from my `leaderboard_expanded.py` + `tournament.py`
(read-only imports). Does NOT modify them.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.leaderboard_expanded import ExpandedLeaderboardEntry

# ---------------------------------------------------------------------------
# Decay reason
# ---------------------------------------------------------------------------


class DecayReason(StrEnum):
    """Reason why a model was flagged for retirement/retraining."""

    CALIBRATION_DEGRADATION = "calibration_degradation"
    NET_EDGE_BELOW_BASELINE = "net_edge_below_baseline"
    FEATURE_AVAILABILITY_DEGRADATION = "feature_availability_degradation"
    LATENCY_BUDGET_VIOLATION = "latency_budget_violation"
    DRAWDOWN_CONTRIBUTION = "drawdown_contribution"
    STALE = "stale"


# ---------------------------------------------------------------------------
# Retirement action
# ---------------------------------------------------------------------------


class RetirementAction(StrEnum):
    """The recommended action for a flagged model."""

    RETIRE = "retire"
    RETRAIN = "retrain"
    MONITOR = "monitor"


# ---------------------------------------------------------------------------
# Decay thresholds
# ---------------------------------------------------------------------------


class DecayThresholds(BaseModel):
    """Thresholds that define when a model is flagged for decay.

    Frozen + extra='forbid'. Carries the maximum Brier score, minimum
    baseline delta, maximum decay score, and maximum days since last
    settlement before a model is flagged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_brier_score: float = 0.25
    min_baseline_delta: float = 0.0
    max_decay_score: float = 0.3
    max_days_since_settlement: int = 30
    # Action thresholds (decay_score >= retire -> RETIRE, >= retrain -> RETRAIN).
    retire_decay_threshold: float = 0.5
    retrain_decay_threshold: float = 0.3


# ---------------------------------------------------------------------------
# Retirement flag
# ---------------------------------------------------------------------------


class RetirementFlag(BaseModel):
    """A retirement flag with reason(s) + action.

    Frozen + extra='forbid'. Carries the model_id, list of decay reasons,
    recommended action, and timestamp. ``to_dict`` is JSON serializable for
    audit. The flag is a **recommendation** — it does not delete artifacts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    reasons: list[DecayReason] = []
    action: RetirementAction = RetirementAction.MONITOR
    flagged_at_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "model_id": self.model_id,
            "reasons": [r.value for r in self.reasons],
            "action": self.action.value,
            "flagged_at_ns": self.flagged_at_ns,
        }


# ---------------------------------------------------------------------------
# The flagger
# ===========================================================================


class RetirementFlagger:
    """Flags models for retirement/retraining based on decay indicators.

    Evaluates an ``ExpandedLeaderboardEntry`` against the configured
    thresholds and returns a ``RetirementFlag`` if the model is flagged,
    or ``None`` if the model is healthy.
    """

    def __init__(self, thresholds: DecayThresholds | None = None) -> None:
        self.thresholds = thresholds or DecayThresholds()

    def evaluate(self, entry: ExpandedLeaderboardEntry) -> RetirementFlag | None:
        """Evaluate an entry and return a RetirementFlag if flagged."""
        reasons: list[DecayReason] = []
        decay_score = 0.0

        # Check calibration degradation.
        if (
            entry.calibration_summary is not None
            and entry.calibration_summary.brier_score > self.thresholds.max_brier_score
        ):
            reasons.append(DecayReason.CALIBRATION_DEGRADATION)

        # Check net edge below baseline.
        if (
            entry.baseline_delta is not None
            and entry.baseline_delta.delta < self.thresholds.min_baseline_delta
        ):
            reasons.append(DecayReason.NET_EDGE_BELOW_BASELINE)

        # Check decay indicator.
        if entry.decay_indicator is not None:
            decay_score = entry.decay_indicator.decay_score
            if entry.decay_indicator.is_stale or (
                entry.decay_indicator.days_since_last_settlement
                > self.thresholds.max_days_since_settlement
            ):
                reasons.append(DecayReason.STALE)
            if entry.decay_indicator.decay_score > self.thresholds.max_decay_score:
                reasons.append(DecayReason.FEATURE_AVAILABILITY_DEGRADATION)

        # If no reasons, the model is healthy.
        if not reasons:
            return None

        # Determine the action based on decay score.
        if decay_score >= self.thresholds.retire_decay_threshold:
            action = RetirementAction.RETIRE
        elif decay_score >= self.thresholds.retrain_decay_threshold:
            action = RetirementAction.RETRAIN
        else:
            action = RetirementAction.MONITOR

        return RetirementFlag(
            model_id=entry.model_id,
            reasons=reasons,
            action=action,
            flagged_at_ns=time.time_ns(),
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def flag_model_for_retirement(
    entry: ExpandedLeaderboardEntry,
    thresholds: DecayThresholds | None = None,
) -> RetirementFlag | None:
    """Flag a model for retirement/retraining.

    Convenience entry point for TASK-0703. Creates a ``RetirementFlagger``
    and evaluates the entry.
    """
    flagger = RetirementFlagger(thresholds=thresholds)
    return flagger.evaluate(entry)
