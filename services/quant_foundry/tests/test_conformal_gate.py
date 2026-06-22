"""
Tests for TASK-1003: Conformal Prediction Risk Gate.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `conformal_gate.py` exists.

Acceptance criteria covered (from spec):
- Produce uncertainty intervals (q10/q50/q90).
- Abstain when the model cannot make a reliable prediction.
- Feed uncertainty into tournament and paper bridge.

File-disjoint from my shadow_inference.py (read-only imports).
Does NOT modify it.
"""

from __future__ import annotations

from typing import Any

import pytest
from quant_foundry.conformal_gate import (
    AbstainReason,
    ConformalCalibrator,
    ConformalGate,
    ConformalGateConfig,
    ConformalInterval,
    ConformalPrediction,
    calibrate_and_predict,
)

# ---------------------------------------------------------------------------
# ConformalInterval
# ===========================================================================


class TestConformalInterval:
    """Uncertainty intervals (q10/q50/q90)."""

    def test_interval_has_required_fields(self) -> None:
        """ConformalInterval has q10, q50, q90."""
        interval = ConformalInterval(q10=0.3, q50=0.5, q90=0.7)
        assert interval.q10 == 0.3
        assert interval.q50 == 0.5
        assert interval.q90 == 0.7

    def test_interval_is_frozen(self) -> None:
        """ConformalInterval is frozen."""
        interval = ConformalInterval(q10=0.3, q50=0.5, q90=0.7)
        with pytest.raises((TypeError, ValueError)):
            interval.q50 = 0.6  # type: ignore[misc]

    def test_interval_width_is_q90_minus_q10(self) -> None:
        """The interval width is q90 - q10."""
        interval = ConformalInterval(q10=0.3, q50=0.5, q90=0.7)
        assert interval.width == pytest.approx(0.4)

    def test_q10_is_below_q50(self) -> None:
        """q10 is below q50."""
        interval = ConformalInterval(q10=0.3, q50=0.5, q90=0.7)
        assert interval.q10 < interval.q50

    def test_q90_is_above_q50(self) -> None:
        """q90 is above q50."""
        interval = ConformalInterval(q10=0.3, q50=0.5, q90=0.7)
        assert interval.q90 > interval.q50


# ---------------------------------------------------------------------------
# ConformalPrediction
# ===========================================================================


class TestConformalPrediction:
    """A conformal prediction with interval + abstain flag."""

    def test_prediction_has_required_fields(self) -> None:
        """ConformalPrediction has interval, is_abstain, abstain_reason."""
        pred = ConformalPrediction(
            interval=ConformalInterval(q10=0.3, q50=0.5, q90=0.7),
            is_abstain=False,
        )
        assert pred.interval is not None
        assert pred.is_abstain is False
        assert pred.abstain_reason is None

    def test_abstain_prediction_has_no_interval(self) -> None:
        """An abstain prediction has no interval and a reason."""
        pred = ConformalPrediction(
            interval=None,
            is_abstain=True,
            abstain_reason=AbstainReason.INTERVAL_TOO_WIDE,
        )
        assert pred.is_abstain is True
        assert pred.interval is None
        assert pred.abstain_reason == AbstainReason.INTERVAL_TOO_WIDE

    def test_prediction_is_frozen(self) -> None:
        """ConformalPrediction is frozen."""
        pred = ConformalPrediction(
            interval=ConformalInterval(q10=0.3, q50=0.5, q90=0.7),
            is_abstain=False,
        )
        with pytest.raises((TypeError, ValueError)):
            pred.is_abstain = True  # type: ignore[misc]

    def test_prediction_to_dict_is_json_serializable(self) -> None:
        """Prediction can be serialized to JSON."""
        import json

        pred = ConformalPrediction(
            interval=ConformalInterval(q10=0.3, q50=0.5, q90=0.7),
            is_abstain=False,
        )
        d = pred.to_dict()
        json.dumps(d)
        assert "interval" in d
        assert "is_abstain" in d


# ---------------------------------------------------------------------------
# AbstainReason
# ===========================================================================


class TestAbstainReason:
    """Abstain reasons for the conformal gate."""

    def test_abstain_reasons_are_defined(self) -> None:
        """AbstainReason has the expected values."""
        assert AbstainReason.INTERVAL_TOO_WIDE is not None
        assert AbstainReason.INSUFFICIENT_CALIBRATION_DATA is not None
        assert AbstainReason.LOW_CONFIDENCE is not None


# ---------------------------------------------------------------------------
# ConformalGateConfig
# ===========================================================================


class TestConformalGateConfig:
    """The gate configuration."""

    def test_config_has_required_fields(self) -> None:
        """Config has max_interval_width, min_calibration_samples."""
        config = ConformalGateConfig(
            max_interval_width=0.5,
            min_calibration_samples=20,
            min_confidence=0.5,
        )
        assert config.max_interval_width == 0.5
        assert config.min_calibration_samples == 20
        assert config.min_confidence == 0.5

    def test_config_defaults_are_reasonable(self) -> None:
        """Config has reasonable defaults."""
        config = ConformalGateConfig()
        assert config.max_interval_width > 0
        assert config.min_calibration_samples > 0
        assert config.min_confidence > 0

    def test_config_is_frozen(self) -> None:
        """Config is frozen."""
        config = ConformalGateConfig()
        with pytest.raises((TypeError, ValueError)):
            config.max_interval_width = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ConformalCalibrator
# ===========================================================================


class TestConformalCalibrator:
    """The conformal calibrator builds intervals from calibration data."""

    def test_calibrator_fits_on_residuals(self) -> None:
        """The calibrator fits on residuals (predictions - outcomes)."""
        calibrator = ConformalCalibrator()
        # residuals = predictions - outcomes
        residuals = [0.1, -0.1, 0.05, -0.05, 0.2, -0.2, 0.0, 0.15, -0.15, 0.1]
        calibrator.fit(residuals)
        assert calibrator.is_fitted

    def test_calibrator_produces_intervals(self) -> None:
        """The fitted calibrator produces intervals for new predictions."""
        calibrator = ConformalCalibrator()
        residuals = [0.1, -0.1, 0.05, -0.05, 0.2, -0.2, 0.0, 0.15, -0.15, 0.1]
        calibrator.fit(residuals)
        interval = calibrator.predict_interval(point_estimate=0.5)
        assert isinstance(interval, ConformalInterval)
        assert interval.q10 < interval.q50
        assert interval.q90 > interval.q50

    def test_unfitted_calibrator_raises(self) -> None:
        """An unfitted calibrator raises when predicting."""
        calibrator = ConformalCalibrator()
        with pytest.raises((RuntimeError, ValueError)):
            calibrator.predict_interval(point_estimate=0.5)

    def test_calibrator_with_insufficient_data_raises(self) -> None:
        """A calibrator with insufficient data raises when predicting."""
        calibrator = ConformalCalibrator()
        calibrator.fit([0.1])  # only 1 sample
        with pytest.raises((RuntimeError, ValueError)):
            calibrator.predict_interval(point_estimate=0.5)


# ---------------------------------------------------------------------------
# ConformalGate — abstain when the model cannot make a reliable prediction
# ===========================================================================


class TestConformalGateAbstain:
    """Abstain when the model cannot make a reliable prediction."""

    def test_gate_abstains_on_wide_interval(self) -> None:
        """The gate abstains when the interval is too wide."""
        calibrator = ConformalCalibrator()
        # Large residuals -> wide intervals.
        residuals = [0.5, -0.5, 0.4, -0.4, 0.6, -0.6, 0.3, -0.3, 0.5, -0.5]
        calibrator.fit(residuals)
        config = ConformalGateConfig(max_interval_width=0.2)
        gate = ConformalGate(calibrator=calibrator, config=config)
        pred = gate.predict(point_estimate=0.5)
        assert pred.is_abstain
        assert pred.abstain_reason == AbstainReason.INTERVAL_TOO_WIDE

    def test_gate_passes_on_narrow_interval(self) -> None:
        """The gate passes when the interval is narrow enough."""
        calibrator = ConformalCalibrator()
        # Small residuals -> narrow intervals.
        residuals = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.0, 0.02, -0.02, 0.01]
        calibrator.fit(residuals)
        config = ConformalGateConfig(max_interval_width=0.5)
        gate = ConformalGate(calibrator=calibrator, config=config)
        pred = gate.predict(point_estimate=0.5)
        assert not pred.is_abstain
        assert pred.interval is not None

    def test_gate_abstains_on_insufficient_calibration_data(self) -> None:
        """The gate abstains when there's insufficient calibration data."""
        calibrator = ConformalCalibrator()
        # Too few residuals.
        calibrator.fit([0.1, 0.2])
        config = ConformalGateConfig(min_calibration_samples=20)
        gate = ConformalGate(calibrator=calibrator, config=config)
        pred = gate.predict(point_estimate=0.5)
        assert pred.is_abstain
        assert pred.abstain_reason == AbstainReason.INSUFFICIENT_CALIBRATION_DATA

    def test_gate_abstains_on_low_confidence(self) -> None:
        """The gate abstains when the point estimate confidence is too low."""
        calibrator = ConformalCalibrator()
        residuals = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.0, 0.02, -0.02, 0.01]
        calibrator.fit(residuals)
        config = ConformalGateConfig(min_confidence=0.8)
        gate = ConformalGate(calibrator=calibrator, config=config)
        pred = gate.predict(point_estimate=0.5, confidence=0.3)
        assert pred.is_abstain
        assert pred.abstain_reason == AbstainReason.LOW_CONFIDENCE


# ---------------------------------------------------------------------------
# Feed uncertainty into tournament and paper bridge
# ===========================================================================


class TestFeedUncertainty:
    """Feed uncertainty into tournament and paper bridge."""

    def test_prediction_includes_interval_for_tournament(self) -> None:
        """The prediction includes the interval for tournament consumption."""
        calibrator = ConformalCalibrator()
        residuals = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.0, 0.02, -0.02, 0.01]
        calibrator.fit(residuals)
        config = ConformalGateConfig(max_interval_width=0.5)
        gate = ConformalGate(calibrator=calibrator, config=config)
        pred = gate.predict(point_estimate=0.5)
        assert pred.interval is not None
        assert pred.interval.q10 is not None
        assert pred.interval.q50 is not None
        assert pred.interval.q90 is not None

    def test_prediction_to_dict_includes_interval(self) -> None:
        """The prediction dict includes the interval for paper bridge consumption."""
        calibrator = ConformalCalibrator()
        residuals = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.0, 0.02, -0.02, 0.01]
        calibrator.fit(residuals)
        config = ConformalGateConfig(max_interval_width=0.5)
        gate = ConformalGate(calibrator=calibrator, config=config)
        pred = gate.predict(point_estimate=0.5)
        d = pred.to_dict()
        assert "interval" in d
        assert d["interval"]["q10"] is not None
        assert d["interval"]["q50"] is not None
        assert d["interval"]["q90"] is not None


# ---------------------------------------------------------------------------
# Convenience function
# ===========================================================================


class TestCalibrateAndPredict:
    """The convenience function calibrate_and_predict works end-to-end."""

    def test_calibrate_and_predict_returns_prediction(self) -> None:
        """calibrate_and_predict returns a ConformalPrediction."""
        residuals = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.0, 0.02, -0.02, 0.01]
        pred = calibrate_and_predict(
            residuals=residuals,
            point_estimate=0.5,
        )
        assert isinstance(pred, ConformalPrediction)
        assert not pred.is_abstain

    def test_calibrate_and_predict_with_config(self) -> None:
        """calibrate_and_predict accepts a config."""
        residuals = [0.5, -0.5, 0.4, -0.4, 0.6, -0.6, 0.3, -0.3, 0.5, -0.5]
        config = ConformalGateConfig(max_interval_width=0.2)
        pred = calibrate_and_predict(
            residuals=residuals,
            point_estimate=0.5,
            config=config,
        )
        assert pred.is_abstain
        assert pred.abstain_reason == AbstainReason.INTERVAL_TOO_WIDE


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInConformalOutput:
    """Conformal gate output must not leak secrets."""

    def test_prediction_to_dict_has_no_secret_keys(self) -> None:

        calibrator = ConformalCalibrator()
        residuals = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.0, 0.02, -0.02, 0.01]
        calibrator.fit(residuals)
        config = ConformalGateConfig(max_interval_width=0.5)
        gate = ConformalGate(calibrator=calibrator, config=config)
        pred = gate.predict(point_estimate=0.5)
        d = pred.to_dict()

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

        secret_names = {"api_key", "token", "secret", "password",
                        "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
