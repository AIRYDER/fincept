"""
Tests for TASK-0504: Train First Real Baseline Model Family.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `baseline_family.py` exists.

Acceptance criteria covered:
- One real trained artifact imports.
- Dossier includes dataset and feature schema plus the full reproducibility
  set, and re-running reproduces the artifact hash.
- The shuffled-label negative control is recorded and passes (no edge on noise).
- Model cannot influence predictions or orders yet.
- Costs and duration are recorded.

Additional checks from the spec:
- Purged walk-forward validation is used (not plain expanding-window).
- Calibration report is produced (Brier score, reliability).
- Feature importance report is produced (with cross-fold stability).
- Trial count is recorded for the family so the tournament can deflate Sharpe.
- Model is kept at `candidate` or `research-approved` status.
- LightGBM is the first family (gradient-boosted trees).

File-disjoint from Builder 2's `runpod_training.py` / `test_runpod_training.py`.
This test module tests the workflow orchestration layer that connects
training → validation → sentinel → artifact → dossier.
"""

from __future__ import annotations

import tempfile
from typing import Any

import pytest
from quant_foundry.artifacts import ArtifactRecord
from quant_foundry.baseline_family import (
    BaselineCalibrationReport,
    BaselineFeatureImportance,
    BaselineTrainingConfig,
    BaselineTrainingResult,
    PurgedWalkForwardResult,
    train_baseline_family,
)
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.registry import DossierRegistry
from quant_foundry.sentinel import SentinelReceipt

# ---------------------------------------------------------------------------
# Helpers — synthetic tabular dataset for LightGBM
# ===========================================================================


def _make_synthetic_dataset(
    n_samples: int = 200,
    n_features: int = 5,
    seed: int = 42,
) -> tuple[list[list[float]], list[int]]:
    """Generate a synthetic binary classification dataset with real signal.

    Feature 0 has genuine predictive power; the rest are noise.
    Returns (features, labels) where features is a list of rows and labels
    is a list of 0/1 ints.
    """
    import random

    rng = random.Random(seed)
    features: list[list[float]] = []
    labels: list[int] = []
    for _ in range(n_samples):
        row = [rng.gauss(0.0, 1.0) for _ in range(n_features)]
        # Feature 0 has signal: P(y=1) = sigmoid(2 * f0).
        logit = 2.0 * row[0]
        p = 1.0 / (1.0 + pow(2.718281828, -logit))
        label = 1 if rng.random() < p else 0
        features.append(row)
        labels.append(label)
    return features, labels


def _make_config(**overrides: Any) -> BaselineTrainingConfig:
    """Create a BaselineTrainingConfig with all required fields + overrides."""
    defaults: dict[str, Any] = dict(
        dataset_manifest_id="ds-1",
        feature_schema_hash="f" * 64,
        label_schema_hash="l" * 64,
        n_features=5,
        n_samples=200,
        seed=42,
        n_folds=3,
        purge_gap=5,
        embargo_gap=3,
    )
    defaults.update(overrides)
    return BaselineTrainingConfig(**defaults)


# ---------------------------------------------------------------------------
# BaselineTrainingConfig
# ===========================================================================


class TestBaselineTrainingConfig:
    """The config for a baseline training run."""

    def test_config_has_required_fields(self) -> None:
        """Config has model_family, dataset_manifest_id, feature_schema_hash, etc."""
        config = BaselineTrainingConfig(
            model_family="lightgbm",
            dataset_manifest_id="ds-1",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            n_features=5,
            n_samples=200,
            seed=42,
            n_folds=3,
            purge_gap=5,
            embargo_gap=3,
        )
        assert config.model_family == "lightgbm"
        assert config.dataset_manifest_id == "ds-1"
        assert config.n_features == 5
        assert config.n_folds == 3

    def test_config_defaults_to_lightgbm(self) -> None:
        """The default model family is lightgbm."""
        config = BaselineTrainingConfig(
            dataset_manifest_id="ds-1",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            n_features=5,
            n_samples=200,
        )
        assert config.model_family == "lightgbm"

    def test_config_is_frozen(self) -> None:
        """Config is frozen (immutable for audit)."""
        config = BaselineTrainingConfig(
            dataset_manifest_id="ds-1",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            n_features=5,
            n_samples=200,
        )
        with pytest.raises((TypeError, ValueError)):
            config.model_family = "xgboost"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Purged walk-forward validation
# ===========================================================================


class TestPurgedWalkForward:
    """Purged walk-forward validation is used, not plain expanding-window."""

    def test_purged_walk_forward_produces_folds(self) -> None:
        """Walk-forward validation produces folds with purge + embargo gaps."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert isinstance(result, BaselineTrainingResult)
        assert isinstance(result.walk_forward, PurgedWalkForwardResult)
        # Should have folds.
        assert len(result.walk_forward.folds) > 0
        # Each fold should have a purge gap and embargo gap.
        for fold in result.walk_forward.folds:
            assert fold.purge_gap >= config.purge_gap
            assert fold.embargo_gap >= config.embargo_gap

    def test_purged_walk_forward_has_oos_predictions(self) -> None:
        """Walk-forward produces out-of-sample predictions for each fold."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        # Each fold should have OOS predictions.
        for fold in result.walk_forward.folds:
            assert len(fold.oos_predictions) > 0
            assert len(fold.oos_labels) > 0
            assert len(fold.oos_predictions) == len(fold.oos_labels)


# ---------------------------------------------------------------------------
# Negative control (shuffled labels)
# ===========================================================================


class TestNegativeControl:
    """The shuffled-label negative control is recorded and passes."""

    def test_negative_control_receipt_is_recorded(self) -> None:
        """The sentinel receipt for the shuffled-label check is recorded."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert isinstance(result.negative_control_receipt, SentinelReceipt)
        assert result.negative_control_receipt.model_id == result.dossier.model_id

    def test_negative_control_passes_for_real_signal(self) -> None:
        """A model trained on data with real signal should pass the negative control.

        When labels are shuffled, the model should NOT find edge (because the
        signal was in the label-feature relationship, which shuffling destroys).
        """
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        # The negative control should pass (no edge on shuffled labels).
        assert result.negative_control_receipt.passed is True


# ---------------------------------------------------------------------------
# Calibration report
# ===========================================================================


class TestCalibrationReport:
    """A calibration report is produced (Brier score, reliability)."""

    def test_calibration_report_has_brier_score(self) -> None:
        """The calibration report includes a Brier score."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert isinstance(result.calibration, BaselineCalibrationReport)
        assert hasattr(result.calibration, "brier_score")
        assert 0.0 <= result.calibration.brier_score <= 1.0

    def test_calibration_report_has_reliability_bins(self) -> None:
        """The calibration report includes reliability bins (predicted vs observed)."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert hasattr(result.calibration, "reliability_bins")
        assert len(result.calibration.reliability_bins) > 0
        # Each bin has predicted_prob and observed_freq.
        for bin_ in result.calibration.reliability_bins:
            assert "predicted_prob" in bin_
            assert "observed_freq" in bin_
            assert 0.0 <= bin_["predicted_prob"] <= 1.0
            assert 0.0 <= bin_["observed_freq"] <= 1.0


# ---------------------------------------------------------------------------
# Feature importance report
# ===========================================================================


class TestFeatureImportance:
    """A feature importance report is produced (with cross-fold stability)."""

    def test_feature_importance_has_per_feature_scores(self) -> None:
        """Feature importance includes per-feature importance scores."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert isinstance(result.feature_importance, BaselineFeatureImportance)
        assert hasattr(result.feature_importance, "importances")
        assert len(result.feature_importance.importances) == config.n_features

    def test_feature_importance_has_cross_fold_stability(self) -> None:
        """Feature importance includes cross-fold stability (CV of importance)."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert hasattr(result.feature_importance, "cross_fold_cv")
        # Feature 0 (genuine signal) should be more stable than noise features.
        assert "f0" in result.feature_importance.cross_fold_cv

    def test_genuine_feature_has_high_importance(self) -> None:
        """Feature 0 (genuine signal) should have higher importance than noise."""
        features, labels = _make_synthetic_dataset(n_samples=300, seed=42)
        config = _make_config(n_samples=300)
        result = train_baseline_family(config=config, features=features, labels=labels)
        importances = result.feature_importance.importances
        # Feature 0 should be the most important (it has genuine signal).
        assert importances["f0"] > importances["f1"]
        assert importances["f0"] > importances["f4"]


# ---------------------------------------------------------------------------
# Artifact + dossier
# ===========================================================================


class TestArtifactAndDossier:
    """One real trained artifact imports and a dossier is created."""

    def test_result_has_artifact_record(self) -> None:
        """The training result includes an ArtifactRecord."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert isinstance(result.artifact, ArtifactRecord)
        assert result.artifact.model_family == "lightgbm"
        assert result.artifact.size_bytes > 0
        assert len(result.artifact.sha256) == 64

    def test_result_has_dossier(self) -> None:
        """The training result includes a DossierRecord."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert isinstance(result.dossier, DossierRecord)
        assert result.dossier.status == DossierStatus.CANDIDATE
        assert result.dossier.feature_schema_hash == config.feature_schema_hash
        assert result.dossier.label_schema_hash == config.label_schema_hash

    def test_re_running_reproduces_artifact_hash(self) -> None:
        """Re-running with the same config + data reproduces the artifact hash."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result1 = train_baseline_family(config=config, features=features, labels=labels)
        result2 = train_baseline_family(config=config, features=features, labels=labels)
        assert result1.artifact.sha256 == result2.artifact.sha256

    def test_different_seed_produces_different_artifact(self) -> None:
        """A different seed produces a different artifact."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config1 = _make_config(seed=42)
        config2 = _make_config(seed=99)
        result1 = train_baseline_family(config=config1, features=features, labels=labels)
        result2 = train_baseline_family(config=config2, features=features, labels=labels)
        assert result1.artifact.sha256 != result2.artifact.sha256


# ---------------------------------------------------------------------------
# Trial count + costs + duration
# ===========================================================================


class TestTrialCountAndCosts:
    """Trial count, costs, and duration are recorded."""

    def test_trial_count_is_recorded(self) -> None:
        """The trial count for this family is recorded."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert result.trial_count >= 1
        assert result.dossier.trial_count == result.trial_count

    def test_duration_is_recorded(self) -> None:
        """The training duration is recorded."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert hasattr(result, "duration_ns")
        assert result.duration_ns > 0

    def test_cost_estimate_is_recorded(self) -> None:
        """A cost estimate is recorded (economic metrics net of the versioned cost model)."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert hasattr(result, "cost_estimate_usd")
        assert result.cost_estimate_usd >= 0.0


# ---------------------------------------------------------------------------
# Model cannot influence predictions or orders yet
# ===========================================================================


class TestNoTradingAuthority:
    """Model cannot influence predictions or orders yet."""

    def test_model_status_is_candidate(self) -> None:
        """The model is kept at `candidate` status (no trading authority)."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        assert result.dossier.status == DossierStatus.CANDIDATE

    def test_no_order_fields_in_result(self) -> None:
        """The result does not contain any order fields."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        d = result.to_dict()
        # Check no order-related keys.
        order_keys = {"order", "signal", "predict", "trade", "position", "allocation"}
        assert not any(k in d for k in order_keys)


# ---------------------------------------------------------------------------
# Full workflow: register in dossier registry
# ===========================================================================


class TestFullWorkflow:
    """The full workflow: train → validate → sentinel → artifact → dossier → register."""

    def test_register_in_dossier_registry(self) -> None:
        """The trained model can be registered in a DossierRegistry."""
        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        # Register in a temp registry.
        tmpdir = tempfile.mkdtemp()
        reg = DossierRegistry(base_dir=tmpdir)
        reg.register(result.dossier)
        # Verify it's registered.
        retrieved = reg.get(result.dossier.model_id)
        assert retrieved.model_id == result.dossier.model_id
        assert retrieved.artifact_sha256 == result.artifact.sha256

    def test_result_to_dict_is_json_serializable(self) -> None:
        """The training result can be serialized for audit/persistence."""
        import json

        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        d = result.to_dict()
        json.dumps(d)
        assert "artifact" in d
        assert "dossier" in d
        assert "calibration" in d
        assert "feature_importance" in d
        assert "negative_control_receipt" in d
        assert "trial_count" in d


# ---------------------------------------------------------------------------
# Cross-cutting: no secrets in output
# ===========================================================================


class TestNoSecretsInBaselineOutput:
    """Baseline training output must not leak secrets."""

    @pytest.mark.parametrize(
        "secret_field",
        [
            "api_key",
            "token",
            "secret",
            "password",
            "broker_account",
            "credential",
        ],
    )
    def test_config_has_no_secret_fields(self, secret_field: str) -> None:
        """BaselineTrainingConfig must not have any secret-named field."""
        fields = set(BaselineTrainingConfig.model_fields.keys())
        assert secret_field not in fields

    def test_result_to_dict_has_no_secret_keys(self) -> None:

        features, labels = _make_synthetic_dataset(n_samples=200, seed=42)
        config = _make_config()
        result = train_baseline_family(config=config, features=features, labels=labels)
        d = result.to_dict()

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
