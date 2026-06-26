"""agents.baselines.logreg - stdlib-only logistic regression baseline.

A tiny, dependency-light logistic regression trained by full-batch
gradient descent.  Intended as a *sanity-check* baseline against the
LightGBM trainer in ``agents.gbm_predictor`` -- NOT a production model.
No sklearn dependency is introduced; only ``numpy`` (already a workspace
dep of the agents service) and the Python stdlib are used.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

import numpy as np


def _sigmoid(z: np.ndarray) -> np.ndarray:
    # Numerically stable sigmoid; clips the argument to avoid overflow.
    z = np.clip(z, -30.0, 30.0)
    return np.asarray(1.0 / (1.0 + np.exp(-z)), dtype=float)


@dataclass
class LogRegBaseline:
    """A fitted logistic-regression baseline (picklable dataclass)."""

    weights: np.ndarray
    bias: float
    n_features: int
    n_iter: int = 0
    loss_history: list[float] = field(default_factory=list)

    def decision_function(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[1] != self.n_features:
            raise ValueError(
                f"X must be 2-D with {self.n_features} columns; got shape {X.shape}"
            )
        return np.asarray(X @ self.weights + self.bias, dtype=float)


def fit_logreg_baseline(
    X: np.ndarray,
    y: np.ndarray,
    *,
    max_iter: int = 200,
    C: float = 1.0,
    lr: float = 0.1,
) -> LogRegBaseline:
    """Fit a binary logistic regression by full-batch gradient descent.

    ``C`` is the inverse-L2 strength (larger => less regularization), matching
    the sklearn convention.  Returns a picklable :class:`LogRegBaseline`.
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if X.ndim != 2:
        raise ValueError(f"X must be 2-D; got shape {X.shape}")
    if y.ndim != 1 or y.shape[0] != X.shape[0]:
        raise ValueError(f"y must be 1-D with {X.shape[0]} rows; got {y.shape}")
    n_samples, n_features = X.shape
    weights = np.zeros(n_features, dtype=float)
    bias = 0.0
    loss_history: list[float] = []
    for _ in range(max_iter):
        logits = X @ weights + bias
        probs = _sigmoid(logits)
        error = probs - y
        grad_w = (X.T @ error) / n_samples + (1.0 / max(C, 1e-12)) * weights / n_samples
        grad_b = float(statistics.mean(error.tolist()))
        weights -= lr * grad_w
        bias -= lr * grad_b
        eps = 1e-12
        loss = -float(
            np.sum(y * np.log(probs + eps) + (1 - y) * np.log(1 - probs + eps))
        ) / n_samples
        loss_history.append(loss)
    return LogRegBaseline(
        weights=weights,
        bias=bias,
        n_features=n_features,
        n_iter=max_iter,
        loss_history=loss_history,
    )


def predict_proba(model: Any, X: np.ndarray) -> np.ndarray:
    """Return a 2-column probability matrix ``[P(y=0), P(y=1)]``.

    Accepts a :class:`LogRegBaseline` or any object exposing
    ``decision_function`` / ``n_features`` (sklearn-compatible shape).
    """
    if hasattr(model, "decision_function"):
        scores = model.decision_function(X)
    elif hasattr(model, "weights") and hasattr(model, "bias"):
        X = np.asarray(X, dtype=float)
        scores = X @ model.weights + model.bias
    else:  # pragma: no cover - defensive
        raise TypeError("model must expose decision_function() or weights/bias")
    p1 = _sigmoid(np.asarray(scores, dtype=float))
    return np.column_stack([1.0 - p1, p1])


def roc_auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Compute ROC AUC via the Mann-Whitney U statistic (no sklearn)."""
    y_true = np.asarray(y_true, dtype=float).ravel()
    y_score = np.asarray(y_score, dtype=float).ravel()
    if y_true.shape != y_score.shape:
        raise ValueError("y_true and y_score must have the same shape")
    pos = y_score[y_true == 1.0]
    neg = y_score[y_true == 0.0]
    if pos.size == 0 or neg.size == 0:
        raise ValueError("roc_auc undefined when one class is empty")
    # Pairwise comparison via broadcasting; ties count as 0.5.
    diff = pos[:, None] - neg[None, :]
    wins = float(np.sum(diff > 0)) + 0.5 * float(np.sum(diff == 0))
    return float(wins / (pos.size * neg.size))
