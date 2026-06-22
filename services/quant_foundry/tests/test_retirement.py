"""
Tests for TASK-0703: Add Retirement and Edge-Decay Flags.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `retirement.py` exists.

Acceptance criteria covered:
- A decayed fixture model is flagged.
- Flag includes reason.
- Retirement recommendation cannot delete artifacts.
- Dashboard shows retire/retrain suggestion.

Additional checks from the spec:
- Define decay thresholds.
- Detect: calibration degradation, net edge below baseline, feature
  availability degradation, latency budget violations, drawdown
  contribution warnings.
- Emit retirement recommendations.

File-disjoint from my `leaderboard_expanded.py` + `tournament.py`
(read-only imports). Does NOT modify them.
"""

from __future__ import annotations

from typing import Any

import pytest
from quant_foundry.leaderboard_expanded import (
    BaselineDelta,
    CalibrationSummary,
    DecayIndicator,
    ExpandedLeaderboardEntry,
)
from quant_foundry.retirement import (
    DecayReason,
    DecayThresholds,
    RetirementAction,
    RetirementFlag,
    RetirementFlagger,
    flag_model_for_retirement,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    model_id: str = "m1",
    total_score: float = 0.8,
    brier_score: float = 0.15,
    baseline_delta: float = 0.1,
    decay_score: float = 0.0,
    is_stale: bool = False,
    is_decayed: bool = False,
    days_since_last_settlement: int = 1,
) -> ExpandedLeaderboardEntry:
    """Build a minimal expanded leaderboard entry for testing."""
    return ExpandedLeaderboardEntry(
        model_id=model_id,
        total_score=total_score,
        horizon_slices=[],
        regime_slices=[],
        symbol_cluster_slices=[],
        baseline_delta=BaselineDelta(
            baseline_model_id="baseline",
            delta=baseline_delta,
            baseline_score=total_score - baseline_delta,
        ),
        calibration_summary=CalibrationSummary(
            brier_score=brier_score,
            reliability=0.85,
            n_bins=10,
        ),
        decay_indicator=DecayIndicator(
            decay_score=decay_score,
            is_stale=is_stale,
            is_decayed=is_decayed,
            days_since_last_settlement=days_since_last_settlement,
        ),
    )


# ---------------------------------------------------------------------------
# DecayReason
# ===========================================================================


class TestDecayReason:
    """Decay reasons for flagging models."""

    def test_decay_reasons_are_defined(self) -> None:
        """DecayReason has the expected values."""
        assert DecayReason.CALIBRATION_DEGRADATION is not None
        assert DecayReason.NET_EDGE_BELOW_BASELINE is not None
        assert DecayReason.FEATURE_AVAILABILITY_DEGRADATION is not None
        assert DecayReason.LATENCY_BUDGET_VIOLATION is not None
        assert DecayReason.DRAWDOWN_CONTRIBUTION is not None
        assert DecayReason.STALE is not None


# ---------------------------------------------------------------------------
# DecayThresholds
# ===========================================================================


class TestDecayThresholds:
    """Decay thresholds define when a model is flagged."""

    def test_thresholds_have_required_fields(self) -> None:
        """DecayThresholds has the expected threshold fields."""
        thresholds = DecayThresholds(
            max_brier_score=0.25,
            min_baseline_delta=0.0,
            max_decay_score=0.3,
            max_days_since_settlement=30,
        )
        assert thresholds.max_brier_score == 0.25
        assert thresholds.min_baseline_delta == 0.0
        assert thresholds.max_decay_score == 0.3
        assert thresholds.max_days_since_settlement == 30

    def test_thresholds_have_reasonable_defaults(self) -> None:
        """DecayThresholds has reasonable defaults."""
        thresholds = DecayThresholds()
        assert thresholds.max_brier_score > 0
        assert thresholds.max_decay_score > 0
        assert thresholds.max_days_since_settlement > 0

    def test_thresholds_is_frozen(self) -> None:
        """DecayThresholds is frozen."""
        thresholds = DecayThresholds()
        with pytest.raises((TypeError, ValueError)):
            thresholds.max_brier_score = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RetirementFlag
# ===========================================================================


class TestRetirementFlag:
    """A retirement flag with reason + action."""

    def test_flag_has_required_fields(self) -> None:
        """RetirementFlag has model_id, reasons, action, flagged_at_ns."""
        flag = RetirementFlag(
            model_id="m1",
            reasons=[DecayReason.CALIBRATION_DEGRADATION],
            action=RetirementAction.RETIRE,
            flagged_at_ns=1000,
        )
        assert flag.model_id == "m1"
        assert DecayReason.CALIBRATION_DEGRADATION in flag.reasons
        assert flag.action == RetirementAction.RETIRE
        assert flag.flagged_at_ns == 1000

    def test_flag_includes_reason(self) -> None:
        """Flag includes the decay reason(s)."""
        flag = RetirementFlag(
            model_id="m1",
            reasons=[DecayReason.STALE, DecayReason.CALIBRATION_DEGRADATION],
            action=RetirementAction.RETRAIN,
            flagged_at_ns=1000,
        )
        assert len(flag.reasons) >= 1
        assert DecayReason.STALE in flag.reasons

    def test_flag_is_frozen(self) -> None:
        """RetirementFlag is frozen (immutable for audit)."""
        flag = RetirementFlag(
            model_id="m1",
            reasons=[DecayReason.STALE],
            action=RetirementAction.RETIRE,
            flagged_at_ns=1000,
        )
        with pytest.raises((TypeError, ValueError)):
            flag.action = RetirementAction.RETRAIN  # type: ignore[misc]

    def test_flag_to_dict_is_json_serializable(self) -> None:
        """Flag can be serialized to JSON."""
        import json

        flag = RetirementFlag(
            model_id="m1",
            reasons=[DecayReason.STALE],
            action=RetirementAction.RETIRE,
            flagged_at_ns=1000,
        )
        d = flag.to_dict()
        json.dumps(d)
        assert "model_id" in d
        assert "reasons" in d
        assert "action" in d


# ---------------------------------------------------------------------------
# RetirementAction
# ===========================================================================


class TestRetirementAction:
    """Retirement actions: RETIRE, RETRAIN, MONITOR."""

    def test_actions_are_defined(self) -> None:
        """RetirementAction has the expected values."""
        assert RetirementAction.RETIRE is not None
        assert RetirementAction.RETRAIN is not None
        assert RetirementAction.MONITOR is not None


# ---------------------------------------------------------------------------
# A decayed fixture model is flagged
# ===========================================================================


class TestDecayedModelFlagged:
    """A decayed fixture model is flagged."""

    def test_decayed_model_is_flagged(self) -> None:
        """A decayed model is flagged."""
        entry = _make_entry(is_decayed=True, decay_score=0.5)
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        assert flag.action == RetirementAction.RETIRE

    def test_stale_model_is_flagged(self) -> None:
        """A stale model is flagged."""
        entry = _make_entry(is_stale=True, days_since_last_settlement=60)
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        assert DecayReason.STALE in flag.reasons

    def test_healthy_model_is_not_flagged(self) -> None:
        """A healthy model is not flagged."""
        entry = _make_entry(
            brier_score=0.1, baseline_delta=0.2, decay_score=0.0,
            is_stale=False, is_decayed=False,
        )
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is None

    def test_calibration_degradation_is_flagged(self) -> None:
        """Calibration degradation (high Brier score) is flagged."""
        entry = _make_entry(brier_score=0.4)  # above default threshold
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        assert DecayReason.CALIBRATION_DEGRADATION in flag.reasons

    def test_net_edge_below_baseline_is_flagged(self) -> None:
        """Net edge below baseline (negative delta) is flagged."""
        entry = _make_entry(baseline_delta=-0.1)
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        assert DecayReason.NET_EDGE_BELOW_BASELINE in flag.reasons


# ---------------------------------------------------------------------------
# Flag includes reason
# ===========================================================================


class TestFlagIncludesReason:
    """Flag includes reason."""

    def test_flag_includes_specific_reason(self) -> None:
        """The flag includes the specific decay reason."""
        entry = _make_entry(is_stale=True, days_since_last_settlement=60)
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        assert DecayReason.STALE in flag.reasons

    def test_flag_includes_multiple_reasons(self) -> None:
        """The flag can include multiple decay reasons."""
        entry = _make_entry(
            brier_score=0.4,  # calibration degradation
            baseline_delta=-0.1,  # net edge below baseline
            is_stale=True,  # stale
            days_since_last_settlement=60,
        )
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        assert len(flag.reasons) >= 3
        assert DecayReason.CALIBRATION_DEGRADATION in flag.reasons
        assert DecayReason.NET_EDGE_BELOW_BASELINE in flag.reasons
        assert DecayReason.STALE in flag.reasons


# ---------------------------------------------------------------------------
# Retirement recommendation cannot delete artifacts
# ===========================================================================


class TestNoArtifactDeletion:
    """Retirement recommendation cannot delete artifacts."""

    def test_retire_action_does_not_delete_artifacts(self) -> None:
        """The RETIRE action does not delete artifacts (it's a recommendation)."""
        entry = _make_entry(is_decayed=True, decay_score=0.5)
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        # The flag is a recommendation — it does not carry a delete operation.
        assert not hasattr(flag, "delete_artifacts")
        assert not hasattr(flag, "artifact_deleted")

    def test_flag_to_dict_has_no_delete_keys(self) -> None:
        """The flag dict has no delete-related keys."""
        entry = _make_entry(is_decayed=True, decay_score=0.5)
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        d = flag.to_dict()
        delete_keys = {"delete", "delete_artifacts", "artifact_deleted",
                       "remove", "purge"}
        assert not any(k in d for k in delete_keys)


# ---------------------------------------------------------------------------
# Dashboard shows retire/retrain suggestion
# ===========================================================================


class TestRetireRetrainSuggestion:
    """Dashboard shows retire/retrain suggestion."""

    def test_retire_action_is_a_suggestion(self) -> None:
        """The RETIRE action is a suggestion (not a command)."""
        entry = _make_entry(is_decayed=True, decay_score=0.5)
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        assert flag.action == RetirementAction.RETIRE

    def test_retrain_action_for_moderate_decay(self) -> None:
        """The RETRAIN action is suggested for moderate decay."""
        entry = _make_entry(decay_score=0.35)  # above default threshold
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        # Moderate decay -> RETRAIN, not RETIRE.
        assert flag.action == RetirementAction.RETRAIN

    def test_monitor_action_for_mild_decay(self) -> None:
        """The MONITOR action is suggested for mild decay."""
        entry = _make_entry(decay_score=0.15)  # below default threshold
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        # Mild decay below threshold -> not flagged (None) or MONITOR.
        # If flagged, it should be MONITOR.
        if flag is not None:
            assert flag.action == RetirementAction.MONITOR


# ---------------------------------------------------------------------------
# Convenience function
# ===========================================================================


class TestFlagModelForRetirement:
    """The convenience function flag_model_for_retirement works end-to-end."""

    def test_flag_model_returns_flag(self) -> None:
        """flag_model_for_retirement returns a RetirementFlag."""
        entry = _make_entry(is_decayed=True, decay_score=0.5)
        flag = flag_model_for_retirement(entry)
        assert isinstance(flag, RetirementFlag)
        assert flag.action == RetirementAction.RETIRE

    def test_flag_model_with_custom_thresholds(self) -> None:
        """flag_model_for_retirement accepts custom thresholds."""
        entry = _make_entry(brier_score=0.3)
        thresholds = DecayThresholds(max_brier_score=0.2)
        flag = flag_model_for_retirement(entry, thresholds=thresholds)
        assert flag is not None
        assert DecayReason.CALIBRATION_DEGRADATION in flag.reasons


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInRetirementOutput:
    """Retirement output must not leak secrets."""

    def test_flag_to_dict_has_no_secret_keys(self) -> None:

        entry = _make_entry(is_decayed=True, decay_score=0.5)
        flagger = RetirementFlagger()
        flag = flagger.evaluate(entry)
        assert flag is not None
        d = flag.to_dict()

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
