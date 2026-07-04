"""
quant_foundry.calibration — Calibrated Probability Layer (T-7.4).

Applies **Platt scaling** (logistic regression) or **isotonic regression** as
a post-fit step for binary classification models, plus the calibration
diagnostics (ECE, Brier score, log-loss, reliability buckets) and the
promotion-eligibility policy hook.

Design invariants (enforced + tested):
- **Pydantic v2 models are frozen + ``extra='forbid'``** (audit integrity).
- **Lazy import of sklearn inside methods** — the module is importable
  without scikit-learn so environments that only need the policy / metric
  helpers (which are pure-Python) are not blocked. The Platt / isotonic
  ``Calibrator.fit`` raises a clear ``ImportError`` if sklearn is missing.
- **Fail-closed for ``NONE``**: :class:`CalibrationMethod.NONE` returns the
  raw probabilities unchanged (no silent calibration).
- **Calibration improves ECE on synthetic miscalibrated data** (verified by
  the test suite): a sigmoid-distorted probability stream is recovered to
  near-calibrated ECE by both Platt and isotonic calibrators.

File-disjoint from ``real_trainer.py`` — integration is handled by another
builder. This module only exposes the standalone calibration surface.
"""

from __future__ import annotations

import math
import pickle
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CalibrationMethod(StrEnum):
    """Post-fit probability calibration method.

    - ``PLATT``: Platt scaling — fits a logistic regression on the raw
      probabilities against the labels (a single sigmoid).
    - ``ISOTONIC``: isotonic regression — fits a non-parametric
      monotone mapping from raw probabilities to labels.
    - ``NONE``: no calibration; the calibrator is a pass-through. This is
      the fail-closed default so a caller that forgets to set a method
      never silently distorts probabilities.
    """

    PLATT = "platt"
    ISOTONIC = "isotonic"
    NONE = "none"


class CalibrationPolicy(StrEnum):
    """Promotion-time calibration policy.

    - ``REQUIRED``: a calibration result MUST be present for the model to
      be eligible for promotion.
    - ``OPTIONAL``: a calibration result may or may not be present; the
      model is eligible either way.
    - ``NONE``: the model is eligible only when NO calibration result is
      present (calibration explicitly disabled for this task).
    """

    REQUIRED = "required"
    OPTIONAL = "optional"
    NONE = "none"


# ---------------------------------------------------------------------------
# Pydantic result models
# ---------------------------------------------------------------------------


class ReliabilityBucket(BaseModel):
    """A single reliability-diagram bin.

    Frozen + ``extra='forbid'``. ``gap`` is ``abs(mean_prob - mean_label)``.

    Attributes:
        lower: inclusive lower probability edge of the bin.
        upper: exclusive upper probability edge of the bin (the final bin
            is inclusive on both ends).
        mean_prob: mean predicted probability of samples falling in the bin.
        mean_label: mean empirical label (event rate) of samples in the bin.
        count: number of samples in the bin.
        gap: ``abs(mean_prob - mean_label)`` — the bin's miscalibration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lower: float
    upper: float
    mean_prob: float
    mean_label: float
    count: int
    gap: float


class CalibrationResult(BaseModel):
    """Result of a calibration pass.

    Frozen + ``extra='forbid'``. Carries the calibrated probabilities, the
    path to the saved calibrator artifact (if any), and the four
    calibration diagnostics (ECE, Brier, log-loss, reliability buckets).

    Attributes:
        method: the :class:`CalibrationMethod` used.
        calibrated_probs: the post-calibration probability for each sample.
        calibration_artifact_path: filesystem path to the saved calibrator
            artifact, or ``None`` when no artifact was persisted.
        ece: Expected Calibration Error (lower is better).
        brier_score: Brier score / mean squared error of probabilities
            (lower is better).
        logloss: negative log-likelihood / cross-entropy (lower is better).
        reliability_buckets: per-bin reliability diagnostics.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: CalibrationMethod
    calibrated_probs: list[float]
    calibration_artifact_path: str | None
    ece: float
    brier_score: float
    logloss: float
    reliability_buckets: list[ReliabilityBucket]


# ---------------------------------------------------------------------------
# Metric helpers (pure-Python, no sklearn required)
# ---------------------------------------------------------------------------

_EPS = 1e-15


def _clip_prob(p: float) -> float:
    """Clamp a probability into ``[_EPS, 1 - _EPS]`` for log safety."""
    if p < _EPS:
        return _EPS
    if p > 1.0 - _EPS:
        return 1.0 - _EPS
    return float(p)


def compute_ece(probs: list[float], labels: list[float], n_bins: int = 10) -> float:
    """Expected Calibration Error.

    Bins predictions into ``n_bins`` equal-width bins by predicted
    probability. For each non-empty bin the contribution is
    ``|mean_prob - mean_label| * (count / total)``. Returns the sum across
    bins — ``0.0`` for an empty input.

    Args:
        probs: predicted probabilities in ``[0, 1]``.
        labels: binary labels in ``{0, 1}``.
        n_bins: number of equal-width bins (>= 1).
    """
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1; got {n_bins}")
    if len(probs) != len(labels):
        raise ValueError(
            f"probs and labels must have equal length; got {len(probs)} and {len(labels)}"
        )
    total = len(probs)
    if total == 0:
        return 0.0
    ece = 0.0
    for bucket in compute_reliability_buckets(probs, labels, n_bins):
        if bucket.count == 0:
            continue
        ece += abs(bucket.mean_prob - bucket.mean_label) * (bucket.count / total)
    return float(ece)


def compute_brier_score(probs: list[float], labels: list[float]) -> float:
    """Brier score — mean squared error of probabilities vs labels.

    ``mean((prob - label) ** 2)``. Returns ``0.0`` for an empty input.
    """
    if len(probs) != len(labels):
        raise ValueError(
            f"probs and labels must have equal length; got {len(probs)} and {len(labels)}"
        )
    if len(probs) == 0:
        return 0.0
    total = len(probs)
    acc = 0.0
    for p, y in zip(probs, labels, strict=False):
        d = float(p) - float(y)
        acc += d * d
    return float(acc / total)


def compute_logloss(probs: list[float], labels: list[float]) -> float:
    """Log-loss / binary cross-entropy.

    ``-mean(label * log(p) + (1 - label) * log(1 - p))`` with probabilities
    clamped to ``[_EPS, 1 - _EPS]`` for numerical safety. Returns ``0.0``
    for an empty input.
    """
    if len(probs) != len(labels):
        raise ValueError(
            f"probs and labels must have equal length; got {len(probs)} and {len(labels)}"
        )
    if len(probs) == 0:
        return 0.0
    total = len(probs)
    acc = 0.0
    for p, y in zip(probs, labels, strict=False):
        pc = _clip_prob(float(p))
        yc = float(y)
        acc += yc * math.log(pc) + (1.0 - yc) * math.log(1.0 - pc)
    return float(-acc / total)


def compute_reliability_buckets(
    probs: list[float],
    labels: list[float],
    n_bins: int = 10,
) -> list[ReliabilityBucket]:
    """Reliability-diagram bins.

    Equal-width bins over ``[0, 1]``. The final bin is inclusive on the
    upper edge so a probability of exactly ``1.0`` is captured. Empty bins
    are still emitted (with ``count == 0`` and ``mean_prob``/``mean_label``
    set to the bin midpoint / ``0.0`` respectively) so the reliability
    diagram has a fixed shape.

    Args:
        probs: predicted probabilities in ``[0, 1]``.
        labels: binary labels in ``{0, 1}``.
        n_bins: number of equal-width bins (>= 1).
    """
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1; got {n_bins}")
    if len(probs) != len(labels):
        raise ValueError(
            f"probs and labels must have equal length; got {len(probs)} and {len(labels)}"
        )
    width = 1.0 / n_bins
    bins: list[ReliabilityBucket] = []
    for i in range(n_bins):
        lower = i * width
        upper = (i + 1) * width
        if i == n_bins - 1:
            in_bin = [
                (float(p), float(y))
                for p, y in zip(probs, labels, strict=False)
                if lower <= float(p) <= upper
            ]
        else:
            in_bin = [
                (float(p), float(y))
                for p, y in zip(probs, labels, strict=False)
                if lower <= float(p) < upper
            ]
        count = len(in_bin)
        if count == 0:
            mean_prob = (lower + upper) / 2.0
            mean_label = 0.0
        else:
            mean_prob = sum(p for p, _ in in_bin) / count
            mean_label = sum(y for _, y in in_bin) / count
        gap = abs(mean_prob - mean_label)
        bins.append(
            ReliabilityBucket(
                lower=lower,
                upper=upper,
                mean_prob=mean_prob,
                mean_label=mean_label,
                count=count,
                gap=gap,
            )
        )
    return bins


# ---------------------------------------------------------------------------
# Calibrator
# ---------------------------------------------------------------------------


class Calibrator:
    """Post-fit probability calibrator (Platt / isotonic / none).

    The calibrator is fit on a held-out calibration set: it learns a
    mapping ``raw_prob -> calibrated_prob`` from ``(raw_probs, labels)``.
    sklearn is imported lazily inside :meth:`fit` so the module remains
    importable in environments without scikit-learn (the pure-Python
    metrics and policy helpers do not require it).

    Args:
        method: the :class:`CalibrationMethod` to apply.
        n_bins: number of bins used only for diagnostics context (the
            calibrator itself does not bin). Defaults to ``10``.
    """

    def __init__(self, method: CalibrationMethod, n_bins: int = 10) -> None:
        if not isinstance(method, CalibrationMethod):
            raise TypeError(f"method must be a CalibrationMethod; got {type(method).__name__}")
        if n_bins < 1:
            raise ValueError(f"n_bins must be >= 1; got {n_bins}")
        self.method = method
        self.n_bins = n_bins
        self._fitted: bool = False
        # The underlying sklearn estimator (LogisticRegression or
        # IsotonicRegression). ``None`` until fit / for the NONE method.
        self._estimator: Any = None

    # -- fit / transform ---------------------------------------------------

    def fit(self, raw_probs: list[float], labels: list[float]) -> Calibrator:
        """Fit the calibrator on ``(raw_probs, labels)``.

        For :attr:`CalibrationMethod.PLATT` a logistic regression is fit
        on the raw probabilities (reshaped to a 2-D feature matrix)
        against the labels. For :attr:`CalibrationMethod.ISOTONIC` an
        isotonic regression is fit. For :attr:`CalibrationMethod.NONE`
        no fitting occurs and :meth:`transform` is a pass-through.

        Returns ``self`` for chaining.
        """
        if len(raw_probs) != len(labels):
            raise ValueError(
                f"raw_probs and labels must have equal length; "
                f"got {len(raw_probs)} and {len(labels)}"
            )
        if len(raw_probs) == 0:
            raise ValueError("cannot fit calibrator on empty inputs")
        if self.method is CalibrationMethod.NONE:
            self._fitted = True
            self._estimator = None
            return self

        # Lazy sklearn import — keeps the module importable without sklearn.
        if self.method is CalibrationMethod.PLATT:
            from sklearn.linear_model import LogisticRegression

            est = LogisticRegression(C=1e10, solver="lbfgs", max_iter=10000)
            X = [[float(p)] for p in raw_probs]
            y = [int(round(float(v))) for v in labels]
            # Need at least two classes for logistic regression.
            if len(set(y)) < 2:
                # Degenerate: all one class. Platt scaling collapses to a
                # constant; fall back to a pass-through so we do not crash.
                self._estimator = None
                self._fitted = True
                return self
            est.fit(X, y)
            self._estimator = est
            self._fitted = True
            return self

        if self.method is CalibrationMethod.ISOTONIC:
            from sklearn.isotonic import IsotonicRegression

            X = [float(p) for p in raw_probs]
            y = [float(v) for v in labels]
            est = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            est.fit(X, y)
            self._estimator = est
            self._fitted = True
            return self

        # Unreachable — enum exhausts the cases above.
        raise ValueError(f"unsupported calibration method: {self.method!r}")

    def transform(self, raw_probs: list[float]) -> list[float]:
        """Apply the fitted calibration to ``raw_probs``.

        For ``NONE`` (or a degenerate single-class fit) the raw
        probabilities are returned unchanged. Raises ``RuntimeError`` if
        :meth:`fit` has not been called.
        """
        if not self._fitted:
            raise RuntimeError("Calibrator.transform called before fit")
        if self._estimator is None:
            return [float(p) for p in raw_probs]
        if self.method is CalibrationMethod.PLATT:
            X = [[float(p)] for p in raw_probs]
            probs = self._estimator.predict_proba(X)[:, 1]
            return [float(p) for p in probs]
        if self.method is CalibrationMethod.ISOTONIC:
            X = [float(p) for p in raw_probs]
            out = self._estimator.transform(X)
            return [float(v) for v in out]
        return [float(p) for p in raw_probs]

    def fit_transform(self, raw_probs: list[float], labels: list[float]) -> list[float]:
        """Convenience: fit then transform on the same data."""
        self.fit(raw_probs, labels)
        return self.transform(raw_probs)

    # -- artifact persistence ---------------------------------------------

    def save_artifact(self, path: str) -> str:
        """Persist the calibrator to ``path`` (pickle).

        Returns the path written. The full calibrator state (method,
        fitted flag, estimator) is pickled so :meth:`load_artifact`
        restores a ready-to-use calibrator.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as fh:
            pickle.dump(
                {
                    "method": self.method,
                    "n_bins": self.n_bins,
                    "fitted": self._fitted,
                    "estimator": self._estimator,
                },
                fh,
            )
        return str(p)

    @classmethod
    def load_artifact(cls, path: str) -> Calibrator:
        """Load a calibrator previously saved with :meth:`save_artifact`.

        Returns a :class:`Calibrator` with ``_fitted`` restored so
        :meth:`transform` can be called immediately.
        """
        with open(path, "rb") as fh:
            state = pickle.load(fh)
        obj = cls(method=state["method"], n_bins=state.get("n_bins", 10))
        obj._fitted = bool(state.get("fitted", False))
        obj._estimator = state.get("estimator")
        return obj


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def calibrate(
    raw_probs: list[float],
    labels: list[float],
    method: CalibrationMethod,
    artifact_path: str | None = None,
    n_bins: int = 10,
) -> CalibrationResult:
    """Calibrate probabilities and emit full diagnostics.

    This is the main entry point. It fits a :class:`Calibrator`, transforms
    the probabilities, computes ECE / Brier / log-loss / reliability
    buckets on the **calibrated** probabilities, optionally saves the
    calibrator artifact, and returns a :class:`CalibrationResult`.

    Args:
        raw_probs: raw model probabilities in ``[0, 1]``.
        labels: binary labels in ``{0, 1}``.
        method: the :class:`CalibrationMethod` to apply.
        artifact_path: if given, the calibrator is persisted here.
        n_bins: number of reliability-diagram bins (>= 1).

    Returns:
        a frozen :class:`CalibrationResult`.
    """
    cal = Calibrator(method=method, n_bins=n_bins)
    calibrated = cal.fit_transform(raw_probs, labels)
    saved_path: str | None = None
    if artifact_path is not None:
        saved_path = cal.save_artifact(artifact_path)
    return CalibrationResult(
        method=method,
        calibrated_probs=calibrated,
        calibration_artifact_path=saved_path,
        ece=compute_ece(calibrated, labels, n_bins=n_bins),
        brier_score=compute_brier_score(calibrated, labels),
        logloss=compute_logloss(calibrated, labels),
        reliability_buckets=compute_reliability_buckets(calibrated, labels, n_bins=n_bins),
    )


# ---------------------------------------------------------------------------
# Promotion-eligibility policy
# ---------------------------------------------------------------------------


def check_calibration_eligibility(
    result: CalibrationResult | None,
    policy: CalibrationPolicy,
) -> bool:
    """Check promotion eligibility under a calibration policy.

    - ``REQUIRED``: eligible only when ``result`` is not ``None``.
    - ``OPTIONAL``: always eligible (with or without a result).
    - ``NONE``: eligible only when ``result`` is ``None`` (calibration
      explicitly disabled — a present result is treated as a policy
      violation).

    Args:
        result: the calibration result for the candidate model, or
            ``None`` if no calibration was performed.
        policy: the :class:`CalibrationPolicy` to enforce.

    Returns:
        ``True`` if the candidate is eligible for promotion under the
        policy.
    """
    if not isinstance(policy, CalibrationPolicy):
        raise TypeError(f"policy must be a CalibrationPolicy; got {type(policy).__name__}")
    if policy is CalibrationPolicy.REQUIRED:
        return result is not None
    if policy is CalibrationPolicy.OPTIONAL:
        return True
    if policy is CalibrationPolicy.NONE:
        return result is None
    # Unreachable for a valid enum.
    return False


__all__ = [
    "CalibrationMethod",
    "CalibrationPolicy",
    "CalibrationResult",
    "Calibrator",
    "ReliabilityBucket",
    "calibrate",
    "check_calibration_eligibility",
    "compute_brier_score",
    "compute_ece",
    "compute_logloss",
    "compute_reliability_buckets",
]
