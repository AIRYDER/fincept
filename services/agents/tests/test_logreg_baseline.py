"""Tests for agents.baselines.logreg (stdlib-only logistic regression)."""

from __future__ import annotations

import numpy as np
import pytest

from agents.baselines.logreg import (
    fit_logreg_baseline,
    predict_proba,
    roc_auc,
)


def _separable_dataset() -> tuple[np.ndarray, np.ndarray]:
    """A perfectly separable 2-D dataset (AUC must reach 1.0)."""
    X = np.array(
        [
            [-3.0, -3.0],
            [-2.5, -2.0],
            [-2.0, -3.5],
            [-1.8, -1.2],
            [3.0, 3.0],
            [2.5, 2.0],
            [2.0, 3.5],
            [1.8, 1.2],
        ]
    )
    y = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    return X, y


def test_separable_dataset_auc_is_one() -> None:
    X, y = _separable_dataset()
    model = fit_logreg_baseline(X, y, max_iter=500, C=10.0, lr=0.5)
    proba = predict_proba(model, X)
    assert proba.shape == (X.shape[0], 2)
    # Probabilities for the two classes should be near {0.0, 1.0}.
    assert np.allclose(np.argmax(proba, axis=1), y)
    auc = roc_auc(y, proba[:, 1])
    assert auc == pytest.approx(1.0, abs=1e-6)


def test_predict_proba_mismatched_features_raises() -> None:
    X, y = _separable_dataset()
    model = fit_logreg_baseline(X, y, max_iter=50)
    bad = np.array([[1.0, 2.0, 3.0]])
    with pytest.raises(ValueError):
        predict_proba(model, bad)


def test_convergence_produces_non_nan_array() -> None:
    rng = np.random.default_rng(42)
    X = rng.normal(size=(40, 3))
    true_w = np.array([1.5, -2.0, 0.5])
    logits = X @ true_w + 0.3
    y = (logits + rng.normal(scale=0.05, size=logits.shape) > 0).astype(int)
    model = fit_logreg_baseline(X, y, max_iter=200)
    proba = predict_proba(model, X)
    assert proba.shape == (40, 2)
    assert not np.any(np.isnan(proba))
    # Row sums to 1 (probability simplex).
    assert np.allclose(proba.sum(axis=1), 1.0)
    # AUC on noisy-but-informative data should beat random chance.
    assert roc_auc(y, proba[:, 1]) > 0.55
