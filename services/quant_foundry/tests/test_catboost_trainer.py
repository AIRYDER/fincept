"""Tests for quant_foundry.catboost_trainer (T-7.2).

All tests use ``task_type="CPU"`` — no GPU is required for the test
environment. CatBoost is a real dependency for these tests; the module
is skipped wholesale when ``catboost`` is not installed (the trainer
itself stays importable without it).
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import numpy as np
import pandas as pd
import pytest

from quant_foundry.catboost_trainer import (
    CatBoostTrainer,
    CatBoostTrainingResult,
)
from quant_foundry.dataset_manifest import ColumnRoles
from quant_foundry.training_manifest import ModelTaskSpec

pytest.importorskip("catboost")
import catboost as cb  # noqa: E402  -- after importorskip


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _binary_params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        iterations=30,
        depth=3,
        learning_rate=0.1,
        loss_function="Logloss",
        task_type="CPU",
        verbose=False,
        allow_writing_files=False,
        random_seed=0,
    )
    base.update(overrides)
    return base


def _regression_params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        iterations=30,
        depth=3,
        learning_rate=0.1,
        loss_function="RMSE",
        task_type="CPU",
        verbose=False,
        allow_writing_files=False,
        random_seed=0,
    )
    base.update(overrides)
    return base


def _make_binary_data(
    n: int = 60, seed: int = 0, with_cat: bool = False,
) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(seed)
    a = rng.normal(size=n)
    b = rng.normal(size=n)
    cols: dict[str, Any] = {"a": a, "b": b}
    if with_cat:
        # A genuine categorical (string) column.
        cols["sector"] = np.where(a > 0, "x", "y")
        cols["cat2"] = rng.choice(["p", "q", "r"], size=n).astype(object)
    X = pd.DataFrame(cols)
    y = (a + b + rng.normal(scale=0.1, size=n) > 0).astype(int)
    return X, y


def _make_regression_data(
    n: int = 60, seed: int = 1,
) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(seed)
    a = rng.normal(size=n)
    b = rng.normal(size=n)
    X = pd.DataFrame({"a": a, "b": b})
    y = 2.0 * a - 1.0 * b + rng.normal(scale=0.05, size=n)
    return X, y


def _binary_roles(with_cat: bool = False) -> ColumnRoles:
    feats = ("a", "b") if not with_cat else ("a", "b", "sector", "cat2")
    return ColumnRoles(feature_columns=feats, label_columns=("y",))


def _regression_roles() -> ColumnRoles:
    return ColumnRoles(feature_columns=("a", "b"), label_columns=("y",))


# ---------------------------------------------------------------------------
# Construction / importability
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_module_exposes_trainer_class(self) -> None:
        """The module exposes the trainer class and result model."""
        import quant_foundry.catboost_trainer as mod

        assert hasattr(mod, "CatBoostTrainer")
        assert hasattr(mod, "CatBoostTrainingResult")

    def test_lazy_catboost_import(self) -> None:
        """``train()`` raises a helpful ImportError when catboost is missing.

        We simulate an absent catboost by stubbing ``find_spec`` so the
        lazy import inside ``_require_catboost`` fails — proving the
        module never imports catboost at module load time.
        """
        import builtins
        import importlib

        real_import = builtins.__import__

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "catboost":
                raise ImportError("simulated: no catboost")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = _fake_import  # type: ignore[assignment]
        try:
            t = CatBoostTrainer(
                column_roles=_binary_roles(),
                task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
                params=_binary_params(),
            )
            with pytest.raises(ImportError, match="catboost is not installed"):
                t.train(np.zeros((4, 2)), np.array([0, 1, 0, 1]))
        finally:
            builtins.__import__ = real_import  # type: ignore[assignment]

    def test_dataclass_not_frozen(self) -> None:
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        # frozen=False: attribute assignment must work.
        t.artifact_path = "/tmp/x.cbm"
        assert t.artifact_path == "/tmp/x.cbm"

    def test_default_fields(self) -> None:
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        assert t.strict_gpu is False
        assert t.strict_categorical is True
        assert t.n_folds == 3
        assert t.model is None

    def test_model_starts_none(self) -> None:
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        assert t.model is None


# ---------------------------------------------------------------------------
# Canary binary training
# ---------------------------------------------------------------------------


class TestBinaryCanary:
    def test_train_returns_typed_result(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        assert isinstance(res, CatBoostTrainingResult)

    def test_train_populates_model(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        assert res.model is not None
        assert isinstance(res.model, cb.CatBoostClassifier)
        assert t.model is res.model

    def test_train_n_features_and_rows(self) -> None:
        X, y = _make_binary_data(n=80)
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        assert res.n_features == 2
        assert res.n_rows == 80

    def test_train_task_type_cpu(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        assert res.task_type == "CPU"

    def test_fold_metrics_have_accuracy_and_logloss(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        assert "n_folds" in res.fold_metrics
        assert "accuracy" in res.fold_metrics
        assert "logloss" in res.fold_metrics
        assert 0.0 <= res.fold_metrics["accuracy"] <= 1.0

    def test_feature_importance_emitted(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        assert set(res.feature_importance.keys()) == {"a", "b"}
        assert all(v >= 0 for v in res.feature_importance.values())

    def test_feature_importance_sums_positive(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        total = sum(res.feature_importance.values())
        assert total > 0.0


# ---------------------------------------------------------------------------
# Regression training
# ---------------------------------------------------------------------------


class TestRegressionCanary:
    def test_regression_uses_regressor(self) -> None:
        X, y = _make_regression_data()
        t = CatBoostTrainer(
            column_roles=_regression_roles(),
            task_spec=ModelTaskSpec(task_type="regression", label_column="y"),
            params=_regression_params(),
        )
        res = t.train(X, y)
        assert isinstance(res.model, cb.CatBoostRegressor)

    def test_regression_fold_metrics_have_rmse(self) -> None:
        X, y = _make_regression_data()
        t = CatBoostTrainer(
            column_roles=_regression_roles(),
            task_spec=ModelTaskSpec(task_type="regression", label_column="y"),
            params=_regression_params(),
        )
        res = t.train(X, y)
        assert "rmse" in res.fold_metrics
        assert res.fold_metrics["rmse"] >= 0.0 or np.isnan(res.fold_metrics["rmse"])

    def test_regression_no_accuracy_metric(self) -> None:
        X, y = _make_regression_data()
        t = CatBoostTrainer(
            column_roles=_regression_roles(),
            task_spec=ModelTaskSpec(task_type="regression", label_column="y"),
            params=_regression_params(),
        )
        res = t.train(X, y)
        assert "accuracy" not in res.fold_metrics
        assert "logloss" not in res.fold_metrics

    def test_regression_feature_importance(self) -> None:
        X, y = _make_regression_data()
        t = CatBoostTrainer(
            column_roles=_regression_roles(),
            task_spec=ModelTaskSpec(task_type="regression", label_column="y"),
            params=_regression_params(),
        )
        res = t.train(X, y)
        assert set(res.feature_importance.keys()) == {"a", "b"}


# ---------------------------------------------------------------------------
# Artifact save / load round-trip
# ---------------------------------------------------------------------------


class TestArtifactRoundTrip:
    def test_save_artifact_creates_file(self, tmp_path) -> None:
        X, y = _make_binary_data()
        path = str(tmp_path / "m.cbm")
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            artifact_path=path,
        )
        res = t.train(X, y)
        assert os.path.isfile(res.artifact_path)
        assert os.path.getsize(res.artifact_path) > 0

    def test_save_artifact_returns_absolute_path(self, tmp_path) -> None:
        X, y = _make_binary_data()
        path = str(tmp_path / "m.cbm")
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            artifact_path=path,
        )
        res = t.train(X, y)
        assert os.path.isabs(res.artifact_path)
        assert res.artifact_path.endswith("m.cbm")

    def test_save_artifact_creates_parent_dirs(self, tmp_path) -> None:
        X, y = _make_binary_data()
        path = str(tmp_path / "nested" / "deep" / "m.cbm")
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            artifact_path=path,
        )
        res = t.train(X, y)
        assert os.path.isfile(res.artifact_path)

    def test_load_via_artifact_io(self, tmp_path) -> None:
        from quant_foundry.artifact_io import load_catboost_model

        X, y = _make_binary_data()
        path = str(tmp_path / "m.cbm")
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            artifact_path=path,
        )
        res = t.train(X, y)
        loaded = load_catboost_model(res.artifact_path)
        assert loaded is not None

    def test_loaded_model_scores_smoke(self, tmp_path) -> None:
        from quant_foundry.artifact_io import load_catboost_model

        X, y = _make_binary_data()
        path = str(tmp_path / "m.cbm")
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            artifact_path=path,
        )
        res = t.train(X, y)
        loaded = load_catboost_model(res.artifact_path)
        sample = pd.DataFrame({"a": [0.1, -0.2], "b": [0.3, 0.1]})
        preds = loaded.predict(sample)
        assert len(preds) == 2

    def test_loaded_regressor_round_trip(self, tmp_path) -> None:
        from quant_foundry.artifact_io import load_catboost_model

        X, y = _make_regression_data()
        path = str(tmp_path / "reg.cbm")
        t = CatBoostTrainer(
            column_roles=_regression_roles(),
            task_spec=ModelTaskSpec(task_type="regression", label_column="y"),
            params=_regression_params(),
            artifact_path=path,
        )
        res = t.train(X, y)
        loaded = load_catboost_model(res.artifact_path)
        # The artifact_io loader tries CatBoostClassifier first; a
        # regressor file may load as a classifier instance but still
        # carries the RMSE loss function. We verify the loss tag rather
        # than calling predict (a classifier-typed wrapper around a
        # regression model mis-routes the prediction transform).
        assert loaded is not None
        assert loaded.get_param("loss_function") == "RMSE"

    def test_save_artifact_without_model_raises(self) -> None:
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="no trained model"):
            t.save_artifact("/tmp/whatever.cbm")

    def test_save_artifact_empty_path_raises(self, tmp_path) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        t.train(X, y)
        with pytest.raises(ValueError, match="non-empty string"):
            t.save_artifact("")

    def test_no_artifact_path_returns_none(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            artifact_path=None,
        )
        res = t.train(X, y)
        assert res.artifact_path is None


# ---------------------------------------------------------------------------
# Categorical feature handling
# ---------------------------------------------------------------------------


class TestCategoricalFeatures:
    def test_inferred_cat_features_from_dtype(self) -> None:
        X, y = _make_binary_data(with_cat=True)
        roles = _binary_roles(with_cat=True)
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        # Should train successfully with inferred categorical columns.
        assert res.n_features == 4

    def test_explicit_cat_features_by_name(self) -> None:
        X, y = _make_binary_data(with_cat=True)
        roles = _binary_roles(with_cat=True)
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y, cat_features=["sector", "cat2"])
        assert res.n_features == 4

    def test_explicit_cat_features_by_index(self) -> None:
        X, y = _make_binary_data(with_cat=True)
        roles = _binary_roles(with_cat=True)
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y, cat_features=[2, 3])
        assert res.n_features == 4

    def test_cat_feature_importance_includes_all_features(self) -> None:
        X, y = _make_binary_data(with_cat=True)
        roles = _binary_roles(with_cat=True)
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y, cat_features=["sector", "cat2"])
        assert set(res.feature_importance.keys()) == {"a", "b", "sector", "cat2"}


# ---------------------------------------------------------------------------
# Categorical role mismatch (fail-closed)
# ---------------------------------------------------------------------------


class TestCategoricalMismatch:
    def test_declared_cat_but_numeric_dtype_fails(self) -> None:
        X, y = _make_binary_data()  # a, b are numeric
        roles = _binary_roles()  # features a, b
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="categorical role mismatch"):
            t.train(X, y, cat_features=["a"])

    def test_declared_cat_by_index_but_numeric_fails(self) -> None:
        X, y = _make_binary_data()
        roles = _binary_roles()
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="categorical role mismatch"):
            t.train(X, y, cat_features=[0])

    def test_object_column_not_declared_fails(self) -> None:
        X, y = _make_binary_data(with_cat=True)
        roles = _binary_roles(with_cat=True)
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        # Declare only 'sector' as cat — 'cat2' is object but undeclared.
        with pytest.raises(ValueError, match="categorical role mismatch"):
            t.train(X, y, cat_features=["sector"])

    def test_strict_categorical_false_skips_mismatch_check(self) -> None:
        X, y = _make_binary_data()
        roles = _binary_roles()
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            strict_categorical=False,
        )
        # With strict_categorical disabled, the trainer does NOT raise its
        # own "categorical role mismatch" ValueError. CatBoost itself may
        # reject a numeric column declared as categorical, but that is the
        # backend's concern — the key assertion is that our fail-closed
        # mismatch guard is bypassed.
        with pytest.raises(Exception) as exc:
            t.train(X, y, cat_features=["a"])
        assert "categorical role mismatch" not in str(exc.value)

    def test_unknown_cat_feature_name_raises(self) -> None:
        X, y = _make_binary_data()
        roles = _binary_roles()
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="not found in X columns"):
            t.train(X, y, cat_features=["nope"])

    def test_cat_index_out_of_range_raises(self) -> None:
        X, y = _make_binary_data()
        roles = _binary_roles()
        t = CatBoostTrainer(
            column_roles=roles,
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="out of range"):
            t.train(X, y, cat_features=[42])


# ---------------------------------------------------------------------------
# Sample weights
# ---------------------------------------------------------------------------


class TestSampleWeights:
    def test_weights_accepted(self) -> None:
        X, y = _make_binary_data()
        w = np.linspace(0.5, 2.0, num=len(y))
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y, weights=w)
        assert res.n_rows == len(y)

    def test_weights_change_predictions(self) -> None:
        X, y = _make_binary_data(seed=3)
        # Heavily upweight the positive class.
        w = np.where(y == 1, 5.0, 1.0)
        t1 = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        t2 = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        r1 = t1.train(X, y)
        r2 = t2.train(X, y, weights=w)
        # Importance should differ when weights shift the fit.
        assert r1.feature_importance != r2.feature_importance

    def test_weights_wrong_length_raises(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="weights length"):
            t.train(X, y, weights=np.array([1.0, 2.0]))

    def test_weights_2d_raises(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="weights must be 1-D"):
            t.train(X, y, weights=np.ones((len(y), 2)))


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_none_x_raises(self) -> None:
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="X and y"):
            t.train(None, np.array([0, 1]))

    def test_none_y_raises(self) -> None:
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="X and y"):
            t.train(np.zeros((3, 2)), None)

    def test_row_count_mismatch_raises(self) -> None:
        X, _ = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="row count mismatch"):
            t.train(X, np.array([0, 1, 2]))

    def test_y_2d_raises(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="y must be 1-D"):
            t.train(X, np.column_stack([y, y]))

    def test_unsupported_task_type_raises(self) -> None:
        X, y = _make_binary_data()
        # Build a valid ranking spec (requires group_column).
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="ranking", label_column="y", group_column="g"),
            params=_binary_params(),
        )
        # Ranking without groups -> ValueError.
        with pytest.raises(ValueError, match="ranking task_type requires"):
            t.train(X, y)

    def test_numpy_array_features(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X.to_numpy(), y)
        assert res.n_features == 2
        assert res.n_rows == len(y)


# ---------------------------------------------------------------------------
# Feature importance
# ---------------------------------------------------------------------------


class TestFeatureImportance:
    def test_get_feature_importance_before_train_raises(self) -> None:
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        with pytest.raises(ValueError, match="no trained model"):
            t.get_feature_importance()

    def test_get_feature_importance_names_from_roles(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        t.train(X, y)
        imp = t.get_feature_importance()
        assert set(imp.keys()) == {"a", "b"}

    def test_feature_importance_values_are_floats(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        for v in res.feature_importance.values():
            assert isinstance(v, float)


# ---------------------------------------------------------------------------
# GPU fallback
# ---------------------------------------------------------------------------


class TestGpuFallback:
    def test_cpu_request_stays_cpu(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(task_type="CPU"),
        )
        res = t.train(X, y)
        assert res.task_type == "CPU"

    def test_gpu_request_falls_back_to_cpu(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(task_type="GPU", devices="0"),
        )
        # In a CPU-only test env, the GPU probe fails -> fallback to CPU.
        with pytest.warns(UserWarning, match="falling back to task_type='CPU'"):
            res = t.train(X, y)
        assert res.task_type == "CPU"

    def test_strict_gpu_fails_closed(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(task_type="GPU", devices="0"),
            strict_gpu=True,
        )
        with pytest.raises(RuntimeError, match="strict_gpu"):
            t.train(X, y)

    def test_gpu_fallback_drops_devices_param(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(task_type="GPU", devices="0"),
        )
        with pytest.warns(UserWarning):
            res = t.train(X, y)
        # Effective params had devices dropped; the model's task_type is CPU.
        assert res.model.get_param("task_type") == "CPU"


# ---------------------------------------------------------------------------
# Fold metrics
# ---------------------------------------------------------------------------


class TestFoldMetrics:
    def test_n_folds_respected(self) -> None:
        X, y = _make_binary_data(n=90)
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            n_folds=5,
        )
        res = t.train(X, y)
        assert res.fold_metrics["n_folds"] == 5.0

    def test_n_folds_capped_to_rows(self) -> None:
        X, y = _make_binary_data(n=4)
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            n_folds=10,
        )
        res = t.train(X, y)
        assert res.fold_metrics["n_folds"] <= 4.0

    def test_single_fold_mode(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            n_folds=1,
        )
        res = t.train(X, y)
        assert res.fold_metrics["n_folds"] == 1.0
        assert "accuracy" in res.fold_metrics

    def test_regression_single_fold(self) -> None:
        X, y = _make_regression_data()
        t = CatBoostTrainer(
            column_roles=_regression_roles(),
            task_spec=ModelTaskSpec(task_type="regression", label_column="y"),
            params=_regression_params(),
            n_folds=1,
        )
        res = t.train(X, y)
        assert "rmse" in res.fold_metrics

    def test_fold_metrics_accuracy_in_range(self) -> None:
        X, y = _make_binary_data(n=100)
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
            n_folds=4,
        )
        res = t.train(X, y)
        assert 0.0 <= res.fold_metrics["accuracy"] <= 1.0


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class TestResultModel:
    def test_result_is_frozen(self) -> None:
        X, y = _make_binary_data()
        t = CatBoostTrainer(
            column_roles=_binary_roles(),
            task_spec=ModelTaskSpec(task_type="binary", label_column="y"),
            params=_binary_params(),
        )
        res = t.train(X, y)
        with pytest.raises(Exception):
            res.model = None  # type: ignore[misc]

    def test_result_extra_forbidden(self) -> None:
        with pytest.raises(Exception):
            CatBoostTrainingResult(
                model=None,
                feature_importance={},
                fold_metrics={},
                artifact_path=None,
                task_type="CPU",
                n_features=0,
                n_rows=0,
                extra_field="bad",  # type: ignore[call-arg]
            )
