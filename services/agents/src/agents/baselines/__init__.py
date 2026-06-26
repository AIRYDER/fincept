"""agents.baselines - stdlib-only baseline models (no sklearn dep)."""

from agents.baselines.logreg import (
    LogRegBaseline,
    fit_logreg_baseline,
    predict_proba,
    roc_auc,
)

__all__ = [
    "LogRegBaseline",
    "fit_logreg_baseline",
    "predict_proba",
    "roc_auc",
]
