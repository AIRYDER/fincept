"""
Tests for TASK-1004: Adversarial Drift Sentinel.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `drift_sentinel.py` exists.

Acceptance criteria covered (from spec):
- Detect when the current market is hostile to the active or shadow
  model set.
- Track feature distribution drift, calibration drift, provider freshness
  drift, prediction disagreement spikes, live edge decay.
- Emit recommendations: lower trust, shadow-only, retrain, retire.

File-disjoint from my retirement.py + leaderboard_expanded.py (read-only
imports). Does NOT modify them.
"""

from __future__ import annotations

from typing import Any

import pytest
from quant_foundry.drift_sentinel import (
    DriftIndicator,
    DriftMetric,
    DriftReport,
    DriftSentinel,
    DriftSentinelConfig,
    DriftSeverity,
    TrustRecommendation,
    check_drift,
)

# ---------------------------------------------------------------------------
# DriftMetric
# ===========================================================================


class TestDriftMetric:
    """A drift metric for a specific drift type."""

    def test_metric_has_required_fields(self) -> None:
        """DriftMetric has name, value, threshold, is_drifting."""
        metric = DriftMetric(
            name="feature_distribution_drift",
            value=0.6,
            threshold=0.3,
            is_drifting=True,
        )
        assert metric.name == "feature_distribution_drift"
        assert metric.value == 0.6
        assert metric.threshold == 0.3
        assert metric.is_drifting is True

    def test_metric_is_frozen(self) -> None:
        """DriftMetric is frozen."""
        metric = DriftMetric(
            name="test",
            value=0.5,
            threshold=0.3,
            is_drifting=True,
        )
        with pytest.raises((TypeError, ValueError)):
            metric.value = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DriftIndicator
# ===========================================================================


class TestDriftIndicator:
    """The drift indicator types."""

    def test_indicators_are_defined(self) -> None:
        """DriftIndicator has the expected values."""
        assert DriftIndicator.FEATURE_DISTRIBUTION_DRIFT is not None
        assert DriftIndicator.CALIBRATION_DRIFT is not None
        assert DriftIndicator.PROVIDER_FRESHNESS_DRIFT is not None
        assert DriftIndicator.PREDICTION_DISAGREEMENT_SPIKE is not None
        assert DriftIndicator.LIVE_EDGE_DECAY is not None


# ---------------------------------------------------------------------------
# DriftSeverity
# ===========================================================================


class TestDriftSeverity:
    """The drift severity levels."""

    def test_severities_are_defined(self) -> None:
        """DriftSeverity has the expected values."""
        assert DriftSeverity.LOW is not None
        assert DriftSeverity.MEDIUM is not None
        assert DriftSeverity.HIGH is not None
        assert DriftSeverity.CRITICAL is not None


# ---------------------------------------------------------------------------
# TrustRecommendation
# ===========================================================================


class TestTrustRecommendation:
    """The trust recommendations emitted by the sentinel."""

    def test_recommendations_are_defined(self) -> None:
        """TrustRecommendation has the expected values."""
        assert TrustRecommendation.LOWER_TRUST is not None
        assert TrustRecommendation.SHADOW_ONLY is not None
        assert TrustRecommendation.RETRAIN is not None
        assert TrustRecommendation.RETIRE is not None
        assert TrustRecommendation.NO_ACTION is not None


# ---------------------------------------------------------------------------
# DriftReport
# ===========================================================================


class TestDriftReport:
    """A drift report with metrics + severity + recommendation."""

    def test_report_has_required_fields(self) -> None:
        """DriftReport has metrics, severity, recommendation, is_hostile."""
        report = DriftReport(
            metrics=[],
            severity=DriftSeverity.LOW,
            recommendation=TrustRecommendation.NO_ACTION,
            is_hostile=False,
        )
        assert report.metrics == []
        assert report.severity == DriftSeverity.LOW
        assert report.recommendation == TrustRecommendation.NO_ACTION
        assert report.is_hostile is False

    def test_report_is_frozen(self) -> None:
        """DriftReport is frozen."""
        report = DriftReport(
            metrics=[],
            severity=DriftSeverity.LOW,
            recommendation=TrustRecommendation.NO_ACTION,
            is_hostile=False,
        )
        with pytest.raises((TypeError, ValueError)):
            report.is_hostile = True  # type: ignore[misc]

    def test_report_to_dict_is_json_serializable(self) -> None:
        """Report can be serialized to JSON."""
        import json

        report = DriftReport(
            metrics=[
                DriftMetric(
                    name="feature_distribution_drift",
                    value=0.6,
                    threshold=0.3,
                    is_drifting=True,
                ),
            ],
            severity=DriftSeverity.HIGH,
            recommendation=TrustRecommendation.LOWER_TRUST,
            is_hostile=True,
        )
        d = report.to_dict()
        json.dumps(d)
        assert "metrics" in d
        assert "severity" in d
        assert "recommendation" in d
        assert "is_hostile" in d


# ---------------------------------------------------------------------------
# DriftSentinelConfig
# ===========================================================================


class TestDriftSentinelConfig:
    """The sentinel configuration."""

    def test_config_has_required_fields(self) -> None:
        """Config has drift thresholds for each indicator."""
        config = DriftSentinelConfig(
            feature_drift_threshold=0.3,
            calibration_drift_threshold=0.2,
            provider_freshness_drift_threshold=0.4,
            prediction_disagreement_threshold=0.5,
            live_edge_decay_threshold=0.3,
        )
        assert config.feature_drift_threshold == 0.3
        assert config.calibration_drift_threshold == 0.2
        assert config.provider_freshness_drift_threshold == 0.4
        assert config.prediction_disagreement_threshold == 0.5
        assert config.live_edge_decay_threshold == 0.3

    def test_config_defaults_are_reasonable(self) -> None:
        """Config has reasonable defaults."""
        config = DriftSentinelConfig()
        assert config.feature_drift_threshold > 0
        assert config.calibration_drift_threshold > 0
        assert config.provider_freshness_drift_threshold > 0
        assert config.prediction_disagreement_threshold > 0
        assert config.live_edge_decay_threshold > 0

    def test_config_is_frozen(self) -> None:
        """Config is frozen."""
        config = DriftSentinelConfig()
        with pytest.raises((TypeError, ValueError)):
            config.feature_drift_threshold = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DriftSentinel — detect hostile market
# ===========================================================================


class TestDriftSentinelDetect:
    """Detect when the current market is hostile to the model set."""

    def test_sentinel_detects_feature_drift(self) -> None:
        """The sentinel detects feature distribution drift."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.6,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert any(m.name == "feature_distribution_drift" and m.is_drifting for m in report.metrics)

    def test_sentinel_detects_calibration_drift(self) -> None:
        """The sentinel detects calibration drift."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.0,
            calibration_drift_value=0.5,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert any(m.name == "calibration_drift" and m.is_drifting for m in report.metrics)

    def test_sentinel_detects_provider_freshness_drift(self) -> None:
        """The sentinel detects provider freshness drift."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.0,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.6,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert any(m.name == "provider_freshness_drift" and m.is_drifting for m in report.metrics)

    def test_sentinel_detects_prediction_disagreement_spike(self) -> None:
        """The sentinel detects prediction disagreement spikes."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.0,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.7,
            live_edge_decay_value=0.0,
        )
        assert any(
            m.name == "prediction_disagreement_spike" and m.is_drifting for m in report.metrics
        )

    def test_sentinel_detects_live_edge_decay(self) -> None:
        """The sentinel detects live edge decay."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.0,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.5,
        )
        assert any(m.name == "live_edge_decay" and m.is_drifting for m in report.metrics)

    def test_sentinel_no_drift(self) -> None:
        """The sentinel reports no drift when all values are below thresholds."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.0,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert not report.is_hostile
        assert report.recommendation == TrustRecommendation.NO_ACTION
        assert not any(m.is_drifting for m in report.metrics)


# ---------------------------------------------------------------------------
# Emit recommendations
# ===========================================================================


class TestEmitRecommendations:
    """Emit recommendations: lower trust, shadow-only, retrain, retire."""

    def test_low_drift_emits_no_action(self) -> None:
        """Low drift emits NO_ACTION."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.1,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert report.recommendation == TrustRecommendation.NO_ACTION

    def test_moderate_drift_emits_lower_trust(self) -> None:
        """Moderate drift emits LOWER_TRUST."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.4,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert report.recommendation == TrustRecommendation.LOWER_TRUST

    def test_high_drift_emits_shadow_only(self) -> None:
        """High drift emits SHADOW_ONLY."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.6,
            calibration_drift_value=0.5,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert report.recommendation == TrustRecommendation.SHADOW_ONLY

    def test_severe_drift_emits_retrain(self) -> None:
        """Severe drift emits RETRAIN."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.8,
            calibration_drift_value=0.7,
            provider_freshness_drift_value=0.6,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert report.recommendation == TrustRecommendation.RETRAIN

    def test_critical_drift_emits_retire(self) -> None:
        """Critical drift emits RETIRE."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.9,
            calibration_drift_value=0.8,
            provider_freshness_drift_value=0.7,
            prediction_disagreement_value=0.8,
            live_edge_decay_value=0.8,
        )
        assert report.recommendation == TrustRecommendation.RETIRE


# ---------------------------------------------------------------------------
# Hostile market detection
# ===========================================================================


class TestHostileMarket:
    """Detect when the current market is hostile to the model set."""

    def test_hostile_when_multiple_drifts(self) -> None:
        """The market is hostile when multiple drifts are present."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.6,
            calibration_drift_value=0.5,
            provider_freshness_drift_value=0.6,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert report.is_hostile

    def test_not_hostile_when_no_drifts(self) -> None:
        """The market is not hostile when no drifts are present."""
        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.0,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert not report.is_hostile


# ---------------------------------------------------------------------------
# Convenience function
# ===========================================================================


class TestCheckDrift:
    """The convenience function check_drift works."""

    def test_check_drift_returns_report(self) -> None:
        """check_drift returns a DriftReport."""
        report = check_drift(
            feature_drift_value=0.6,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        assert isinstance(report, DriftReport)


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInDriftOutput:
    """Drift sentinel output must not leak secrets."""

    def test_report_to_dict_has_no_secret_keys(self) -> None:

        sentinel = DriftSentinel()
        report = sentinel.evaluate(
            feature_drift_value=0.6,
            calibration_drift_value=0.0,
            provider_freshness_drift_value=0.0,
            prediction_disagreement_value=0.0,
            live_edge_decay_value=0.0,
        )
        d = report.to_dict()

        def _has_secret(d: Any, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password", "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
