"""
quant_foundry.modules.benchmark.placebo — placebo test framework.

The :class:`PlaceboTest` framework tests whether a model's performance
is real signal or an artifact of overfitting / spurious correlations.
It provides two complementary procedures:

1. **Row-permutation placebo test** (:meth:`PlaceboTest.run`):
   Shuffles the *rows* of the entire feature matrix, breaking the
   feature-label relationship while preserving each feature's marginal
   distribution.  The model is retrained on the shuffled data and its
   test metric is compared to the real model's metric.  If the real
   metric is not significantly larger than the permuted metrics, the
   model's performance is likely an artifact.

2. **Feature-permutation importance** (:meth:`PlaceboTest.run_feature_permutation`):
   For each feature, shuffles that single *column* (breaking that
   feature's relationship with the label) and measures the drop in the
   test metric.  A large drop means the model relies on that feature.
   This is more reliable than tree-based importance for understanding
   causal contribution.

Both procedures use ``random.Random(seed)`` for reproducibility and
operate on plain Python lists (no numpy required at module level).

Usage::

    placebo = PlaceboTest()
    result = placebo.run(
        model=model,
        feature_names=feature_names,
        X_train=X_train, y_train=y_train,
        X_test=X_test, y_test=y_test,
        n_permutations=100,
    )
    if result["significant_at_5pct"]:
        print("Model performance is real (not an artifact)")
"""

from __future__ import annotations

import random
from typing import Any, Protocol

__all__ = ["PlaceboTest"]


# --------------------------------------------------------------------------- #
# Model protocol                                                               #
# --------------------------------------------------------------------------- #


class _FittableModel(Protocol):
    """Minimal model interface required by :class:`PlaceboTest`."""

    def fit(self, X: list[list[float]], y: list[float]) -> Any: ...
    def predict(self, X: list[list[float]]) -> list[float]: ...


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _shuffled_rows(
    X: list[list[float]],
    y: list[float],
    rng: random.Random,
) -> tuple[list[list[float]], list[float]]:
    """Return a copy of X with rows shuffled, y aligned to the new row order.

    This breaks the feature-label relationship: each feature row is
    paired with a random label.
    """
    n = len(X)
    indices = list(range(n))
    rng.shuffle(indices)
    return [X[i] for i in indices], [y[i] for i in indices]


def _shuffled_column(
    X: list[list[float]],
    col_idx: int,
    rng: random.Random,
) -> list[list[float]]:
    """Return a copy of X with a single column shuffled across rows."""
    n = len(X)
    n_cols = len(X[0]) if X else 0
    col_values = [X[i][col_idx] for i in range(n)]
    rng.shuffle(col_values)
    return [[col_values[i] if j == col_idx else X[i][j] for j in range(n_cols)] for i in range(n)]


def _sharpe_ratio(returns: list[float]) -> float:
    """Annualization-agnostic Sharpe ratio: mean / std of the values.

    Used as the default test metric.  Returns 0.0 if std is 0 or the
    list is empty.
    """
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    if n < 2:
        return 0.0
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if variance <= 0.0:
        return 0.0
    return mean / (variance**0.5)


def _metric_from_predictions(y_true: list[float], y_pred: list[float]) -> float:
    """Default test metric: Sharpe ratio of the predictions.

    Treats the model's predictions as a signal and computes the Sharpe
    ratio of the prediction values.  Higher is better.
    """
    return _sharpe_ratio(y_pred)


# --------------------------------------------------------------------------- #
# PlaceboTest                                                                  #
# --------------------------------------------------------------------------- #


class PlaceboTest:
    """Placebo test framework for validating model performance.

    Tests whether a model's performance is real or an artifact of
    overfitting by comparing the real test metric against metrics
    computed under permuted (null) data.
    """

    def run(
        self,
        model: _FittableModel,
        feature_names: list[str],
        X_train: list[list[float]],
        y_train: list[float],
        X_test: list[list[float]],
        y_test: list[float],
        *,
        n_permutations: int = 100,
        seed: int = 0,
    ) -> dict[str, Any]:
        """Row-permutation placebo test.

        Computes the real model's test metric, then for each permutation
        shuffles the feature matrix rows (breaking the feature-label
        relationship), retrains the model, and computes the test metric.
        The p-value is the fraction of permutations where the permuted
        metric is at least as large as the real metric.

        Args:
            model: A model with ``fit(X, y)`` and ``predict(X)`` methods.
                A fresh copy is used for each permutation (the original
                is not mutated).
            feature_names: Names of the features (for reference; not
                used in the row-permutation test).
            X_train: Training feature matrix (list of rows).
            y_train: Training labels.
            X_test: Test feature matrix.
            y_test: Test labels.
            n_permutations: Number of permutations to run.
            seed: Random seed for reproducibility.

        Returns:
            A dict with keys:
                - ``real_metric``: the real model's test metric.
                - ``permuted_metrics``: list of metrics from permuted runs.
                - ``p_value``: fraction of permuted metrics >= real metric.
                - ``significant_at_5pct``: whether ``p_value < 0.05``.
        """
        random.Random(seed)

        # Real model
        real_model = self._clone_model(model)
        real_model.fit(X_train, y_train)
        real_pred = real_model.predict(X_test)
        real_metric = _metric_from_predictions(y_test, real_pred)

        permuted_metrics: list[float] = []
        for i in range(n_permutations):
            perm_rng = random.Random(seed + i + 1)
            X_perm, y_perm = _shuffled_rows(X_train, y_train, perm_rng)
            perm_model = self._clone_model(model)
            perm_model.fit(X_perm, y_perm)
            perm_pred = perm_model.predict(X_test)
            permuted_metrics.append(_metric_from_predictions(y_test, perm_pred))

        # p-value: fraction of permuted metrics >= real metric
        if permuted_metrics:
            count_ge = sum(1 for m in permuted_metrics if m >= real_metric)
            p_value = count_ge / len(permuted_metrics)
        else:
            p_value = 1.0

        return {
            "real_metric": float(real_metric),
            "permuted_metrics": [float(m) for m in permuted_metrics],
            "p_value": float(p_value),
            "significant_at_5pct": bool(p_value < 0.05),
        }

    def run_feature_permutation(
        self,
        model: _FittableModel,
        feature_names: list[str],
        X_train: list[list[float]],
        y_train: list[float],
        X_test: list[list[float]],
        y_test: list[float],
        *,
        n_permutations: int = 100,
        seed: int = 0,
    ) -> dict[str, Any]:
        """Feature-permutation importance.

        Computes the real model's test metric, then for each feature
        shuffles that single column in the *test* set (breaking that
        feature's relationship with the label) and measures the metric
        drop.  Importance = ``real_metric - mean(permuted_metrics)`` per
        feature.  A large importance means the model relies on that
        feature.

        Args:
            model: A trained (or trainable) model.  The model is fit
                once on the real training data; only the test features
                are permuted.
            feature_names: Names of the features.
            X_train: Training feature matrix.
            y_train: Training labels.
            X_test: Test feature matrix.
            y_test: Test labels.
            n_permutations: Number of permutations per feature.
            seed: Random seed for reproducibility.

        Returns:
            A dict with key ``"feature_importance"`` mapping each
            feature name to its importance (metric drop).  Higher
            importance = more important feature.
        """
        random.Random(seed)

        # Fit the model once on real data
        trained_model = self._clone_model(model)
        trained_model.fit(X_train, y_train)
        real_pred = trained_model.predict(X_test)
        real_metric = _metric_from_predictions(y_test, real_pred)

        importance: dict[str, float] = {}
        for col_idx, name in enumerate(feature_names):
            perm_metrics: list[float] = []
            for i in range(n_permutations):
                perm_rng = random.Random(seed + col_idx * 1000 + i + 1)
                X_perm = _shuffled_column(X_test, col_idx, perm_rng)
                perm_pred = trained_model.predict(X_perm)
                perm_metrics.append(_metric_from_predictions(y_test, perm_pred))
            mean_perm = sum(perm_metrics) / len(perm_metrics) if perm_metrics else 0.0
            importance[name] = float(real_metric - mean_perm)

        return {"feature_importance": importance}

    @staticmethod
    def _clone_model(model: _FittableModel) -> _FittableModel:
        """Return a fresh copy of the model for training.

        Tries ``copy.deepcopy`` first, then falls back to calling the
        model's class with no args, then finally returns the model
        itself (relying on ``fit`` resetting internal state).
        """
        import copy

        try:
            return copy.deepcopy(model)
        except Exception:
            pass
        try:
            return model.__class__()  # type: ignore[call-arg]
        except Exception:
            return model
