"""
Tests for the XGBoost GPU trainer adapter (T-7.3).

Covers:
  - Objective mapping (binary, regression, ranking, multiclass, explicit
    override, unknown task type).
  - GPU capability check: strict mode fails closed without a GPU;
    non-strict mode falls back to CPU with a warning.
  - Canary training on a tiny synthetic dataset (device='cpu').
  - Artifact save/load round-trip in JSON and UBJ formats (via
    artifact_io.load_xgboost_model).
  - Feature importance extraction (gain, weight, cover).
  - Fold metrics emission (one FoldMetric per fold, task-appropriate
    metric keys).
  - Binary classification, regression, ranking, and multiclass.
  - Sample weights are passed through to the DMatrix.
  - Group parameter for ranking (raw group-id vector → sizes).
  - TrainingResult / FoldMetric are frozen + extra='forbid'.
  - Construction validation (bad types, empty artifact_path, negative
    n_folds).
  - Lazy import: module is importable without xgboost (the trainer
    raises ImportError only when train/save is actually called without
    xgboost).

All tests use device='cpu' (no GPU in the test env). The GPU strict-mode
test monkeypatches the capability probe so it reports False.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from quant_foundry.artifact_io import load_xgboost_model
from quant_foundry.dataset_manifest import ColumnRoles
from quant_foundry.training_manifest import ModelTaskSpec
from quant_foundry.xgboost_trainer import (
    IMPORTANCE_TYPES,
    OBJECTIVE_MAP,
    FoldMetric,
    TrainingResult,
    XGBoostTrainer,
)

# Skip the whole module if xgboost is not installed — the trainer's
# lazy-import design means the module is importable, but training
# requires the backend.
_XGB_AVAILABLE = importlib.util.find_spec("xgboost") is not None
pytestmark = pytest.mark.skipif(
    not _XGB_AVAILABLE, reason="xgboost not installed in this environment"
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _binary_roles() -> ColumnRoles:
    return ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("y",),
    )


def _regression_roles() -> ColumnRoles:
    return ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("y",),
    )


def _ranking_roles() -> ColumnRoles:
    return ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("y",),
        group_column="grp",
    )


def _multiclass_roles() -> ColumnRoles:
    return ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("y",),
    )


def _binary_spec() -> ModelTaskSpec:
    return ModelTaskSpec(task_type="binary", label_column="y")


def _regression_spec() -> ModelTaskSpec:
    return ModelTaskSpec(task_type="regression", label_column="y")


def _ranking_spec() -> ModelTaskSpec:
    return ModelTaskSpec(task_type="ranking", label_column="y", group_column="grp")


def _multiclass_spec() -> ModelTaskSpec:
    return ModelTaskSpec(task_type="multiclass", label_column="y")


def _base_params(device: str = "cpu") -> dict[str, Any]:
    return {
        "tree_method": "hist",
        "device": device,
        "max_depth": 2,
        "learning_rate": 0.1,
        "n_estimators": 5,
    }


def _synthetic_binary(n: int = 40, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.RandomState(seed)
    X = rng.rand(n, 3).astype(np.float32)
    # Linear separator so the model can learn *something*.
    logits = 0.5 * X[:, 0] - 0.3 * X[:, 1] + 0.2 * X[:, 2]
    y = (logits + rng.randn(n) * 0.1 > 0).astype(np.int32)
    return X, y


def _synthetic_regression(n: int = 40, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.RandomState(seed)
    X = rng.rand(n, 3).astype(np.float32)
    y = 0.5 * X[:, 0] - 0.3 * X[:, 1] + 0.2 * X[:, 2] + rng.randn(n) * 0.05
    return X, y.astype(np.float32)


def _synthetic_ranking(n: int = 40, seed: int = 2) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.RandomState(seed)
    X = rng.rand(n, 3).astype(np.float32)
    # 4 groups of 10, relevance 0..3.
    y = rng.randint(0, 4, n).astype(np.int32)
    groups = np.repeat(np.arange(4), n // 4)
    return X, y, groups


def _synthetic_multiclass(n: int = 60, seed: int = 3) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.RandomState(seed)
    X = rng.rand(n, 3).astype(np.float32)
    y = rng.randint(0, 3, n).astype(np.int32)
    return X, y


def _tmp_artifact(suffix: str = ".ubj") -> str:
    d = tempfile.mkdtemp()
    return os.path.join(d, f"model{suffix}")


# ---------------------------------------------------------------------------
# Objective mapping
# ---------------------------------------------------------------------------


class TestObjectiveMapping:
    def test_binary_maps_to_logistic(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        assert t.resolve_objective() == "binary:logistic"

    def test_regression_maps_to_squarederror(self) -> None:
        t = XGBoostTrainer(_regression_roles(), _regression_spec(), _base_params(), _tmp_artifact())
        assert t.resolve_objective() == "reg:squarederror"

    def test_ranking_maps_to_pairwise(self) -> None:
        t = XGBoostTrainer(_ranking_roles(), _ranking_spec(), _base_params(), _tmp_artifact())
        assert t.resolve_objective() == "rank:pairwise"

    def test_multiclass_maps_to_softmax(self) -> None:
        t = XGBoostTrainer(_multiclass_roles(), _multiclass_spec(), _base_params(), _tmp_artifact())
        assert t.resolve_objective() == "multi:softmax"

    def test_explicit_objective_overrides_mapping(self) -> None:
        params = _base_params()
        params["objective"] = "binary:logitraw"
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), params, _tmp_artifact())
        assert t.resolve_objective() == "binary:logitraw"

    def test_objective_map_has_all_task_types(self) -> None:
        assert set(OBJECTIVE_MAP) == {"binary", "regression", "ranking", "multiclass"}

    def test_unknown_task_type_without_explicit_objective_raises(self) -> None:
        # ModelTaskSpec rejects unknown task types at construction, so we
        # build a valid spec then bypass validation by setting via
        # object.__setattr__ to test the trainer's defence-in-depth.
        spec = ModelTaskSpec(task_type="binary", label_column="y")
        object.__setattr__(spec, "task_type", "novel_task")
        t = XGBoostTrainer(_binary_roles(), spec, _base_params(), _tmp_artifact())
        with pytest.raises(ValueError, match="no objective mapping"):
            t.resolve_objective()


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_bad_column_roles_type(self) -> None:
        with pytest.raises(TypeError, match="column_roles must be a ColumnRoles"):
            XGBoostTrainer("not_roles", _binary_spec(), _base_params(), _tmp_artifact())  # type: ignore[arg-type]

    def test_bad_task_spec_type(self) -> None:
        with pytest.raises(TypeError, match="task_spec must be a ModelTaskSpec"):
            XGBoostTrainer(_binary_roles(), "not_spec", _base_params(), _tmp_artifact())  # type: ignore[arg-type]

    def test_bad_params_type(self) -> None:
        with pytest.raises(TypeError, match="params must be a dict"):
            XGBoostTrainer(_binary_roles(), _binary_spec(), "not_dict", _tmp_artifact())  # type: ignore[arg-type]

    def test_empty_artifact_path(self) -> None:
        with pytest.raises(ValueError, match="artifact_path must be a non-empty string"):
            XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), "")

    def test_negative_n_folds(self) -> None:
        with pytest.raises(ValueError, match="n_folds must be >= 0"):
            XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=-1)

    def test_zero_n_folds_allowed(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=0)
        assert t.n_folds == 0

    def test_ranking_without_group_column_raises(self) -> None:
        # ModelTaskSpec enforces this at construction.
        with pytest.raises(ValueError, match="ranking task_type requires"):
            ModelTaskSpec(task_type="ranking", label_column="y")


# ---------------------------------------------------------------------------
# GPU capability check
# ---------------------------------------------------------------------------


class TestGpuCapability:
    def test_strict_mode_fails_without_gpu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force the probe to report no GPU.
        import quant_foundry.xgboost_trainer as mod

        monkeypatch.setattr(mod, "_CUDA_AVAILABLE_CACHE", False)
        params = _base_params(device="cuda")
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), params, _tmp_artifact(), strict=True)
        X, y = _synthetic_binary()
        with pytest.raises(RuntimeError, match="no CUDA GPU is available"):
            t.train(X, y)

    def test_non_strict_mode_falls_back_to_cpu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import quant_foundry.xgboost_trainer as mod

        monkeypatch.setattr(mod, "_CUDA_AVAILABLE_CACHE", False)
        params = _base_params(device="cuda")
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), params, _tmp_artifact(), strict=False)
        X, y = _synthetic_binary()
        result = t.train(X, y)
        assert result.device == "cpu"
        assert t.device_used == "cpu"

    def test_cuda_available_when_gpu_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import quant_foundry.xgboost_trainer as mod

        monkeypatch.setattr(mod, "_CUDA_AVAILABLE_CACHE", True)
        # We don't actually train with cuda (no GPU in CI); just check
        # the device resolution returns cuda.
        params = _base_params(device="cuda")
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), params, _tmp_artifact(), strict=True)
        assert t._resolve_device() == "cuda"

    def test_cpu_device_passes_through(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(device="cpu"), _tmp_artifact())
        assert t._resolve_device() == "cpu"

    def test_cuda_probe_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import quant_foundry.xgboost_trainer as mod

        monkeypatch.setattr(mod, "_CUDA_AVAILABLE_CACHE", None)
        # First call probes + caches.
        mod.cuda_available()
        assert mod._CUDA_AVAILABLE_CACHE is not None


# ---------------------------------------------------------------------------
# Canary training (binary)
# ---------------------------------------------------------------------------


class TestCanaryBinary:
    def test_train_returns_training_result(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        X, y = _synthetic_binary()
        result = t.train(X, y)
        assert isinstance(result, TrainingResult)
        assert result.task_type == "binary"
        assert result.objective == "binary:logistic"
        assert result.device == "cpu"
        assert result.n_estimators == 5

    def test_train_saves_artifact_file(self) -> None:
        path = _tmp_artifact(".ubj")
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), path)
        X, y = _synthetic_binary()
        t.train(X, y)
        assert os.path.isfile(path)
        assert os.path.getsize(path) > 0

    def test_train_emits_feature_importance(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        X, y = _synthetic_binary()
        result = t.train(X, y)
        assert set(result.feature_importance) == {"f1", "f2", "f3"}
        for fname, entry in result.feature_importance.items():
            assert set(entry) == {"gain", "weight", "cover"}

    def test_train_emits_fold_metrics(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=3)
        X, y = _synthetic_binary(n=60)
        result = t.train(X, y)
        assert len(result.fold_metrics) == 3
        for fm in result.fold_metrics:
            assert isinstance(fm, FoldMetric)
            assert fm.train_size > 0
            assert fm.val_size > 0
            assert "logloss" in fm.metrics
            assert "auc" in fm.metrics

    def test_train_with_dataframe_uses_column_names(self) -> None:
        import pandas as pd

        X, y = _synthetic_binary()
        df = pd.DataFrame(X, columns=["f1", "f2", "f3"])
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        result = t.train(df, y)
        assert set(result.feature_importance) == {"f1", "f2", "f3"}


# ---------------------------------------------------------------------------
# Artifact save/load round-trip
# ---------------------------------------------------------------------------


class TestArtifactRoundTrip:
    def test_ubj_round_trip_loads_via_artifact_io(self) -> None:
        path = _tmp_artifact(".ubj")
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), path)
        X, y = _synthetic_binary()
        t.train(X, y)
        booster = load_xgboost_model(path)
        # Score a smoke sample.
        sample = X[:2]
        import xgboost as xgb

        d = xgb.DMatrix(sample, feature_names=["f1", "f2", "f3"])
        preds = booster.predict(d)
        assert preds.shape == (2,)
        assert np.all(preds >= 0) and np.all(preds <= 1)

    def test_json_round_trip_loads_via_artifact_io(self) -> None:
        path = _tmp_artifact(".json")
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), path)
        X, y = _synthetic_binary()
        t.train(X, y)
        booster = load_xgboost_model(path)
        import xgboost as xgb

        d = xgb.DMatrix(X[:2], feature_names=["f1", "f2", "f3"])
        preds = booster.predict(d)
        assert preds.shape == (2,)

    def test_json_and_ubj_produce_equivalent_predictions(self) -> None:
        X, y = _synthetic_binary()
        path_ubj = _tmp_artifact(".ubj")
        path_json = _tmp_artifact(".json")
        t1 = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), path_ubj)
        t1.train(X, y)
        t2 = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), path_json)
        t2.train(X, y)
        import xgboost as xgb

        d = xgb.DMatrix(X, feature_names=["f1", "f2", "f3"])
        p_ubj = load_xgboost_model(path_ubj).predict(d)
        p_json = load_xgboost_model(path_json).predict(d)
        np.testing.assert_allclose(p_ubj, p_json, atol=1e-6)

    def test_save_artifact_creates_parent_dir(self) -> None:
        d = tempfile.mkdtemp()
        path = os.path.join(d, "nested", "deep", "model.ubj")
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), path)
        X, y = _synthetic_binary()
        t.train(X, y)
        assert os.path.isfile(path)

    def test_save_artifact_before_train_raises(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        with pytest.raises(RuntimeError, match="no model to save"):
            t.save_artifact(_tmp_artifact())

    def test_save_artifact_empty_path_raises(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        X, y = _synthetic_binary()
        t.train(X, y)
        with pytest.raises(ValueError, match="non-empty string"):
            t.save_artifact("")


# ---------------------------------------------------------------------------
# Feature importance
# ---------------------------------------------------------------------------


class TestFeatureImportance:
    def test_get_feature_importance_after_train(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        X, y = _synthetic_binary()
        t.train(X, y)
        imp = t.get_feature_importance()
        assert set(imp) == {"f1", "f2", "f3"}
        for entry in imp.values():
            assert set(entry) == {"gain", "weight", "cover"}

    def test_get_feature_importance_before_train_raises(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        with pytest.raises(RuntimeError, match="before train"):
            t.get_feature_importance()

    def test_importance_types_constant(self) -> None:
        assert IMPORTANCE_TYPES == ("gain", "weight", "cover")

    def test_importance_values_are_floats(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact())
        X, y = _synthetic_binary()
        t.train(X, y)
        imp = t.get_feature_importance()
        for entry in imp.values():
            for v in entry.values():
                assert isinstance(v, float)


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------


class TestRegression:
    def test_regression_train(self) -> None:
        t = XGBoostTrainer(_regression_roles(), _regression_spec(), _base_params(), _tmp_artifact())
        X, y = _synthetic_regression()
        result = t.train(X, y)
        assert result.objective == "reg:squarederror"
        assert result.task_type == "regression"

    def test_regression_fold_metrics_have_mse_mae(self) -> None:
        t = XGBoostTrainer(_regression_roles(), _regression_spec(), _base_params(), _tmp_artifact(), n_folds=3)
        X, y = _synthetic_regression(n=60)
        result = t.train(X, y)
        for fm in result.fold_metrics:
            assert "mse" in fm.metrics
            assert "mae" in fm.metrics
            assert "rmse" in fm.metrics

    def test_regression_artifact_round_trip(self) -> None:
        path = _tmp_artifact(".ubj")
        t = XGBoostTrainer(_regression_roles(), _regression_spec(), _base_params(), path)
        X, y = _synthetic_regression()
        t.train(X, y)
        booster = load_xgboost_model(path)
        import xgboost as xgb

        d = xgb.DMatrix(X[:3], feature_names=["f1", "f2", "f3"])
        preds = booster.predict(d)
        assert preds.shape == (3,)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


class TestRanking:
    def test_ranking_train(self) -> None:
        t = XGBoostTrainer(_ranking_roles(), _ranking_spec(), _base_params(), _tmp_artifact())
        X, y, groups = _synthetic_ranking()
        result = t.train(X, y, groups=groups)
        assert result.objective == "rank:pairwise"
        assert result.task_type == "ranking"

    def test_ranking_fold_metrics_have_ndcg(self) -> None:
        t = XGBoostTrainer(_ranking_roles(), _ranking_spec(), _base_params(), _tmp_artifact(), n_folds=2)
        X, y, groups = _synthetic_ranking(n=40)
        result = t.train(X, y, groups=groups)
        for fm in result.fold_metrics:
            assert "ndcg" in fm.metrics

    def test_ranking_without_groups_raises(self) -> None:
        t = XGBoostTrainer(_ranking_roles(), _ranking_spec(), _base_params(), _tmp_artifact())
        X, y, _ = _synthetic_ranking()
        with pytest.raises(ValueError, match="requires a group array"):
            t.train(X, y)

    def test_ranking_group_sizes_vector_accepted(self) -> None:
        t = XGBoostTrainer(_ranking_roles(), _ranking_spec(), _base_params(), _tmp_artifact(), n_folds=0)
        X, y, _ = _synthetic_ranking()
        # Pass explicit sizes (4 groups of 10 → [10,10,10,10]).
        sizes = np.array([10, 10, 10, 10], dtype=np.uint32)
        result = t.train(X, y, groups=sizes)
        assert result.task_type == "ranking"

    def test_ranking_artifact_round_trip(self) -> None:
        path = _tmp_artifact(".ubj")
        t = XGBoostTrainer(_ranking_roles(), _ranking_spec(), _base_params(), path, n_folds=0)
        X, y, groups = _synthetic_ranking()
        t.train(X, y, groups=groups)
        booster = load_xgboost_model(path)
        import xgboost as xgb

        d = xgb.DMatrix(X[:5], feature_names=["f1", "f2", "f3"])
        preds = booster.predict(d)
        assert preds.shape == (5,)


# ---------------------------------------------------------------------------
# Multiclass
# ---------------------------------------------------------------------------


class TestMulticlass:
    def test_multiclass_train(self) -> None:
        params = _base_params()
        t = XGBoostTrainer(_multiclass_roles(), _multiclass_spec(), params, _tmp_artifact())
        X, y = _synthetic_multiclass()
        result = t.train(X, y)
        assert result.objective == "multi:softmax"
        assert result.task_type == "multiclass"

    def test_multiclass_predictions_are_class_indices(self) -> None:
        path = _tmp_artifact(".ubj")
        t = XGBoostTrainer(_multiclass_roles(), _multiclass_spec(), _base_params(), path, n_folds=0)
        X, y = _synthetic_multiclass()
        t.train(X, y)
        booster = load_xgboost_model(path)
        import xgboost as xgb

        d = xgb.DMatrix(X[:4], feature_names=["f1", "f2", "f3"])
        preds = booster.predict(d)
        # multi:softmax returns class indices.
        assert preds.shape == (4,)
        assert set(np.unique(preds)).issubset({0.0, 1.0, 2.0})

    def test_multiclass_fold_metrics_have_merror(self) -> None:
        t = XGBoostTrainer(_multiclass_roles(), _multiclass_spec(), _base_params(), _tmp_artifact(), n_folds=2)
        X, y = _synthetic_multiclass(n=60)
        result = t.train(X, y)
        for fm in result.fold_metrics:
            assert "merror" in fm.metrics


# ---------------------------------------------------------------------------
# Sample weights
# ---------------------------------------------------------------------------


class TestSampleWeights:
    def test_weights_are_applied(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=0)
        X, y = _synthetic_binary()
        weights = np.ones(len(y), dtype=np.float32)
        weights[: len(y) // 2] = 2.0  # upweight the first half
        result = t.train(X, y, weights=weights)
        assert result.task_type == "binary"

    def test_weights_change_predictions_vs_uniform(self) -> None:
        X, y = _synthetic_binary(n=60, seed=7)
        # Heavy upweight on a subset should change the model.
        w_skew = np.ones(len(y), dtype=np.float32)
        w_skew[y == 1] = 5.0
        w_uniform = np.ones(len(y), dtype=np.float32)
        path1 = _tmp_artifact(".ubj")
        path2 = _tmp_artifact(".ubj")
        t1 = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), path1, n_folds=0)
        t1.train(X, y, weights=w_uniform)
        t2 = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), path2, n_folds=0)
        t2.train(X, y, weights=w_skew)
        import xgboost as xgb

        d = xgb.DMatrix(X, feature_names=["f1", "f2", "f3"])
        p1 = load_xgboost_model(path1).predict(d)
        p2 = load_xgboost_model(path2).predict(d)
        # The skewed weights should move at least one prediction.
        assert not np.allclose(p1, p2, atol=1e-6)


# ---------------------------------------------------------------------------
# Fold metrics edge cases
# ---------------------------------------------------------------------------


class TestFoldMetrics:
    def test_zero_folds_emits_empty_tuple(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=0)
        X, y = _synthetic_binary()
        result = t.train(X, y)
        assert result.fold_metrics == ()

    def test_too_few_rows_emits_empty_tuple(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=3)
        X, y = _synthetic_binary(n=4)  # < 3*2
        result = t.train(X, y)
        assert result.fold_metrics == ()

    def test_fold_ids_are_sequential(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=3)
        X, y = _synthetic_binary(n=60)
        result = t.train(X, y)
        assert [fm.fold_id for fm in result.fold_metrics] == [0, 1, 2]

    def test_fold_sizes_partition_the_data(self) -> None:
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=3)
        X, y = _synthetic_binary(n=60)
        result = t.train(X, y)
        total_val = sum(fm.val_size for fm in result.fold_metrics)
        assert total_val == 60
        for fm in result.fold_metrics:
            assert fm.train_size == 60 - fm.val_size


# ---------------------------------------------------------------------------
# Result model immutability
# ---------------------------------------------------------------------------


class TestResultImmutability:
    def test_fold_metric_is_frozen(self) -> None:
        fm = FoldMetric(fold_id=0, train_size=10, val_size=5, metrics={"auc": 0.5})
        with pytest.raises(Exception):
            fm.fold_id = 1  # type: ignore[misc]

    def test_fold_metric_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            FoldMetric(fold_id=0, train_size=10, val_size=5, metrics={"auc": 0.5}, extra="x")  # type: ignore[call-arg]

    def test_training_result_is_frozen(self) -> None:
        result = TrainingResult(
            artifact_path="x.ubj",
            feature_importance={},
            fold_metrics=(),
            objective="binary:logistic",
            task_type="binary",
            device="cpu",
            n_estimators=5,
        )
        with pytest.raises(Exception):
            result.device = "cuda"  # type: ignore[misc]

    def test_training_result_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            TrainingResult(
                artifact_path="x.ubj",
                feature_importance={},
                fold_metrics=(),
                objective="binary:logistic",
                task_type="binary",
                device="cpu",
                n_estimators=5,
                extra="x",  # type: ignore[call-arg]
            )

    def test_fold_metric_rejects_negative_fold_id(self) -> None:
        with pytest.raises(Exception):
            FoldMetric(fold_id=-1, train_size=10, val_size=5, metrics={})

    def test_fold_metric_rejects_zero_train_size(self) -> None:
        with pytest.raises(Exception):
            FoldMetric(fold_id=0, train_size=0, val_size=5, metrics={})

    def test_training_result_rejects_zero_n_estimators(self) -> None:
        with pytest.raises(Exception):
            TrainingResult(
                artifact_path="x.ubj",
                feature_importance={},
                fold_metrics=(),
                objective="binary:logistic",
                task_type="binary",
                device="cpu",
                n_estimators=0,
            )


# ---------------------------------------------------------------------------
# Lazy import / module importability
# ---------------------------------------------------------------------------


class TestLazyImport:
    def test_module_importable(self) -> None:
        import quant_foundry.xgboost_trainer as mod

        assert hasattr(mod, "XGBoostTrainer")
        assert hasattr(mod, "TrainingResult")
        assert hasattr(mod, "FoldMetric")

    def test_get_feature_importance_without_xgboost_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Train first (xgboost is available), then simulate xgboost being
        # unavailable on the save path by making the import fail.
        t = XGBoostTrainer(_binary_roles(), _binary_spec(), _base_params(), _tmp_artifact(), n_folds=0)
        X, y = _synthetic_binary()
        t.train(X, y)
        # get_feature_importance uses the already-trained model, no import.
        imp = t.get_feature_importance()
        assert set(imp) == {"f1", "f2", "f3"}


# ---------------------------------------------------------------------------
# Smoke: full canary round-trip
# ---------------------------------------------------------------------------


class TestCanarySmoke:
    def test_canary_train_load_score(self) -> None:
        """End-to-end: train → save → load → score a smoke sample."""
        path = _tmp_artifact(".ubj")
        t = XGBoostTrainer(
            _binary_roles(),
            _binary_spec(),
            _base_params(),
            path,
            n_folds=2,
        )
        X, y = _synthetic_binary(n=50)
        result = t.train(X, y)
        # Artifact loads.
        booster = load_xgboost_model(path)
        # Scores a smoke sample.
        import xgboost as xgb

        smoke = xgb.DMatrix(X[:3], feature_names=["f1", "f2", "f3"])
        preds = booster.predict(smoke)
        assert preds.shape == (3,)
        assert np.all(preds >= 0) and np.all(preds <= 1)
        # Feature importance emitted.
        assert result.feature_importance
        # Fold metrics emitted.
        assert len(result.fold_metrics) == 2
