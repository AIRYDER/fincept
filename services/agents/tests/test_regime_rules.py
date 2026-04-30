"""Unit tests for agents.regime_agent.rules.classify."""

from __future__ import annotations

import pytest

from agents.regime_agent.rules import (
    REGIME_DIRECTION,
    VIX_HIGH_VOL,
    VIX_RISK_OFF,
    VIX_RISK_ON,
    classify,
)


class TestClassify:
    def test_vix_panic_yields_risk_off(self) -> None:
        view = classify(vix=VIX_RISK_OFF + 5, yield_spread=0.5)
        assert view.regime == "risk_off"
        assert view.confidence > 0.4

    def test_inverted_curve_yields_risk_off_even_with_calm_vix(self) -> None:
        view = classify(vix=14.0, yield_spread=-0.20)
        assert view.regime == "risk_off"

    def test_high_vol_band(self) -> None:
        view = classify(vix=VIX_HIGH_VOL + 2, yield_spread=0.4)
        assert view.regime == "high_vol"
        # High vol should have moderate but not panic confidence.
        assert 0.3 <= view.confidence <= 0.7

    def test_risk_on_requires_low_vix_and_healthy_curve(self) -> None:
        view = classify(vix=VIX_RISK_ON - 2, yield_spread=0.6)
        assert view.regime == "risk_on"

    def test_low_vix_but_flat_curve_is_neutral(self) -> None:
        view = classify(vix=VIX_RISK_ON - 1, yield_spread=0.10)
        assert view.regime == "neutral"

    def test_all_inputs_missing_yields_zero_confidence_neutral(self) -> None:
        view = classify(vix=None, yield_spread=None)
        assert view.regime == "neutral"
        assert view.confidence == 0.0

    def test_vix_only_missing_curve_still_classifies(self) -> None:
        view = classify(vix=35.0, yield_spread=None)
        assert view.regime == "risk_off"

    def test_curve_only_missing_vix_inversion_wins(self) -> None:
        view = classify(vix=None, yield_spread=-0.5)
        assert view.regime == "risk_off"

    def test_vix_higher_overshoots_more_confidence(self) -> None:
        moderate = classify(vix=VIX_RISK_OFF + 1, yield_spread=0.5)
        extreme = classify(vix=VIX_RISK_OFF + 30, yield_spread=0.5)
        assert extreme.confidence >= moderate.confidence

    def test_all_regime_labels_have_a_direction(self) -> None:
        """Every label classify() can return must be in REGIME_DIRECTION
        so the orchestrator never gets a 0.0 fallback for a known regime."""
        for label in {"risk_on", "risk_off", "high_vol", "neutral"}:
            assert label in REGIME_DIRECTION

    def test_risk_on_direction_is_positive(self) -> None:
        assert REGIME_DIRECTION["risk_on"] > 0

    def test_risk_off_direction_is_negative(self) -> None:
        assert REGIME_DIRECTION["risk_off"] < 0

    def test_high_vol_direction_is_mildly_negative(self) -> None:
        assert REGIME_DIRECTION["high_vol"] < 0

    @pytest.mark.parametrize(
        "vix,spread,expected",
        [
            (45.0, 0.5, "risk_off"),
            (28.0, 0.4, "high_vol"),
            (12.0, 0.6, "risk_on"),
            (18.0, 0.20, "neutral"),
            (14.0, -0.10, "risk_off"),
        ],
    )
    def test_calibration_table(self, vix: float, spread: float, expected: str) -> None:
        """Lock down the classifier output for representative inputs.

        These five points are the calibration table - if you change a
        threshold in rules.py, expect this test to fail and update it
        deliberately."""
        assert classify(vix=vix, yield_spread=spread).regime == expected
