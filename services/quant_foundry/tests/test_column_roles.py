"""Tests for Phase 8 / T-8.1 — Column Roles, Groups, Weights, Horizons.

Covers:
- ``ColumnRoles`` construction + fail-closed validators (frozen, extra=forbid,
  non-empty features/labels, no feature/excluded overlap, no label/feature
  overlap, no duplicates).
- ``validate_column_roles`` against available columns.
- ``ModelTaskSpec`` construction + fail-closed validators (ranking requires
  group, calibration policy allowlist, task type allowlist).
- ``validate_task_spec`` against ``ColumnRoles``.
- ``RealLightGBMTrainer`` integration: explicit features, excluded-column
  leakage prevention, ranking-without-group failure, missing-label failure,
  backward-compat deprecation warning when ``column_roles`` is None.
"""

from __future__ import annotations

import time
import warnings
from pathlib import Path

import pytest

# --- ColumnRoles -----------------------------------------------------------


def _basic_roles(**overrides):
    """Build a minimal valid ColumnRoles with optional overrides."""
    from quant_foundry.dataset_manifest import ColumnRoles

    base = dict(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("label",),
    )
    base.update(overrides)
    return ColumnRoles(**base)


# ---------------------------------------------------------------------------
# ColumnRoles construction
# ---------------------------------------------------------------------------


def test_column_roles_frozen():
    """ColumnRoles must be frozen (immutable)."""
    roles = _basic_roles()
    with pytest.raises(Exception):
        roles.feature_columns = ("f1",)  # type: ignore[misc]


def test_column_roles_extra_forbid():
    """ColumnRoles must reject unknown fields (extra=forbid)."""
    from quant_foundry.dataset_manifest import ColumnRoles

    with pytest.raises(Exception):
        ColumnRoles(
            feature_columns=("f1",),
            label_columns=("label",),
            unknown_field="bad",  # type: ignore[call-arg]
        )


def test_column_roles_basic_construction():
    roles = _basic_roles()
    assert roles.feature_columns == ("f1", "f2", "f3")
    assert roles.label_columns == ("label",)
    assert roles.excluded_columns == ()
    assert roles.timestamp_column is None
    assert roles.primary_label == "label"


def test_column_roles_with_all_optional_fields():
    from quant_foundry.dataset_manifest import ColumnRoles

    roles = ColumnRoles(
        feature_columns=("f1", "f2"),
        label_columns=("label",),
        timestamp_column="ts",
        symbol_column="sym",
        horizon_column="horizon",
        weight_column="weight",
        group_column="group_id",
        sector_column="sector",
        excluded_columns=("audit_col", "leakage_col"),
    )
    assert roles.timestamp_column == "ts"
    assert roles.symbol_column == "sym"
    assert roles.horizon_column == "horizon"
    assert roles.weight_column == "weight"
    assert roles.group_column == "group_id"
    assert roles.sector_column == "sector"
    assert roles.excluded_columns == ("audit_col", "leakage_col")


def test_column_roles_empty_features_rejected():
    from quant_foundry.dataset_manifest import ColumnRoles

    with pytest.raises(Exception, match="feature_columns must be non-empty"):
        ColumnRoles(feature_columns=(), label_columns=("label",))


def test_column_roles_empty_labels_rejected():
    from quant_foundry.dataset_manifest import ColumnRoles

    with pytest.raises(Exception, match="label_columns must be non-empty"):
        ColumnRoles(feature_columns=("f1",), label_columns=())


def test_column_roles_feature_excluded_overlap_rejected():
    """A column declared both as feature and excluded is leakage — reject."""
    from quant_foundry.dataset_manifest import ColumnRoles

    with pytest.raises(Exception, match="must not overlap excluded_columns"):
        ColumnRoles(
            feature_columns=("f1", "leakage_col"),
            label_columns=("label",),
            excluded_columns=("leakage_col",),
        )


def test_column_roles_label_feature_overlap_rejected():
    """A label column cannot also be a feature column."""
    from quant_foundry.dataset_manifest import ColumnRoles

    with pytest.raises(Exception, match="must not overlap feature_columns"):
        ColumnRoles(
            feature_columns=("f1", "label"),
            label_columns=("label",),
        )


def test_column_roles_duplicate_features_rejected():
    from quant_foundry.dataset_manifest import ColumnRoles

    with pytest.raises(Exception, match="duplicates"):
        ColumnRoles(
            feature_columns=("f1", "f1", "f2"),
            label_columns=("label",),
        )


def test_column_roles_duplicate_labels_rejected():
    from quant_foundry.dataset_manifest import ColumnRoles

    with pytest.raises(Exception, match="duplicates"):
        ColumnRoles(
            feature_columns=("f1",),
            label_columns=("label", "label"),
        )


def test_column_roles_is_excluded():
    roles = _basic_roles(excluded_columns=("audit", "leak"))
    assert roles.is_excluded("audit") is True
    assert roles.is_excluded("leak") is True
    assert roles.is_excluded("f1") is False


def test_column_roles_all_declared_columns():
    from quant_foundry.dataset_manifest import ColumnRoles

    roles = ColumnRoles(
        feature_columns=("f1", "f2"),
        label_columns=("label",),
        timestamp_column="ts",
        weight_column="w",
        group_column="g",
        excluded_columns=("audit",),
    )
    cols = roles.all_declared_columns
    assert "f1" in cols
    assert "f2" in cols
    assert "label" in cols
    assert "ts" in cols
    assert "w" in cols
    assert "g" in cols
    assert "audit" in cols


# ---------------------------------------------------------------------------
# validate_column_roles
# ---------------------------------------------------------------------------


def test_validate_column_roles_pass():
    roles = _basic_roles(timestamp_column="ts")
    available = {"f1", "f2", "f3", "label", "ts"}
    from quant_foundry.dataset_manifest import validate_column_roles

    result = validate_column_roles(roles, available)
    assert result.passed is True
    assert result.errors == ()


def test_validate_column_roles_missing_feature():
    roles = _basic_roles()
    from quant_foundry.dataset_manifest import validate_column_roles

    result = validate_column_roles(roles, {"f1", "f2", "label"})
    assert result.passed is False
    assert any("f3" in e for e in result.errors)


def test_validate_column_roles_missing_label():
    roles = _basic_roles()
    from quant_foundry.dataset_manifest import validate_column_roles

    result = validate_column_roles(roles, {"f1", "f2", "f3"})
    assert result.passed is False
    assert any("label" in e for e in result.errors)


def test_validate_column_roles_missing_optional_column():
    from quant_foundry.dataset_manifest import ColumnRoles, validate_column_roles

    roles = ColumnRoles(
        feature_columns=("f1",),
        label_columns=("label",),
        weight_column="weight",
    )
    result = validate_column_roles(roles, {"f1", "label"})
    assert result.passed is False
    assert any("weight_column" in e for e in result.errors)


def test_validate_column_roles_excluded_not_present_warns():
    """An excluded column absent from the dataset is a warning, not error."""
    roles = _basic_roles(excluded_columns=("stale_audit",))
    from quant_foundry.dataset_manifest import validate_column_roles

    result = validate_column_roles(roles, {"f1", "f2", "f3", "label"})
    assert result.passed is True
    assert any("stale_audit" in w for w in result.warnings)


def test_validate_column_roles_raise_if_failed():
    roles = _basic_roles()
    from quant_foundry.dataset_manifest import validate_column_roles

    result = validate_column_roles(roles, {"f1", "label"})
    with pytest.raises(ValueError, match="validation failed"):
        result.raise_if_failed()


# ---------------------------------------------------------------------------
# ColumnRolesValidationResult
# ---------------------------------------------------------------------------


def test_validation_result_frozen():
    from quant_foundry.dataset_manifest import ColumnRolesValidationResult

    result = ColumnRolesValidationResult(passed=True)
    with pytest.raises(Exception):
        result.passed = False  # type: ignore[misc]


def test_validation_result_extra_forbid():
    from quant_foundry.dataset_manifest import ColumnRolesValidationResult

    with pytest.raises(Exception):
        ColumnRolesValidationResult(passed=True, extra="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ModelTaskSpec construction
# ---------------------------------------------------------------------------


def _basic_spec(**overrides):
    from quant_foundry.training_manifest import ModelTaskSpec

    base = dict(task_type="binary", label_column="label")
    base.update(overrides)
    return ModelTaskSpec(**base)


def test_model_task_spec_frozen():
    spec = _basic_spec()
    with pytest.raises(Exception):
        spec.task_type = "regression"  # type: ignore[misc]


def test_model_task_spec_extra_forbid():
    from quant_foundry.training_manifest import ModelTaskSpec

    with pytest.raises(Exception):
        ModelTaskSpec(
            task_type="binary",
            label_column="label",
            unknown="bad",  # type: ignore[call-arg]
        )


def test_model_task_spec_basic():
    spec = _basic_spec()
    assert spec.task_type == "binary"
    assert spec.label_column == "label"
    assert spec.horizon is None
    assert spec.weight_column is None
    assert spec.group_column is None
    assert spec.calibration_policy == "none"


def test_model_task_spec_all_task_types():
    from quant_foundry.training_manifest import ModelTaskSpec

    for tt in ("binary", "regression", "multiclass"):
        spec = ModelTaskSpec(task_type=tt, label_column="label")
        assert spec.task_type == tt


def test_model_task_spec_ranking_requires_group():
    """Ranking without group_column must fail (fail-closed)."""
    from quant_foundry.training_manifest import ModelTaskSpec

    with pytest.raises(Exception, match="ranking.*group_column"):
        ModelTaskSpec(task_type="ranking", label_column="label")


def test_model_task_spec_ranking_with_group_ok():
    from quant_foundry.training_manifest import ModelTaskSpec

    spec = ModelTaskSpec(
        task_type="ranking",
        label_column="label",
        group_column="group_id",
    )
    assert spec.group_column == "group_id"


def test_model_task_spec_invalid_task_type():
    from quant_foundry.training_manifest import ModelTaskSpec

    with pytest.raises(Exception, match="task_type"):
        ModelTaskSpec(task_type="invalid", label_column="label")


def test_model_task_spec_empty_label_rejected():
    from quant_foundry.training_manifest import ModelTaskSpec

    with pytest.raises(Exception, match="label_column"):
        ModelTaskSpec(task_type="binary", label_column="")


def test_model_task_spec_invalid_calibration_policy():
    from quant_foundry.training_manifest import ModelTaskSpec

    with pytest.raises(Exception, match="calibration_policy"):
        ModelTaskSpec(
            task_type="binary",
            label_column="label",
            calibration_policy="bad",
        )


def test_model_task_spec_valid_calibration_policies():
    from quant_foundry.training_manifest import ModelTaskSpec

    for policy in ("none", "platt", "isotonic"):
        spec = ModelTaskSpec(
            task_type="binary",
            label_column="label",
            calibration_policy=policy,
        )
        assert spec.calibration_policy == policy


def test_model_task_spec_horizon_must_be_positive():
    from quant_foundry.training_manifest import ModelTaskSpec

    with pytest.raises(Exception, match="horizon"):
        ModelTaskSpec(task_type="binary", label_column="label", horizon=0)

    with pytest.raises(Exception, match="horizon"):
        ModelTaskSpec(task_type="binary", label_column="label", horizon=-1)


def test_model_task_spec_horizon_none_ok():
    spec = _basic_spec()
    assert spec.horizon is None


def test_model_task_spec_horizon_positive_ok():
    from quant_foundry.training_manifest import ModelTaskSpec

    spec = ModelTaskSpec(task_type="binary", label_column="label", horizon=15)
    assert spec.horizon == 15


# ---------------------------------------------------------------------------
# validate_task_spec
# ---------------------------------------------------------------------------


def test_validate_task_spec_pass():
    from quant_foundry.training_manifest import validate_task_spec

    roles = _basic_roles()
    spec = _basic_spec()
    result = validate_task_spec(spec, roles)
    assert result.passed is True
    assert result.errors == ()


def test_validate_task_spec_label_not_in_roles():
    """Label column not declared in column_roles.label_columns fails."""
    from quant_foundry.training_manifest import validate_task_spec

    roles = _basic_roles()
    spec = _basic_spec(label_column="wrong_label")
    result = validate_task_spec(spec, roles)
    assert result.passed is False
    assert any("wrong_label" in e for e in result.errors)


def test_validate_task_spec_weight_column_not_in_roles():
    from quant_foundry.training_manifest import validate_task_spec

    roles = _basic_roles()
    spec = _basic_spec(weight_column="missing_weight")
    result = validate_task_spec(spec, roles)
    assert result.passed is False
    assert any("weight_column" in e for e in result.errors)


def test_validate_task_spec_group_column_not_in_roles():
    from quant_foundry.training_manifest import validate_task_spec

    roles = _basic_roles()
    spec = _basic_spec(
        task_type="ranking",
        label_column="label",
        group_column="missing_group",
    )
    result = validate_task_spec(spec, roles)
    assert result.passed is False
    assert any("group_column" in e for e in result.errors)


def test_validate_task_spec_ranking_without_group_in_roles():
    """Ranking task where column_roles has no group_column fails."""
    from quant_foundry.training_manifest import validate_task_spec

    roles = _basic_roles()
    # ModelTaskSpec requires group_column for ranking, so we set it on
    # the spec but NOT on the roles.
    spec = _basic_spec(
        task_type="ranking",
        label_column="label",
        group_column="group_id",
    )
    result = validate_task_spec(spec, roles)
    assert result.passed is False
    assert any("group_column" in e for e in result.errors)


def test_validate_task_spec_ranking_with_group_in_roles_pass():
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.training_manifest import validate_task_spec

    roles = ColumnRoles(
        feature_columns=("f1", "f2"),
        label_columns=("label",),
        group_column="group_id",
    )
    spec = _basic_spec(
        task_type="ranking",
        label_column="label",
        group_column="group_id",
    )
    result = validate_task_spec(spec, roles)
    assert result.passed is True


# ---------------------------------------------------------------------------
# Trainer integration (requires lightgbm)
# ---------------------------------------------------------------------------

_LIGHTGBM = pytest.importorskip("lightgbm")
_NUMPY = pytest.importorskip("numpy")


def _make_test_dataset_with_extra_cols(
    tmp_path: Path,
    n: int = 300,
    seed: int = 42,
) -> Path:
    """Create a CSV with timestamp, features, a leakage col, weight, label."""
    import numpy as np

    rng = np.random.RandomState(seed)
    timestamps = np.arange(n, dtype=np.int64)
    f1 = rng.randn(n)
    f2 = rng.randn(n)
    f3 = rng.randn(n)
    # A leakage column that perfectly predicts the label — if used as a
    # feature, the model would "cheat".
    logit = 0.8 * f1 + 0.5 * f2 - 0.6 * f3 + 0.05 * rng.randn(n)
    label = (logit > 0).astype(float)
    leakage = label.copy()  # perfect leakage
    weight = rng.rand(n) + 0.1
    data = np.column_stack([timestamps, f1, f2, f3, leakage, weight, label])
    path = tmp_path / "test_data_roles.csv"
    np.savetxt(
        str(path),
        data,
        delimiter=",",
        header="timestamp,f1,f2,f3,leakage_col,weight,label",
        comments="",
    )
    return path


def _make_training_request(job_id, dataset_ref, seed=42):
    from quant_foundry.schemas import RunPodTrainingRequest

    return RunPodTrainingRequest(
        job_id=job_id,
        dataset_manifest_ref=dataset_ref,
        model_family="gbm",
        search_space={"n_estimators": [50]},
        random_seed=seed,
        hardware_class="cpu",
        extra_constraints={},
    )


def test_trainer_with_explicit_column_roles_uses_only_declared_features(tmp_path):
    """The trainer must use ONLY declared feature_columns, not infer."""
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    roles = ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("label",),
        timestamp_column="timestamp",
        weight_column="weight",
        excluded_columns=("leakage_col",),
    )
    trainer = RealLightGBMTrainer(column_roles=roles, n_folds=3)
    req = _make_training_request("qf:train:roles:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000  # 60s
    artifact, dossier = trainer.train(req, deadline_ns=deadline)

    # The trainer should have used 3 features (f1, f2, f3), NOT 5 (which
    # would include leakage_col and weight).
    assert dossier.metadata["n_features"] == "3"
    assert artifact.artifact_id.startswith("artifact:")


def test_trainer_excluded_column_never_used_as_feature(tmp_path):
    """A leakage column declared excluded must never appear in features."""
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    roles = ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("label",),
        timestamp_column="timestamp",
        excluded_columns=("leakage_col",),
    )
    trainer = RealLightGBMTrainer(column_roles=roles, n_folds=3)
    req = _make_training_request("qf:train:excl:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    artifact, dossier = trainer.train(req, deadline_ns=deadline)
    # 3 features, not 4 (leakage_col excluded).
    assert dossier.metadata["n_features"] == "3"


def test_trainer_fail_closed_if_excluded_in_features(tmp_path):
    """If an excluded column appears in feature_columns, fail closed.

    ColumnRoles construction already rejects this, but the trainer
    re-checks at train time (defence in depth).
    """
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import TrainingFailure

    # Bypass construction validation by using object.__setattr__ to
    # simulate a mutated/stale roles object.
    roles = ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("label",),
        excluded_columns=("leakage_col",),
    )
    # Mutate to create the overlap (simulating stale state).
    object.__setattr__(
        roles,
        "feature_columns",
        ("f1", "f2", "f3", "leakage_col"),
    )
    trainer = RealLightGBMTrainer(column_roles=roles)
    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    req = _make_training_request("qf:train:leak:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    with pytest.raises(TrainingFailure, match="leakage"):
        trainer.train(req, deadline_ns=deadline)


def test_trainer_ranking_without_group_fails(tmp_path):
    """Ranking task without group_column must fail (fail-closed)."""
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import TrainingFailure
    from quant_foundry.training_manifest import ModelTaskSpec

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    roles = ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("label",),
        timestamp_column="timestamp",
        # group_column NOT set.
    )
    # ModelTaskSpec requires group for ranking, so we can't construct one
    # without group. Instead, bypass and simulate stale state.
    spec = ModelTaskSpec(
        task_type="binary",  # construct as binary first
        label_column="label",
    )
    object.__setattr__(spec, "task_type", "ranking")  # mutate to ranking
    trainer = RealLightGBMTrainer(column_roles=roles, task_spec=spec)
    req = _make_training_request("qf:train:rank:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    with pytest.raises(TrainingFailure, match="ranking.*group"):
        trainer.train(req, deadline_ns=deadline)


def test_trainer_missing_label_fails(tmp_path):
    """Missing label column in dataset must fail (fail-closed)."""
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import TrainingFailure

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    roles = ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("nonexistent_label",),
        timestamp_column="timestamp",
    )
    trainer = RealLightGBMTrainer(column_roles=roles)
    req = _make_training_request("qf:train:nolabel:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    with pytest.raises(TrainingFailure, match="label"):
        trainer.train(req, deadline_ns=deadline)


def test_trainer_backward_compat_without_column_roles(tmp_path):
    """Without column_roles, trainer falls back to legacy behavior.

    A DeprecationWarning is emitted but training still succeeds.
    """
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    trainer = RealLightGBMTrainer(n_folds=3)  # no column_roles
    req = _make_training_request("qf:train:legacy:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        artifact, dossier = trainer.train(req, deadline_ns=deadline)

    # Legacy mode infers features by dropping label + timestamp.
    # Dataset has: timestamp, f1, f2, f3, leakage_col, weight, label
    # Legacy drops timestamp + label => 5 features.
    assert dossier.metadata["n_features"] == "5"


def test_trainer_backward_compat_emits_deprecation_warning(tmp_path):
    """Without column_roles, a DeprecationWarning is emitted."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    trainer = RealLightGBMTrainer(n_folds=3)
    req = _make_training_request("qf:train:warn:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    with pytest.warns(DeprecationWarning, match="column_roles"):
        trainer.train(req, deadline_ns=deadline)


def test_trainer_with_task_spec_validates_label(tmp_path):
    """Trainer with task_spec validates label against column_roles."""
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import TrainingFailure
    from quant_foundry.training_manifest import ModelTaskSpec

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    roles = ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("label",),
        timestamp_column="timestamp",
    )
    # task_spec references a label not in roles.label_columns.
    spec = ModelTaskSpec(task_type="binary", label_column="wrong_label")
    trainer = RealLightGBMTrainer(column_roles=roles, task_spec=spec)
    req = _make_training_request("qf:train:spec:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    with pytest.raises(TrainingFailure, match="task spec"):
        trainer.train(req, deadline_ns=deadline)


def test_trainer_with_weights_uses_weight_column(tmp_path):
    """Trainer with weight_column passes sample weights to LightGBM."""
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    roles = ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("label",),
        timestamp_column="timestamp",
        weight_column="weight",
        excluded_columns=("leakage_col",),
    )
    trainer = RealLightGBMTrainer(column_roles=roles, n_folds=3)
    req = _make_training_request("qf:train:wgt:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    artifact, dossier = trainer.train(req, deadline_ns=deadline)
    # Training succeeded with weights.
    assert dossier.metadata["n_features"] == "3"
    assert artifact.artifact_id.startswith("artifact:")


def test_trainer_explicit_features_not_inferred(tmp_path):
    """Trainer must NOT infer features by dropping a few names.

    With column_roles declaring only f1 as a feature, the trainer must
    use exactly 1 feature — not infer f2, f3, etc.
    """
    from quant_foundry.dataset_manifest import ColumnRoles
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset_with_extra_cols(tmp_path)
    roles = ColumnRoles(
        feature_columns=("f1",),  # only ONE feature declared
        label_columns=("label",),
        timestamp_column="timestamp",
        excluded_columns=("leakage_col",),
    )
    trainer = RealLightGBMTrainer(column_roles=roles, n_folds=3)
    req = _make_training_request("qf:train:onefeat:1", data_path.as_uri())

    deadline = time.time_ns() + 60_000_000_000
    artifact, dossier = trainer.train(req, deadline_ns=deadline)
    assert dossier.metadata["n_features"] == "1"


# ---------------------------------------------------------------------------
# Module-level import safety
# ---------------------------------------------------------------------------


def test_column_roles_importable_from_dataset_manifest():
    from quant_foundry.dataset_manifest import (
        ColumnRoles,
        ColumnRolesValidationResult,
        validate_column_roles,
    )

    assert ColumnRoles is not None
    assert ColumnRolesValidationResult is not None
    assert callable(validate_column_roles)


def test_model_task_spec_importable_from_training_manifest():
    from quant_foundry.training_manifest import (
        ModelTaskSpec,
        validate_task_spec,
    )

    assert ModelTaskSpec is not None
    assert callable(validate_task_spec)
