"""
quant_foundry.drift_sentinel — adversarial drift sentinel (TASK-1004).

Detects when the current market is hostile to the active or shadow model
set. Tracks:
- **Feature distribution drift** — feature values have shifted from the
  training distribution.
- **Calibration drift** — the model's calibration has degraded.
- **Provider freshness drift** — data providers are stale or delayed.
- **Prediction disagreement spikes** — models disagree more than usual.
- **Live edge decay** — the live edge is decaying.

Emits recommendations:
- **NO_ACTION** — no drift detected.
- **LOWER_TRUST** — mild drift; reduce trust in model predictions.
- **SHADOW_ONLY** — moderate drift; restrict to shadow mode.
- **RETRAIN** — severe drift; retrain the model.
- **RETIRE** — critical drift; retire the model.

File-disjoint from my ``retirement.py`` + ``leaderboard_expanded.py``
(read-only imports). Does NOT modify them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Drift indicator
# ---------------------------------------------------------------------------


class DriftIndicator(StrEnum):
    """The types of drift tracked by the sentinel."""

    FEATURE_DISTRIBUTION_DRIFT = "feature_distribution_drift"
    CALIBRATION_DRIFT = "calibration_drift"
    PROVIDER_FRESHNESS_DRIFT = "provider_freshness_drift"
    PREDICTION_DISAGREEMENT_SPIKE = "prediction_disagreement_spike"
    LIVE_EDGE_DECAY = "live_edge_decay"


# ---------------------------------------------------------------------------
# Drift severity
# ---------------------------------------------------------------------------


class DriftSeverity(StrEnum):
    """The severity of a drift report."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Trust recommendation
# ---------------------------------------------------------------------------


class TrustRecommendation(StrEnum):
    """The trust recommendation emitted by the sentinel."""

    NO_ACTION = "no_action"
    LOWER_TRUST = "lower_trust"
    SHADOW_ONLY = "shadow_only"
    RETRAIN = "retrain"
    RETIRE = "retire"


# ---------------------------------------------------------------------------
# Drift metric
# ---------------------------------------------------------------------------


class DriftMetric(BaseModel):
    """A drift metric for a specific drift type.

    Frozen + extra='forbid'. Carries the metric name, value, threshold,
    and whether the metric is drifting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float
    threshold: float
    is_drifting: bool


# ---------------------------------------------------------------------------
# Drift report
# ---------------------------------------------------------------------------


class DriftReport(BaseModel):
    """A drift report with metrics + severity + recommendation.

    Frozen + extra='forbid'. Carries the drift metrics, overall severity,
    trust recommendation, and whether the market is hostile.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: list[DriftMetric] = []
    severity: DriftSeverity = DriftSeverity.LOW
    recommendation: TrustRecommendation = TrustRecommendation.NO_ACTION
    is_hostile: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "metrics": [m.model_dump() for m in self.metrics],
            "severity": self.severity.value,
            "recommendation": self.recommendation.value,
            "is_hostile": self.is_hostile,
        }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class DriftSentinelConfig(BaseModel):
    """Configuration for the drift sentinel.

    Frozen + extra='forbid'. Carries the drift thresholds for each
    indicator type.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_drift_threshold: float = 0.3
    calibration_drift_threshold: float = 0.2
    provider_freshness_drift_threshold: float = 0.4
    prediction_disagreement_threshold: float = 0.5
    live_edge_decay_threshold: float = 0.3


# ---------------------------------------------------------------------------
# The sentinel
# ===========================================================================


class DriftSentinel:
    """Adversarial drift sentinel.

    Evaluates drift metrics and emits a ``DriftReport`` with severity and
    trust recommendation. The market is considered hostile when 2 or more
    drift indicators are active.
    """

    def __init__(self, config: DriftSentinelConfig | None = None) -> None:
        self.config = config or DriftSentinelConfig()

    def evaluate(
        self,
        feature_drift_value: float,
        calibration_drift_value: float,
        provider_freshness_drift_value: float,
        prediction_disagreement_value: float,
        live_edge_decay_value: float,
    ) -> DriftReport:
        """Evaluate drift metrics and emit a report.

        Args:
        - ``feature_drift_value``: feature distribution drift score (0-1).
        - ``calibration_drift_value``: calibration drift score (0-1).
        - ``provider_freshness_drift_value``: provider freshness drift (0-1).
        - ``prediction_disagreement_value``: prediction disagreement (0-1).
        - ``live_edge_decay_value``: live edge decay score (0-1).

        Returns a ``DriftReport`` with metrics, severity, recommendation,
        and hostility flag.
        """
        # Build metrics.
        metrics = [
            DriftMetric(
                name=DriftIndicator.FEATURE_DISTRIBUTION_DRIFT.value,
                value=feature_drift_value,
                threshold=self.config.feature_drift_threshold,
                is_drifting=feature_drift_value > self.config.feature_drift_threshold,
            ),
            DriftMetric(
                name=DriftIndicator.CALIBRATION_DRIFT.value,
                value=calibration_drift_value,
                threshold=self.config.calibration_drift_threshold,
                is_drifting=calibration_drift_value > self.config.calibration_drift_threshold,
            ),
            DriftMetric(
                name=DriftIndicator.PROVIDER_FRESHNESS_DRIFT.value,
                value=provider_freshness_drift_value,
                threshold=self.config.provider_freshness_drift_threshold,
                is_drifting=provider_freshness_drift_value
                > self.config.provider_freshness_drift_threshold,
            ),
            DriftMetric(
                name=DriftIndicator.PREDICTION_DISAGREEMENT_SPIKE.value,
                value=prediction_disagreement_value,
                threshold=self.config.prediction_disagreement_threshold,
                is_drifting=prediction_disagreement_value
                > self.config.prediction_disagreement_threshold,
            ),
            DriftMetric(
                name=DriftIndicator.LIVE_EDGE_DECAY.value,
                value=live_edge_decay_value,
                threshold=self.config.live_edge_decay_threshold,
                is_drifting=live_edge_decay_value > self.config.live_edge_decay_threshold,
            ),
        ]

        # Count drifting indicators.
        drifting_count = sum(1 for m in metrics if m.is_drifting)

        # Compute the average drift value across all indicators.
        avg_drift = (
            feature_drift_value
            + calibration_drift_value
            + provider_freshness_drift_value
            + prediction_disagreement_value
            + live_edge_decay_value
        ) / 5.0

        # Determine severity and recommendation.
        if drifting_count == 0:
            severity = DriftSeverity.LOW
            recommendation = TrustRecommendation.NO_ACTION
        elif drifting_count == 1 and avg_drift < 0.5:
            severity = DriftSeverity.MEDIUM
            recommendation = TrustRecommendation.LOWER_TRUST
        elif drifting_count <= 2 and avg_drift < 0.7:
            severity = DriftSeverity.HIGH
            recommendation = TrustRecommendation.SHADOW_ONLY
        elif drifting_count <= 3 and avg_drift < 0.85:
            severity = DriftSeverity.CRITICAL
            recommendation = TrustRecommendation.RETRAIN
        else:
            severity = DriftSeverity.CRITICAL
            recommendation = TrustRecommendation.RETIRE

        # The market is hostile when 2+ drifts are active.
        is_hostile = drifting_count >= 2

        return DriftReport(
            metrics=metrics,
            severity=severity,
            recommendation=recommendation,
            is_hostile=is_hostile,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def check_drift(
    feature_drift_value: float,
    calibration_drift_value: float,
    provider_freshness_drift_value: float,
    prediction_disagreement_value: float,
    live_edge_decay_value: float,
    config: DriftSentinelConfig | None = None,
) -> DriftReport:
    """Check for drift and emit a report.

    Convenience entry point for TASK-1004. Creates a ``DriftSentinel`` and
    evaluates the drift metrics.
    """
    sentinel = DriftSentinel(config=config)
    return sentinel.evaluate(
        feature_drift_value=feature_drift_value,
        calibration_drift_value=calibration_drift_value,
        provider_freshness_drift_value=provider_freshness_drift_value,
        prediction_disagreement_value=prediction_disagreement_value,
        live_edge_decay_value=live_edge_decay_value,
    )
