"""
quant_foundry.moe_expert_router — Mixture-Of-Experts Router (T-13.1).

Combines expert predictions with **regime-aware weighting** and an
**abstention** capability. The router learns, on **out-of-fold** data only,
how much to trust each expert as a function of the current market regime,
then emits a combined prediction (or abstains when confidence is too low).

Design invariants (enforced + tested):
- **Pydantic v2 models are frozen + ``extra='forbid'``** (audit integrity).
- **Fail-closed on in-fold leakage.** :func:`validate_no_infold_leakage`
  raises ``ValueError`` if any expert prediction originates from a fold the
  expert was trained on — the router must never see in-fold predictions.
- **Lazy import of sklearn inside methods** — the module is importable
  without scikit-learn so environments that only need the pure-Python
  helpers (disagreement, max-weight enforcement, leakage validation) are not
  blocked. ``MoERouter.fit`` / ``MoERouter.calibrate`` raise a clear
  ``ImportError`` if sklearn is missing.
- **Max-weight constraint prevents concentration.** No single expert may
  receive more than ``max_weight``; :func:`enforce_max_weight` clips and
  renormalizes.
- **Abstention when confidence below threshold.** When the router's
  confidence in its top expert is below ``abstention_threshold`` it emits
  ``abstain=True`` with zero weights and no combined signal.

File-disjoint from the TASK-1001 :mod:`quant_foundry.moe_router` (rule-based
router). This module is the learned, regime-aware expert combiner.
"""

from __future__ import annotations

import math
import pickle
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EPS = 1e-12
_VALID_ROUTER_TYPES = {"linear", "logistic", "neural"}
_VALID_CALIBRATION_METHODS = {"isotonic", "platt", "none"}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ExpertInput(BaseModel):
    """A single expert's prediction at one timestep.

    Frozen + ``extra='forbid'``. Carries the expert identifier, its point
    prediction, the predicted uncertainty (>= 0), and the out-of-fold
    performance metric (e.g. negative MSE — higher is better).

    Attributes:
        expert_id: non-empty model-family identifier.
        prediction: the expert's point prediction.
        uncertainty: predicted uncertainty (must be >= 0).
        oof_performance: out-of-fold performance metric (higher is better).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    expert_id: str
    prediction: float
    uncertainty: float = 0.0
    oof_performance: float = 0.0

    @field_validator("expert_id")
    @classmethod
    def _expert_id_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or v.strip() == "":
            raise ValueError("expert_id must be a non-empty string")
        return v

    @field_validator("uncertainty")
    @classmethod
    def _uncertainty_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"uncertainty must be >= 0; got {v}")
        return float(v)


class RegimeFeatures(BaseModel):
    """Market-regime feature vector used to condition the router.

    Frozen + ``extra='forbid'``. Carries the four canonical regime signals
    plus an open-ended ``custom_features`` dict for extension.

    Attributes:
        volatility_regime: e.g. VIX level or realized volatility.
        trend_regime: e.g. momentum signal.
        liquidity_regime: market liquidity signal.
        dispersion_regime: cross-sectional dispersion.
        custom_features: additional regime features (name -> value).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    volatility_regime: float = 0.0
    trend_regime: float = 0.0
    liquidity_regime: float = 0.0
    dispersion_regime: float = 0.0
    custom_features: dict[str, float] = Field(default_factory=dict)


class RouterConfig(BaseModel):
    """Configuration for the :class:`MoERouter`.

    Frozen + ``extra='forbid'``.

    Attributes:
        router_type: ``"linear"``, ``"logistic"``, or ``"neural"``.
        n_experts: number of experts (must be >= 2).
        abstention_threshold: minimum confidence to emit a signal, in
            ``[0, 1]``.
        max_weight: maximum weight per expert, in ``(0, 1]``. Must satisfy
            ``max_weight * n_experts >= 1`` (feasibility — the cap must leave
            enough mass to distribute).
        use_regime_features: whether to condition on regime features.
        calibration_method: ``"isotonic"``, ``"platt"``, or ``"none"``.
        seed: RNG seed for reproducible fitting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    router_type: str = "linear"
    n_experts: int = 2
    abstention_threshold: float = 0.3
    max_weight: float = 0.5
    use_regime_features: bool = True
    calibration_method: str = "isotonic"
    seed: int = 42

    @field_validator("router_type")
    @classmethod
    def _router_type_valid(cls, v: str) -> str:
        if v not in _VALID_ROUTER_TYPES:
            raise ValueError(
                f"router_type must be one of {sorted(_VALID_ROUTER_TYPES)}; got {v!r}"
            )
        return v

    @field_validator("n_experts")
    @classmethod
    def _n_experts_min(cls, v: int) -> int:
        if v < 2:
            raise ValueError(f"n_experts must be >= 2; got {v}")
        return int(v)

    @field_validator("abstention_threshold")
    @classmethod
    def _abstention_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(
                f"abstention_threshold must be in [0, 1]; got {v}"
            )
        return float(v)

    @field_validator("max_weight")
    @classmethod
    def _max_weight_range(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError(f"max_weight must be in (0, 1]; got {v}")
        return float(v)

    @field_validator("calibration_method")
    @classmethod
    def _calibration_method_valid(cls, v: str) -> str:
        if v not in _VALID_CALIBRATION_METHODS:
            raise ValueError(
                f"calibration_method must be one of "
                f"{sorted(_VALID_CALIBRATION_METHODS)}; got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _feasibility(self) -> "RouterConfig":
        if self.max_weight * self.n_experts < 1.0:
            raise ValueError(
                f"max_weight * n_experts must be >= 1 (feasibility); "
                f"got {self.max_weight} * {self.n_experts} = "
                f"{self.max_weight * self.n_experts}"
            )
        return self


class RouterOutput(BaseModel):
    """Output of :meth:`MoERouter.route`.

    Frozen + ``extra='forbid'``. When ``abstain`` is ``True`` the
    ``expert_weights`` sum to ``0.0`` and ``combined_prediction`` /
    ``combined_uncertainty`` are ``0.0``; otherwise ``expert_weights`` sum
    to ``1.0``.

    Attributes:
        expert_weights: expert_id -> weight.
        combined_prediction: weighted combination of expert predictions.
        combined_uncertainty: combined uncertainty of the ensemble.
        abstain: whether the router abstained.
        confidence: router confidence in ``[0, 1]``.
        regime_features: the regime features used (if any).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    expert_weights: dict[str, float]
    combined_prediction: float
    combined_uncertainty: float
    abstain: bool
    confidence: float
    regime_features: RegimeFeatures | None = None


class CalibrationReport(BaseModel):
    """Report from :meth:`MoERouter.calibrate`.

    Frozen + ``extra='forbid'``. Carries the calibration method, sample
    count, before/after calibration metrics (ECE, Brier score), and the
    per-bin reliability diagnostics.

    Attributes:
        method: calibration method used.
        n_samples: number of samples calibrated on.
        before_calibration: metrics before calibration (``ece``,
            ``brier_score``).
        after_calibration: metrics after calibration.
        reliability_bins: per-bin ``{lower, upper, mean_prob,
            mean_label, count, gap}`` diagnostics after calibration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str
    n_samples: int
    before_calibration: dict[str, float]
    after_calibration: dict[str, float]
    reliability_bins: list[dict[str, float]]


# ---------------------------------------------------------------------------
# Pure-Python helpers (no sklearn required)
# ---------------------------------------------------------------------------


def compute_model_disagreement(expert_inputs: list[ExpertInput]) -> float:
    """Compute the disagreement (std) among expert predictions.

    Returns the population standard deviation of the expert predictions.
    A single expert yields ``0.0`` (no disagreement).

    Args:
        expert_inputs: list of :class:`ExpertInput` (one per expert).

    Returns:
        The disagreement score (>= 0).
    """
    if len(expert_inputs) == 0:
        return 0.0
    preds = np.array([float(e.prediction) for e in expert_inputs], dtype=float)
    if preds.size == 1:
        return 0.0
    return float(np.std(preds))


def enforce_max_weight(
    weights: dict[str, float], max_weight: float
) -> dict[str, float]:
    """Clip weights to ``max_weight`` and renormalize to sum to 1.0.

    Iteratively clips any weight exceeding ``max_weight``, redistributes the
    excess mass to the remaining (un-clipped) experts proportionally, and
    repeats until no weight exceeds the cap. If every expert is clipped
    (i.e. ``max_weight == 1/n``), returns uniform weights at the cap.

    Args:
        weights: expert_id -> raw weight (need not sum to 1).
        max_weight: maximum allowed weight per expert, in ``(0, 1]``.

    Returns:
        A new dict of expert_id -> clipped, renormalized weights summing
        to ``1.0``.
    """
    if not weights:
        return {}
    if max_weight <= 0 or max_weight > 1.0:
        raise ValueError(f"max_weight must be in (0, 1]; got {max_weight}")
    n = len(weights)
    # If the cap is infeasible (cap * n < 1) fall back to uniform at cap.
    if max_weight * n < 1.0 - _EPS:
        # Cannot satisfy sum=1 with this cap — return uniform capped.
        uniform = min(max_weight, 1.0 / n)
        total = uniform * n
        return {k: float(uniform / total) for k in weights}

    w = {k: max(0.0, float(v)) for k, v in weights.items()}
    total = sum(w.values())
    if total <= 0:
        # All-zero input -> uniform.
        return {k: 1.0 / n for k in weights}
    w = {k: v / total for k, v in w.items()}

    capped: dict[str, float] = {}
    for _ in range(n + 5):  # converges in at most n iterations
        over = {k: v for k, v in w.items() if v > max_weight + _EPS}
        if not over:
            break
        excess = sum(v - max_weight for v in over.values())
        for k in over:
            w[k] = max_weight
        free = {k: v for k, v in w.items() if k not in over and v > 0}
        free_total = sum(free.values())
        if free_total <= _EPS:
            # No free mass to redistribute -> distribute uniformly among free.
            free_keys = [k for k in w if k not in over]
            if free_keys:
                share = excess / len(free_keys)
                for k in free_keys:
                    w[k] += share
            break
        for k in free:
            w[k] += excess * (w[k] / free_total)
    # Final normalization to guard against float drift.
    total = sum(w.values())
    if total > 0:
        w = {k: v / total for k, v in w.items()}
    return {k: float(v) for k, v in w.items()}


def validate_no_infold_leakage(
    expert_inputs: list[ExpertInput],
    fold_id: int,
    expert_fold_ids: dict[str, int],
) -> bool:
    """Validate that no expert prediction is an in-fold (leakage) prediction.

    For each expert, checks that the current ``fold_id`` is **not** among the
    folds that expert was trained on (i.e. the prediction is out-of-fold).

    Args:
        expert_inputs: list of :class:`ExpertInput`.
        fold_id: the fold being evaluated.
        expert_fold_ids: expert_id -> the fold the expert was trained on.

    Returns:
        ``True`` if no leakage is detected.

    Raises:
        ValueError: if any expert's prediction is in-fold (the expert was
            trained on ``fold_id``).
    """
    for ei in expert_inputs:
        trained_fold = expert_fold_ids.get(ei.expert_id)
        if trained_fold is None:
            raise ValueError(
                f"expert {ei.expert_id!r} has no fold assignment in "
                f"expert_fold_ids; cannot verify OOF status"
            )
        if trained_fold == fold_id:
            raise ValueError(
                f"in-fold leakage detected: expert {ei.expert_id!r} was "
                f"trained on fold {fold_id} — router must use OOF predictions"
            )
    return True


# ---------------------------------------------------------------------------
# Calibration metric helpers (pure-Python)
# ---------------------------------------------------------------------------


def _clip_prob(p: float) -> float:
    """Clamp a probability into ``[_EPS, 1 - _EPS]`` for log safety."""
    if p < _EPS:
        return _EPS
    if p > 1.0 - _EPS:
        return 1.0 - _EPS
    return float(p)


def _compute_ece(probs: list[float], labels: list[float], n_bins: int = 10) -> float:
    """Expected Calibration Error (pure-Python)."""
    if len(probs) != len(labels):
        raise ValueError("probs and labels must have equal length")
    total = len(probs)
    if total == 0:
        return 0.0
    ece = 0.0
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        if i == n_bins - 1:
            in_bin = [
                j for j in range(total) if lo <= float(probs[j]) <= hi
            ]
        else:
            in_bin = [
                j for j in range(total) if lo <= float(probs[j]) < hi
            ]
        if not in_bin:
            continue
        mean_prob = sum(float(probs[j]) for j in in_bin) / len(in_bin)
        mean_label = sum(float(labels[j]) for j in in_bin) / len(in_bin)
        ece += abs(mean_prob - mean_label) * (len(in_bin) / total)
    return float(ece)


def _compute_brier(probs: list[float], labels: list[float]) -> float:
    """Brier score (pure-Python)."""
    if len(probs) != len(labels):
        raise ValueError("probs and labels must have equal length")
    if len(probs) == 0:
        return 0.0
    acc = 0.0
    for p, y in zip(probs, labels):
        d = float(p) - float(y)
        acc += d * d
    return float(acc / len(probs))


def _reliability_bins(
    probs: list[float], labels: list[float], n_bins: int = 10
) -> list[dict[str, float]]:
    """Per-bin reliability diagnostics."""
    total = len(probs)
    bins: list[dict[str, float]] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        if i == n_bins - 1:
            in_bin = [
                j for j in range(total) if lo <= float(probs[j]) <= hi
            ]
        else:
            in_bin = [
                j for j in range(total) if lo <= float(probs[j]) < hi
            ]
        count = len(in_bin)
        if count == 0:
            mean_prob = (lo + hi) / 2.0
            mean_label = 0.0
        else:
            mean_prob = sum(float(probs[j]) for j in in_bin) / count
            mean_label = sum(float(labels[j]) for j in in_bin) / count
        bins.append(
            {
                "lower": float(lo),
                "upper": float(hi),
                "mean_prob": float(mean_prob),
                "mean_label": float(mean_label),
                "count": float(count),
                "gap": float(abs(mean_prob - mean_label)),
            }
        )
    return bins


# ---------------------------------------------------------------------------
# The router
# ===========================================================================


class MoERouter:
    """Mixture-Of-Experts router with regime-aware weighting + abstention.

    Learns, on out-of-fold data, how to weight expert predictions as a
    function of the market regime. Supports ``linear`` (logistic regression
    on per-expert features), ``logistic`` (softmax over OOF performance +
    regime interaction), and ``neural`` (fallback to logistic weighting).

    The router abstains when its confidence in the top expert falls below
    ``abstention_threshold``, and enforces a ``max_weight`` cap to prevent
    concentration.
    """

    def __init__(self, config: RouterConfig) -> None:
        """Create a router.

        Args:
            config: :class:`RouterConfig` with the router type, expert
                count, abstention threshold, and max-weight cap.
        """
        self.config = config
        self._fitted: bool = False
        self._expert_ids: list[str] = []
        # Learned parameters.
        self._coef: np.ndarray | None = None
        self._intercept: float = 0.0
        self._base_scores: dict[str, float] = {}
        self._regime_coefs: dict[str, float] = {}
        # Stored calibration data.
        self._calib_predictions: list[float] = []
        self._calib_actuals: list[float] = []
        self._calibrator: Any = None

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _regime_vector(rf: RegimeFeatures | None) -> list[float]:
        """Flatten regime features into a fixed-order vector."""
        if rf is None:
            return [0.0, 0.0, 0.0, 0.0]
        return [
            float(rf.volatility_regime),
            float(rf.trend_regime),
            float(rf.liquidity_regime),
            float(rf.dispersion_regime),
        ]

    def _expert_feature(
        self, ei: ExpertInput, rf: RegimeFeatures | None
    ) -> list[float]:
        """Per-expert feature vector: [prediction, uncertainty, oof_perf, *regime]."""
        feats = [float(ei.prediction), float(ei.uncertainty), float(ei.oof_performance)]
        if self.config.use_regime_features:
            feats.extend(self._regime_vector(rf))
        return feats

    def _expert_scores(
        self, expert_inputs: list[ExpertInput], rf: RegimeFeatures | None
    ) -> dict[str, float]:
        """Compute a raw (pre-softmax) score per expert."""
        if self.config.router_type == "linear" and self._fitted and self._coef is not None:
            scores: dict[str, float] = {}
            for ei in expert_inputs:
                x = np.array(self._expert_feature(ei, rf), dtype=float)
                s = float(np.dot(x, self._coef) + self._intercept)
                scores[ei.expert_id] = s
            return scores
        # logistic / neural / unfitted -> OOF performance + regime interaction.
        scores = {}
        rv = self._regime_vector(rf) if self.config.use_regime_features else [0.0, 0.0, 0.0, 0.0]
        for ei in expert_inputs:
            base = float(ei.oof_performance)
            # Regime interaction: dot of regime vector with stored coefs.
            regime_adj = 0.0
            if self._fitted and self.config.use_regime_features:
                regime_adj = sum(
                    self._regime_coefs.get(name, 0.0) * rv[i]
                    for i, name in enumerate(
                        ["volatility", "trend", "liquidity", "dispersion"]
                    )
                )
            scores[ei.expert_id] = base + regime_adj
        return scores

    @staticmethod
    def _softmax(scores: dict[str, float], temperature: float = 1.0) -> dict[str, float]:
        """Numerically stable softmax over a score dict."""
        if not scores:
            return {}
        vals = np.array(list(scores.values()), dtype=float)
        if temperature <= 0:
            temperature = _EPS
        vals = vals / temperature
        vals = vals - np.max(vals)
        exp = np.exp(vals)
        probs = exp / np.sum(exp)
        return {k: float(p) for k, p in zip(scores.keys(), probs)}

    def _confidence(self, raw_weights: dict[str, float]) -> float:
        """Confidence = max raw weight (concentration of belief)."""
        if not raw_weights:
            return 0.0
        return float(max(raw_weights.values()))

    # -- fit ------------------------------------------------------------

    def fit(
        self,
        expert_inputs: list[list[ExpertInput]],
        regime_features: list[RegimeFeatures],
        targets: list[float],
    ) -> None:
        """Train the router on out-of-fold data.

        The router must **never** be fit on in-fold predictions — callers are
        expected to supply OOF expert predictions only (use
        :func:`validate_no_infold_leakage` upstream).

        For ``linear``: fits a logistic regression on per-expert features
        (prediction, uncertainty, OOF performance, regime features) against a
        binary label indicating whether the expert's prediction was close to
        the target. The resulting coefficients map features -> "expert is
        good" probability, which is then softmaxed into weights.

        For ``logistic`` / ``neural``: stores per-expert average OOF
        performance and learns simple regime-interaction coefficients by
        least squares.

        Args:
            expert_inputs: per-timestep list of :class:`ExpertInput`.
            regime_features: per-timestep :class:`RegimeFeatures`.
            targets: per-timestep target value.

        Raises:
            ValueError: if inputs are inconsistent or too few.
            ImportError: if sklearn is required but not installed.
        """
        n = len(expert_inputs)
        if n == 0:
            raise ValueError("fit requires at least one timestep")
        if len(regime_features) != n or len(targets) != n:
            raise ValueError(
                f"expert_inputs ({n}), regime_features ({len(regime_features)}), "
                f"and targets ({len(targets)}) must have equal length"
            )
        # Determine expert id order from the first timestep.
        first = expert_inputs[0]
        self._expert_ids = [ei.expert_id for ei in first]
        if len(self._expert_ids) != self.config.n_experts:
            raise ValueError(
                f"expected {self.config.n_experts} experts, "
                f"got {len(self._expert_ids)}"
            )

        if self.config.router_type == "linear":
            self._fit_linear(expert_inputs, regime_features, targets)
        else:
            self._fit_logistic(expert_inputs, regime_features, targets)

        # Store calibration data (combined predictions vs targets).
        self._calib_predictions = []
        self._calib_actuals = []
        for eis, rf, tgt in zip(expert_inputs, regime_features, targets):
            out = self.route(eis, rf)
            if not out.abstain:
                self._calib_predictions.append(out.combined_prediction)
                self._calib_actuals.append(float(tgt))
        self._fitted = True

    def _fit_linear(
        self,
        expert_inputs: list[list[ExpertInput]],
        regime_features: list[RegimeFeatures],
        targets: list[float],
    ) -> None:
        """Fit logistic regression on per-expert features."""
        try:
            from sklearn.linear_model import LogisticRegression
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "scikit-learn is required for router_type='linear'; "
                "install scikit-learn or use router_type='logistic'"
            ) from exc

        X_rows: list[list[float]] = []
        y_rows: list[int] = []
        # Label: 1 if this expert's error is below the median error at that
        # timestep (i.e. the expert is "good" for this regime).
        for eis, rf, tgt in zip(expert_inputs, regime_features, targets):
            errors = [abs(float(ei.prediction) - float(tgt)) for ei in eis]
            med = float(np.median(errors)) if errors else 0.0
            for ei, err in zip(eis, errors):
                X_rows.append(self._expert_feature(ei, rf))
                y_rows.append(1 if err <= med + _EPS else 0)

        X = np.array(X_rows, dtype=float)
        y = np.array(y_rows, dtype=int)
        if len(np.unique(y)) < 2:
            # Degenerate: all-good or all-bad -> fall back to OOF performance.
            self._coef = np.zeros(X.shape[1], dtype=float)
            self._coef[2] = 1.0  # oof_performance index
            self._intercept = 0.0
            return
        model = LogisticRegression(
            max_iter=1000, random_state=self.config.seed
        )
        model.fit(X, y)
        self._coef = model.coef_[0].astype(float)
        self._intercept = float(model.intercept_[0])

    def _fit_logistic(
        self,
        expert_inputs: list[list[ExpertInput]],
        regime_features: list[RegimeFeatures],
        targets: list[float],
    ) -> None:
        """Fit softmax weighting with regime interaction (least squares)."""
        # Base score = mean OOF performance per expert.
        perf_sums: dict[str, float] = {eid: 0.0 for eid in self._expert_ids}
        counts: dict[str, int] = {eid: 0 for eid in self._expert_ids}
        for eis in expert_inputs:
            for ei in eis:
                perf_sums[ei.expert_id] += float(ei.oof_performance)
                counts[ei.expert_id] += 1
        self._base_scores = {
            k: (perf_sums[k] / counts[k] if counts[k] else 0.0)
            for k in self._expert_ids
        }
        # Regime coefs: simple least-squares of regime vector against
        # per-expert "goodness" (inverse rank of error).
        if self.config.use_regime_features and len(expert_inputs) > 1:
            reg_names = ["volatility", "trend", "liquidity", "dispersion"]
            R: list[list[float]] = []
            g: list[float] = []
            for eis, rf, tgt in zip(expert_inputs, regime_features, targets):
                rv = self._regime_vector(rf)
                errors = [abs(float(ei.prediction) - float(tgt)) for ei in eis]
                order = np.argsort(errors)
                # goodness: 1.0 for best expert, 0.0 for worst.
                rank = np.empty(len(eis))
                rank[order] = np.linspace(1.0, 0.0, len(eis))
                for ei_val, gi in zip(eis, rank):
                    R.append(rv)
                    g.append(float(gi))
            R_arr = np.array(R, dtype=float)
            g_arr = np.array(g, dtype=float)
            if R_arr.shape[0] >= 4:
                coef, *_ = np.linalg.lstsq(R_arr, g_arr, rcond=None)
                self._regime_coefs = {
                    name: float(c) for name, c in zip(reg_names, coef)
                }
            else:
                self._regime_coefs = {name: 0.0 for name in reg_names}
        else:
            reg_names = ["volatility", "trend", "liquidity", "dispersion"]
            self._regime_coefs = {name: 0.0 for name in reg_names}

    # -- route ----------------------------------------------------------

    def route(
        self,
        expert_inputs: list[ExpertInput],
        regime_features: RegimeFeatures | None = None,
    ) -> RouterOutput:
        """Route expert predictions to a combined output (or abstain).

        Computes expert weights from the learned router (or OOF performance
        if unfitted), enforces the ``max_weight`` cap, combines predictions,
        and abstains if confidence < ``abstention_threshold``.

        Args:
            expert_inputs: list of :class:`ExpertInput` (one per expert).
            regime_features: current regime features (optional).

        Returns:
            :class:`RouterOutput`.
        """
        if len(expert_inputs) == 0:
            return RouterOutput(
                expert_weights={},
                combined_prediction=0.0,
                combined_uncertainty=0.0,
                abstain=True,
                confidence=0.0,
                regime_features=regime_features,
            )
        if len(expert_inputs) != self.config.n_experts:
            raise ValueError(
                f"expected {self.config.n_experts} experts, "
                f"got {len(expert_inputs)}"
            )

        scores = self._expert_scores(expert_inputs, regime_features)
        raw_weights = self._softmax(scores)
        confidence = self._confidence(raw_weights)

        # Abstention: confidence below threshold.
        if confidence < self.config.abstention_threshold:
            return RouterOutput(
                expert_weights={k: 0.0 for k in raw_weights},
                combined_prediction=0.0,
                combined_uncertainty=0.0,
                abstain=True,
                confidence=float(confidence),
                regime_features=regime_features,
            )

        # Enforce max-weight cap.
        weights = enforce_max_weight(raw_weights, self.config.max_weight)

        # Combine predictions.
        combined_pred = sum(
            weights.get(ei.expert_id, 0.0) * float(ei.prediction)
            for ei in expert_inputs
        )
        # Combined uncertainty: weighted RMS (accounts for diversification).
        combined_var = sum(
            (weights.get(ei.expert_id, 0.0) ** 2) * (float(ei.uncertainty) ** 2)
            for ei in expert_inputs
        )
        combined_unc = float(math.sqrt(combined_var))

        return RouterOutput(
            expert_weights=weights,
            combined_prediction=float(combined_pred),
            combined_uncertainty=float(combined_unc),
            abstain=False,
            confidence=float(confidence),
            regime_features=regime_features,
        )

    # -- calibrate ------------------------------------------------------

    def calibrate(
        self, predictions: list[float], actuals: list[float]
    ) -> CalibrationReport:
        """Calibrate the router's combined predictions against actuals.

        Computes ECE and Brier score before and after applying the configured
        calibration method (isotonic / platt / none). For ``none`` the
        before/after metrics are identical.

        Args:
            predictions: raw combined predictions in ``[0, 1]``.
            actuals: binary labels in ``{0, 1}``.

        Returns:
            :class:`CalibrationReport`.

        Raises:
            ValueError: if inputs are inconsistent.
            ImportError: if sklearn is required but not installed.
        """
        if len(predictions) != len(actuals):
            raise ValueError(
                f"predictions ({len(predictions)}) and actuals "
                f"({len(actuals)}) must have equal length"
            )
        if len(predictions) == 0:
            raise ValueError("calibrate requires at least one sample")

        preds = [float(p) for p in predictions]
        acts = [float(a) for a in actuals]

        before_ece = _compute_ece(preds, acts)
        before_brier = _compute_brier(preds, acts)

        method = self.config.calibration_method
        if method == "none":
            calibrated = list(preds)
            self._calibrator = None
        else:
            try:
                from sklearn.isotonic import IsotonicRegression
                from sklearn.linear_model import LogisticRegression
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "scikit-learn is required for calibration_method != 'none'"
                ) from exc

            X = np.array(preds, dtype=float).reshape(-1, 1)
            y = np.array(acts, dtype=float)
            if method == "isotonic":
                cal = IsotonicRegression(out_of_bounds="clip")
                cal.fit(X.ravel(), y)
                calibrated = [float(v) for v in cal.predict(X.ravel())]
                self._calibrator = cal
            elif method == "platt":
                cal = LogisticRegression(max_iter=1000)
                if len(np.unique(y)) < 2:
                    calibrated = list(preds)
                    self._calibrator = None
                else:
                    cal.fit(X, y)
                    calibrated = [
                        float(v[1]) for v in cal.predict_proba(X)
                    ]
                    self._calibrator = cal
            else:  # pragma: no cover
                calibrated = list(preds)

        after_ece = _compute_ece(calibrated, acts)
        after_brier = _compute_brier(calibrated, acts)
        rel_bins = _reliability_bins(calibrated, acts)

        return CalibrationReport(
            method=method,
            n_samples=len(preds),
            before_calibration={
                "ece": float(before_ece),
                "brier_score": float(before_brier),
            },
            after_calibration={
                "ece": float(after_ece),
                "brier_score": float(after_brier),
            },
            reliability_bins=rel_bins,
        )

    # -- persistence ----------------------------------------------------

    def save(self, path: str) -> None:
        """Pickle the router state to ``path``.

        Args:
            path: filesystem path (created if needed).
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "config": self.config.model_dump(),
            "fitted": self._fitted,
            "expert_ids": self._expert_ids,
            "coef": None if self._coef is None else self._coef.tolist(),
            "intercept": self._intercept,
            "base_scores": self._base_scores,
            "regime_coefs": self._regime_coefs,
            "calib_predictions": self._calib_predictions,
            "calib_actuals": self._calib_actuals,
        }
        with open(p, "wb") as fh:
            pickle.dump(state, fh)

    def load(self, path: str) -> None:
        """Restore the router state from ``path``.

        Args:
            path: filesystem path previously written by :meth:`save`.
        """
        with open(path, "rb") as fh:
            state = pickle.load(fh)
        self.config = RouterConfig(**state["config"])
        self._fitted = state["fitted"]
        self._expert_ids = state["expert_ids"]
        self._coef = (
            None if state["coef"] is None else np.array(state["coef"], dtype=float)
        )
        self._intercept = state["intercept"]
        self._base_scores = state["base_scores"]
        self._regime_coefs = state["regime_coefs"]
        self._calib_predictions = state["calib_predictions"]
        self._calib_actuals = state["calib_actuals"]
