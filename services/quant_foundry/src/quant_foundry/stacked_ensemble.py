"""quant_foundry.stacked_ensemble — Stacked Ensemble meta-learner (T-8.5).

This module implements the **stacked ensemble** (a.k.a. *stacking* /
*super-learner*) pattern: several *base* models are trained independently
on walk-forward folds, their **out-of-fold (OOF) predictions** are
collected as tamper-evident artifacts (see
:mod:`quant_foundry.oof_artifacts`), and a **meta-learner** is then
trained on those OOF predictions (never on the raw features) to produce
a final ensemble model.

The flow is:

1. Base models (LightGBM, CatBoost, XGBoost, ...) are trained per fold
   and their OOF predictions are written with
   :func:`quant_foundry.oof_artifacts.write_oof_artifact`.
2. A :class:`StackedEnsemble` is constructed with one
   :class:`BaseModelSpec` per base model (recording the artifact paths
   and hashes for audit).
3. :meth:`StackedEnsemble.train_meta_learner` merges the OOF artifacts
   via :func:`merge_oof_artifacts`, builds a feature matrix whose
   columns are the base models' OOF predictions (one column per model
   family, sorted deterministically), trains a meta-learner
   (LightGBM or logistic regression) on that matrix, computes ensemble
   metrics, a per-base-model contribution report, and an optional
   calibration report, then returns an :class:`EnsembleResult`.
4. :meth:`StackedEnsemble.predict` aligns base-model predictions by
   model family (sorted deterministically) and runs them through the
   meta-learner to produce ensemble predictions.
5. :meth:`StackedEnsemble.save_manifest` /
   :meth:`StackedEnsemble.load_manifest` persist / restore the
   :class:`EnsembleManifest` (a frozen, tamper-evident record of every
   base artifact hash + the meta-learner artifact hash + a deterministic
   ``ensemble_hash``).

Design invariants (enforced + tested):

- **All Pydantic models are ``frozen=True`` and ``extra='forbid'``**
  (audit integrity — an ensemble manifest is an immutable record).
- **The meta-learner only ever sees OOF predictions**, never raw
  features. The feature matrix passed to the meta-learner is built
  exclusively from :func:`merge_oof_artifacts` output.
- **Deterministic ordering.** Base models are always processed in
  sorted-by-``model_family`` order so that two identical ensembles
  produce identical feature matrices, identical meta-learners (given
  the same seed), and identical ``ensemble_hash`` values.
- **Fail-closed.** Missing base artifacts, fewer than two base models,
  duplicate model families, and hash mismatches all raise immediately.
- **Lazy ML imports.** ``lightgbm`` and ``scikit-learn`` are imported
  inside methods so the module remains importable in environments
  without those backends.

File-disjoint from ``real_trainer.py`` — this module never modifies the
trainer. It only *consumes* the OOF artifacts that trainers produce.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.calibration import (
    CalibrationMethod,
    CalibrationPolicy,
    CalibrationResult,
    calibrate,
    check_calibration_eligibility,
)
from quant_foundry.oof_artifacts import (
    OOFArtifact,
    merge_oof_artifacts,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Supported meta-learner families.
SUPPORTED_META_LEARNER_FAMILIES: frozenset[str] = frozenset(
    {
        "lightgbm",
        "logistic_regression",
    }
)

#: Supported calibration policy strings (mapped to
#: :class:`~quant_foundry.calibration.CalibrationPolicy`).
SUPPORTED_CALIBRATION_POLICIES: frozenset[str] = frozenset({"required", "optional", "none"})

#: Default LightGBM params for the meta-learner (small, fast, regularized
#: to avoid over-fitting the typically narrow OOF feature matrix).
_DEFAULT_META_LGBM_PARAMS: dict[str, Any] = {
    "objective": "regression",
    "metric": "rmse",
    "n_estimators": 50,
    "learning_rate": 0.1,
    "num_leaves": 8,
    "min_child_samples": 5,
    "verbose": -1,
    "seed": 42,
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BaseModelSpec(BaseModel):
    """Specification of a single base model in a stacked ensemble.

    Records the model family, the trained-model artifact path + hash, and
    the OOF-predictions artifact path + hash. All four paths/hashes are
    required so the ensemble manifest is a complete audit trail.

    Frozen + ``extra='forbid'`` (audit integrity).

    Attributes:
        model_family: the model family (e.g. ``"lightgbm"``,
            ``"catboost"``, ``"xgboost"``).
        artifact_path: filesystem path to the trained base-model
            artifact.
        artifact_hash: hash of the base-model artifact.
        oof_artifact_path: filesystem path to the OOF predictions
            artifact.
        oof_artifact_hash: hash of the OOF predictions artifact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    model_family: str
    artifact_path: str
    artifact_hash: str
    oof_artifact_path: str
    oof_artifact_hash: str

    @field_validator(
        "model_family",
        "artifact_path",
        "artifact_hash",
        "oof_artifact_path",
        "oof_artifact_hash",
    )
    @classmethod
    def _nonempty_str(cls, v: str, info: Any) -> str:
        """Reject empty / whitespace-only strings."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return v


class EnsembleManifest(BaseModel):
    """Manifest describing a complete stacked ensemble.

    Frozen + ``extra='forbid'``. Carries the list of base-model specs,
    the meta-learner family + artifact path/hash, a deterministic
    ``ensemble_hash`` over the full manifest content, and a creation
    timestamp.

    Validators:
        - At least 2 base models.
        - No duplicate model families among the base models.
        - ``ensemble_hash`` is a 64-char hex SHA-256.

    Attributes:
        base_models: list of :class:`BaseModelSpec` (>= 2, no duplicate
            families).
        meta_learner_family: the meta-learner family (e.g.
            ``"lightgbm"``, ``"logistic_regression"``).
        meta_learner_artifact_path: path to the saved meta-learner
            artifact.
        meta_learner_artifact_hash: hash of the meta-learner artifact.
        ensemble_hash: deterministic SHA-256 of the full manifest
            content (see :func:`compute_ensemble_hash`).
        created_at: ISO-format timestamp of manifest creation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    base_models: list[BaseModelSpec] = Field(min_length=2)
    meta_learner_family: str
    meta_learner_artifact_path: str
    meta_learner_artifact_hash: str
    ensemble_hash: str
    created_at: str

    @field_validator(
        "meta_learner_family",
        "meta_learner_artifact_path",
        "meta_learner_artifact_hash",
        "created_at",
    )
    @classmethod
    def _nonempty_str(cls, v: str, info: Any) -> str:
        """Reject empty / whitespace-only strings."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return v

    @field_validator("ensemble_hash")
    @classmethod
    def _hash_hex64(cls, v: str) -> str:
        """ensemble_hash must be a 64-char lowercase hex string."""
        if not isinstance(v, str) or len(v) != 64:
            raise ValueError(
                f"ensemble_hash must be a 64-character hex string; "
                f"got length {len(v) if isinstance(v, str) else type(v).__name__}"
            )
        try:
            int(v, 16)
        except ValueError as exc:
            raise ValueError("ensemble_hash must be a valid hexadecimal string") from exc
        return v.lower()

    @model_validator(mode="after")
    def _check_min_base_models(self) -> EnsembleManifest:
        """Require at least 2 base models."""
        if len(self.base_models) < 2:
            raise ValueError(f"at least 2 base models are required; got {len(self.base_models)}")
        return self

    @model_validator(mode="after")
    def _check_no_duplicate_families(self) -> EnsembleManifest:
        """Reject duplicate model families among base models."""
        families = [bm.model_family for bm in self.base_models]
        seen: set[str] = set()
        dupes: list[str] = []
        for fam in families:
            if fam in seen:
                dupes.append(fam)
            seen.add(fam)
        if dupes:
            raise ValueError(
                f"duplicate model families in base_models: "
                f"{sorted(set(dupes))!r} — each base model must have a "
                "unique model family"
            )
        return self


class ContributionReport(BaseModel):
    """Per-base-model contribution to the ensemble.

    Frozen + ``extra='forbid'``. The ``contribution_score`` is either a
    permutation-importance score or a normalized coefficient magnitude
    (see :func:`compute_contributions`). Reports are sorted by
    ``contribution_score`` descending and assigned a 1-based ``rank``.

    Attributes:
        model_family: the base model family this report refers to.
        contribution_score: the contribution score (>= 0, higher means
            more important).
        rank: 1-based rank (1 = most important).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    model_family: str
    contribution_score: float
    rank: int

    @field_validator("model_family")
    @classmethod
    def _nonempty_str(cls, v: str, info: Any) -> str:
        """Reject empty / whitespace-only strings."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return v

    @field_validator("contribution_score")
    @classmethod
    def _score_nonnegative(cls, v: float) -> float:
        """Contribution scores must be non-negative."""
        if v < 0:
            raise ValueError(f"contribution_score must be >= 0; got {v}")
        return float(v)

    @field_validator("rank")
    @classmethod
    def _rank_positive(cls, v: int) -> int:
        """Ranks are 1-based and must be >= 1."""
        if v < 1:
            raise ValueError(f"rank must be >= 1; got {v}")
        return v


class EnsembleCalibrationReport(BaseModel):
    """Calibration report for the ensemble meta-learner.

    Frozen + ``extra='forbid'``. Wraps the optional
    :class:`~quant_foundry.calibration.CalibrationResult` together with
    the eligibility decision under the configured policy.

    Attributes:
        calibration_result: the calibration result, or ``None`` when no
            calibration was performed.
        is_eligible: whether the ensemble is eligible for promotion
            under the configured policy.
        policy: the calibration policy string (``"required"``,
            ``"optional"``, or ``"none"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    calibration_result: CalibrationResult | None = None
    is_eligible: bool
    policy: str

    @field_validator("policy")
    @classmethod
    def _policy_supported(cls, v: str) -> str:
        """Policy must be one of the supported values."""
        if v not in SUPPORTED_CALIBRATION_POLICIES:
            raise ValueError(
                f"unsupported calibration policy {v!r}; "
                f"supported: {sorted(SUPPORTED_CALIBRATION_POLICIES)}"
            )
        return v


class EnsembleResult(BaseModel):
    """Result of training a stacked ensemble meta-learner.

    Frozen + ``extra='forbid'``. Bundles the manifest, the meta-learner's
    predictions on the OOF data, the per-base-model contribution reports,
    the calibration report, and the ensemble-level metrics.

    Attributes:
        manifest: the :class:`EnsembleManifest`.
        meta_learner_predictions: the meta-learner's predictions on the
            OOF feature matrix (one per row, aligned with the merged
            OOF row order).
        contributions: per-base-model :class:`ContributionReport`
            list, sorted by contribution descending.
        calibration_report: the :class:`EnsembleCalibrationReport`.
        ensemble_metrics: dict of metric name -> value (e.g. ``"mse"``,
            ``"rmse"``, ``"correlation"``, ``"mae"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    manifest: EnsembleManifest
    meta_learner_predictions: list[float]
    contributions: list[ContributionReport]
    calibration_report: EnsembleCalibrationReport
    ensemble_metrics: dict[str, float]

    @field_validator("meta_learner_predictions")
    @classmethod
    def _preds_finite(cls, v: list[float]) -> list[float]:
        """Predictions must be finite floats."""
        for i, p in enumerate(v):
            if not isinstance(p, (int, float)):
                raise ValueError(
                    f"meta_learner_predictions[{i}] must be a number; got {type(p).__name__}"
                )
            if not math.isfinite(float(p)):
                raise ValueError(f"meta_learner_predictions[{i}] must be finite; got {p}")
        return [float(p) for p in v]

    @model_validator(mode="after")
    def _check_contributions_match_base_models(self) -> EnsembleResult:
        """Contribution reports must cover exactly the base model families."""
        base_families = {bm.model_family for bm in self.manifest.base_models}
        contrib_families = {c.model_family for c in self.contributions}
        if base_families != contrib_families:
            missing = base_families - contrib_families
            extra = contrib_families - base_families
            raise ValueError(
                f"contributions must cover exactly the base model "
                f"families; missing={sorted(missing)!r}, "
                f"extra={sorted(extra)!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_contribution_ranks(self) -> EnsembleResult:
        """Contribution ranks must be a contiguous 1..N sequence."""
        if not self.contributions:
            return self
        ranks = sorted(c.rank for c in self.contributions)
        expected = list(range(1, len(self.contributions) + 1))
        if ranks != expected:
            raise ValueError(f"contribution ranks must be a contiguous 1..N sequence; got {ranks}")
        return self


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def compute_ensemble_hash(manifest_data: dict) -> str:
    """Compute a deterministic SHA-256 hash over a manifest dict.

    The manifest data is serialized to canonical JSON (sorted keys,
    compact separators) before hashing. The ``ensemble_hash`` field
    itself (if present) is excluded from the hash input so the hash is
    self-consistent (the hash is computed over the manifest content
    *without* the hash itself, then stored).

    Args:
        manifest_data: the manifest content as a dict. A shallow copy is
            made so the caller's dict is not mutated. The
            ``"ensemble_hash"`` key is removed from the copy before
            hashing.

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    payload = dict(manifest_data)
    payload.pop("ensemble_hash", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Contribution computation
# ---------------------------------------------------------------------------


def compute_contributions(
    meta_learner: Any,
    feature_names: list[str],
    X: Any,
    y: Any,
) -> list[ContributionReport]:
    """Compute per-feature contribution scores for the meta-learner.

    Uses **coefficient-based** contributions when the meta-learner exposes
    a ``coef_`` attribute (e.g. logistic regression): the absolute value
    of each coefficient, normalized to sum to 1. Otherwise uses
    **permutation importance**: for each feature, shuffle that column and
    measure the increase in mean-squared-error; the scores are normalized
    to sum to 1 and floored at 0.

    The returned list is sorted by ``contribution_score`` descending and
    each report carries a 1-based ``rank``.

    Args:
        meta_learner: the fitted meta-learner model object.
        feature_names: the feature (base-model-family) names, one per
            column of ``X``.
        X: the feature matrix (OOF predictions). A numpy array or
            array-like with shape ``(n_rows, n_features)``.
        y: the label vector aligned with ``X``.

    Returns:
        A list of :class:`ContributionReport` sorted by contribution
        descending, with 1-based ranks.

    Raises:
        ValueError: if ``feature_names`` is empty or does not match the
            number of columns in ``X``.
    """
    import numpy as np

    if not feature_names:
        raise ValueError("feature_names must be non-empty")
    X_arr = np.asarray(X, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    if X_arr.ndim != 2:
        raise ValueError(f"X must be 2-D; got {X_arr.ndim}D")
    if X_arr.shape[1] != len(feature_names):
        raise ValueError(
            f"feature_names length {len(feature_names)} does not match X columns {X_arr.shape[1]}"
        )
    if X_arr.shape[0] != y_arr.shape[0]:
        raise ValueError(f"X rows {X_arr.shape[0]} does not match y rows {y_arr.shape[0]}")

    # --- Coefficient-based contributions (logistic regression etc.) -----
    if hasattr(meta_learner, "coef_"):
        coef = np.asarray(meta_learner.coef_, dtype=np.float64).ravel()
        if coef.shape[0] == len(feature_names):
            raw = np.abs(coef)
            total = float(raw.sum())
            if total > 0:
                scores = raw / total
            else:
                scores = np.full(len(feature_names), 1.0 / len(feature_names))
        else:
            # coef_ shape mismatch — fall back to permutation.
            scores = _permutation_importance(meta_learner, X_arr, y_arr)
    elif hasattr(meta_learner, "feature_importances_"):
        # sklearn-style feature importances (e.g. some LightGBM sklearn API).
        imp = np.asarray(meta_learner.feature_importances_, dtype=np.float64)
        total = float(imp.sum())
        if total > 0:
            scores = imp / total
        else:
            scores = np.full(len(feature_names), 1.0 / len(feature_names))
    else:
        # Fall back to permutation importance for LightGBM Booster etc.
        scores = _permutation_importance(meta_learner, X_arr, y_arr)

    # Build reports sorted by score descending.
    indexed = list(zip(feature_names, scores, strict=False))
    indexed.sort(key=lambda kv: (-float(kv[1]), kv[0]))
    reports: list[ContributionReport] = []
    for rank, (name, score) in enumerate(indexed, start=1):
        reports.append(
            ContributionReport(
                model_family=name,
                contribution_score=float(max(0.0, score)),
                rank=rank,
            )
        )
    return reports


def _permutation_importance(
    meta_learner: Any,
    X: Any,
    y: Any,
    *,
    n_repeats: int = 5,
    seed: int = 42,
) -> Any:
    """Compute permutation importance scores (MSE increase per feature).

    For each feature column, shuffle that column ``n_repeats`` times and
    measure the increase in mean-squared-error relative to the baseline
    (unshuffled) MSE. Returns a 1-D numpy array of average importance
    scores (one per column), floored at 0.

    Args:
        meta_learner: the fitted meta-learner. Must support
            ``predict(X)``.
        X: 2-D feature matrix (numpy array).
        y: 1-D label vector (numpy array).
        n_repeats: number of shuffle repeats per feature.
        seed: RNG seed for reproducibility.

    Returns:
        1-D numpy array of non-negative importance scores.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    baseline_pred = _safe_predict(meta_learner, X)
    baseline_mse = float(np.mean((baseline_pred - y) ** 2))

    n_features = X.shape[1]
    scores = np.zeros(n_features, dtype=np.float64)
    for col in range(n_features):
        increases: list[float] = []
        for _ in range(n_repeats):
            X_perm = X.copy()
            rng.shuffle(X_perm[:, col])
            perm_pred = _safe_predict(meta_learner, X_perm)
            perm_mse = float(np.mean((perm_pred - y) ** 2))
            increases.append(perm_mse - baseline_mse)
        scores[col] = max(0.0, float(np.mean(increases)))

    # Normalize to sum to 1 (guard against all-zero).
    total = float(scores.sum())
    if total > 0:
        scores = scores / total
    else:
        scores = np.full(n_features, 1.0 / n_features)
    return scores


def _safe_predict(meta_learner: Any, X: Any) -> Any:
    """Call ``predict`` on the meta-learner, handling different APIs.

    LightGBM ``Booster.predict`` expects a numpy array / Dataset.
    sklearn estimators expose ``predict``. Both return a 1-D array for
    regression.

    A benign ``UserWarning`` from sklearn ("X does not have valid feature
    names") is suppressed — the meta-learner is always called with plain
    numpy arrays (OOF predictions), and the warning is a false positive
    in the stacking context.
    """
    import warnings

    import numpy as np

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="X does not have valid feature names",
            category=UserWarning,
        )
        raw = meta_learner.predict(X)
    arr = np.asarray(raw, dtype=np.float64).ravel()
    return arr


# ---------------------------------------------------------------------------
# StackedEnsemble
# ---------------------------------------------------------------------------


class StackedEnsemble:
    """Stacked ensemble meta-learner trainer.

    Orchestrates the stacking flow: validate base-model specs, merge OOF
    artifacts, train a meta-learner on the OOF predictions, compute
    ensemble metrics + contributions, apply calibration per the policy,
    and emit an :class:`EnsembleResult` + :class:`EnsembleManifest`.

    ML dependencies (``lightgbm``, ``scikit-learn``, ``numpy``) are
    imported lazily inside :meth:`train_meta_learner` /
    :meth:`predict` so the module remains importable without them.

    Args:
        base_specs: list of :class:`BaseModelSpec` (>= 2, no duplicate
            families).
        meta_learner_family: the meta-learner family — ``"lightgbm"``
            or ``"logistic_regression"``.
        calibration_policy: calibration policy — ``"required"``,
            ``"optional"``, or ``"none"``.

    Raises:
        ValueError: if ``base_specs`` has fewer than 2 entries,
            duplicate model families, an unsupported meta-learner family,
            or an unsupported calibration policy.
    """

    def __init__(
        self,
        base_specs: list[BaseModelSpec],
        meta_learner_family: str = "lightgbm",
        calibration_policy: str = "optional",
    ) -> None:
        # --- Validate base specs ----------------------------------------
        if not isinstance(base_specs, list):
            raise TypeError(f"base_specs must be a list; got {type(base_specs).__name__}")
        if len(base_specs) < 2:
            raise ValueError(f"at least 2 base models are required; got {len(base_specs)}")
        for i, spec in enumerate(base_specs):
            if not isinstance(spec, BaseModelSpec):
                raise TypeError(
                    f"base_specs[{i}] must be a BaseModelSpec; got {type(spec).__name__}"
                )
        families = [s.model_family for s in base_specs]
        seen: set[str] = set()
        dupes: list[str] = []
        for fam in families:
            if fam in seen:
                dupes.append(fam)
            seen.add(fam)
        if dupes:
            raise ValueError(f"duplicate model families in base_specs: {sorted(set(dupes))!r}")

        # --- Validate meta-learner family -------------------------------
        if meta_learner_family not in SUPPORTED_META_LEARNER_FAMILIES:
            raise ValueError(
                f"unsupported meta_learner_family {meta_learner_family!r}; "
                f"supported: {sorted(SUPPORTED_META_LEARNER_FAMILIES)}"
            )

        # --- Validate calibration policy --------------------------------
        if calibration_policy not in SUPPORTED_CALIBRATION_POLICIES:
            raise ValueError(
                f"unsupported calibration_policy {calibration_policy!r}; "
                f"supported: {sorted(SUPPORTED_CALIBRATION_POLICIES)}"
            )

        # Store in deterministic (sorted-by-family) order.
        self._base_specs: list[BaseModelSpec] = sorted(base_specs, key=lambda s: s.model_family)
        self.meta_learner_family: str = meta_learner_family
        self.calibration_policy: str = calibration_policy

        # Populated by train_meta_learner.
        self._meta_learner: Any = None
        self._manifest: EnsembleManifest | None = None
        self._feature_names: list[str] = [s.model_family for s in self._base_specs]

    # -- public properties ------------------------------------------------

    @property
    def base_specs(self) -> list[BaseModelSpec]:
        """The base-model specs in deterministic (sorted) order."""
        return list(self._base_specs)

    @property
    def feature_names(self) -> list[str]:
        """The meta-learner feature names (= base model families, sorted)."""
        return list(self._feature_names)

    # -- artifact existence checks ---------------------------------------

    def _validate_base_artifacts_exist(self) -> None:
        """Fail-closed: every base artifact + OOF artifact must exist.

        Raises:
            FileNotFoundError: if any artifact path does not exist.
            ValueError: if any artifact file is empty.
        """
        for spec in self._base_specs:
            for path, label in (
                (spec.artifact_path, "artifact_path"),
                (spec.oof_artifact_path, "oof_artifact_path"),
            ):
                if not os.path.isfile(path):
                    raise FileNotFoundError(
                        f"base model {spec.model_family!r} {label} does not exist: {path!r}"
                    )
                if os.path.getsize(path) == 0:
                    raise ValueError(
                        f"base model {spec.model_family!r} {label} is empty (0 bytes): {path!r}"
                    )

    # -- meta-learner training -------------------------------------------

    def train_meta_learner(
        self,
        oof_artifacts: list[OOFArtifact],
        labels: list[float],
        *,
        meta_learner_artifact_path: str | None = None,
    ) -> EnsembleResult:
        """Train the meta-learner on merged OOF predictions.

        Steps:

        1. Validate that base artifact paths exist (fail-closed).
        2. Validate that the OOF artifacts match the base specs (one
           per model family, same families, same order after sorting).
        3. Merge the OOF artifacts via :func:`merge_oof_artifacts`.
        4. Build the feature matrix ``X`` (columns = base-model OOF
           predictions, sorted by model family) and label vector ``y``
           from the merged OOF + the provided ``labels``.
        5. Train the meta-learner (LightGBM or logistic regression).
        6. Compute meta-learner predictions on ``X``.
        7. Compute ensemble metrics (MSE, RMSE, MAE, correlation).
        8. Compute per-base-model contributions.
        9. Apply calibration per the policy (binary classification only).
        10. Build the :class:`EnsembleManifest` with a deterministic
            ``ensemble_hash`` and (optionally) save the meta-learner.
        11. Return the :class:`EnsembleResult`.

        Args:
            oof_artifacts: the OOF artifacts for each base model (one
                per model family). Order does not matter — they are
                aligned by model family.
            labels: the ground-truth labels aligned with the merged OOF
                row order (sorted by ``row_id``). Must have the same
                length as the merged OOF.
            meta_learner_artifact_path: if given, the fitted
                meta-learner is persisted here (pickle) and the path +
                hash are recorded on the manifest. If ``None``, a
                placeholder path is used and the meta-learner is kept
                in-memory only.

        Returns:
            The :class:`EnsembleResult`.

        Raises:
            FileNotFoundError: if a base artifact is missing.
            ValueError: on OOF/base-spec mismatch, label-length
            mismatch, or empty inputs.
        """
        import numpy as np

        # 1. Fail-closed: base artifacts must exist.
        self._validate_base_artifacts_exist()

        # 2. Align OOF artifacts with base specs by model family.
        spec_families = {s.model_family for s in self._base_specs}
        if len(oof_artifacts) != len(self._base_specs):
            raise ValueError(
                f"expected {len(self._base_specs)} OOF artifacts (one "
                f"per base model family); got {len(oof_artifacts)}"
            )
        oof_by_family: dict[str, OOFArtifact] = {}
        for art in oof_artifacts:
            if art.model_family not in spec_families:
                raise ValueError(
                    f"OOF artifact model_family={art.model_family!r} "
                    "does not match any base model spec"
                )
            if art.model_family in oof_by_family:
                raise ValueError(f"duplicate OOF artifact for model_family={art.model_family!r}")
            oof_by_family[art.model_family] = art

        # Sort OOF artifacts in the same deterministic order as base specs.
        sorted_oof = [oof_by_family[s.model_family] for s in self._base_specs]

        # 3. Merge OOF artifacts -> row_id -> [pred per model].
        merged = merge_oof_artifacts(sorted_oof)
        row_ids = sorted(merged.keys())
        n_rows = len(row_ids)
        if n_rows == 0:
            raise ValueError("merged OOF artifacts contain no rows")

        # 4. Build X (n_rows x n_models) and y.
        X = np.array(
            [merged[rid] for rid in row_ids],
            dtype=np.float64,
        )
        # The merged predictions are already in sorted-oof order (which
        # matches _feature_names).
        if X.shape[1] != len(self._feature_names):
            raise ValueError(
                f"merged OOF has {X.shape[1]} columns but "
                f"{len(self._feature_names)} base models were expected"
            )

        # Labels: use the provided labels, validated against OOF row labels.
        if len(labels) != n_rows:
            raise ValueError(
                f"labels length {len(labels)} does not match merged OOF row count {n_rows}"
            )
        y = np.array([float(v) for v in labels], dtype=np.float64)

        # Cross-check: the OOF rows' labels should match the provided
        # labels (fail-closed on mismatch — protects against the caller
        # passing misaligned labels).
        first_art = sorted_oof[0]
        oof_label_by_row = {r.row_id: r.label for r in first_art.rows}
        for i, rid in enumerate(row_ids):
            oof_lbl = float(oof_label_by_row[rid])
            if not math.isclose(oof_lbl, y[i], rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError(
                    f"label mismatch at row_id={rid!r}: provided "
                    f"label={y[i]}, OOF artifact label={oof_lbl}"
                )

        # 5. Train the meta-learner.
        meta_learner = self._fit_meta_learner(X, y)
        self._meta_learner = meta_learner

        # 6. Meta-learner predictions on the OOF matrix.
        meta_preds = _safe_predict(meta_learner, X)
        meta_preds_list = [float(p) for p in meta_preds]

        # 7. Ensemble metrics.
        ensemble_metrics = self._compute_ensemble_metrics(meta_preds, y)

        # 8. Contributions.
        contributions = compute_contributions(
            meta_learner=meta_learner,
            feature_names=self._feature_names,
            X=X,
            y=y,
        )

        # 9. Calibration.
        calibration_report = self._apply_calibration(meta_preds_list, y)

        # 10. Build manifest.
        manifest = self._build_manifest(
            meta_learner=meta_learner,
            meta_learner_artifact_path=meta_learner_artifact_path,
        )
        self._manifest = manifest

        # 11. EnsembleResult.
        return EnsembleResult(
            manifest=manifest,
            meta_learner_predictions=meta_preds_list,
            contributions=contributions,
            calibration_report=calibration_report,
            ensemble_metrics=ensemble_metrics,
        )

    # -- meta-learner fitting --------------------------------------------

    def _fit_meta_learner(self, X: Any, y: Any) -> Any:
        """Fit the meta-learner on the OOF feature matrix.

        Dispatches on ``self.meta_learner_family``:

        - ``"lightgbm"``: trains a LightGBM ``LGBMRegressor`` (small,
          regularized) on ``(X, y)``.
        - ``"logistic_regression"``: trains a sklearn
          ``LogisticRegression`` on ``(X, y)`` (labels coerced to 0/1).

        Args:
            X: 2-D numpy array of OOF predictions.
            y: 1-D numpy array of labels.

        Returns:
            The fitted meta-learner model object.
        """
        import numpy as np

        if self.meta_learner_family == "lightgbm":
            return self._fit_lightgbm_meta(X, y)
        if self.meta_learner_family == "logistic_regression":
            return self._fit_logreg_meta(X, y, np)
        # Unreachable — validated in __init__.
        raise ValueError(f"unsupported meta_learner_family {self.meta_learner_family!r}")

    def _fit_lightgbm_meta(self, X: Any, y: Any) -> Any:
        """Train a LightGBM meta-learner."""
        try:
            from lightgbm import LGBMRegressor  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "lightgbm is not installed — cannot train a LightGBM "
                "meta-learner. Install lightgbm or use "
                "meta_learner_family='logistic_regression'."
            ) from exc

        params = dict(_DEFAULT_META_LGBM_PARAMS)
        model = LGBMRegressor(**params)
        model.fit(X, y)
        return model

    def _fit_logreg_meta(self, X: Any, y: Any, np: Any) -> Any:
        """Train a logistic-regression meta-learner."""
        try:
            from sklearn.linear_model import (
                LogisticRegression,  # type: ignore[import-not-found]
            )
        except ImportError as exc:
            raise ImportError(
                "scikit-learn is not installed — cannot train a "
                "logistic-regression meta-learner. Install scikit-learn "
                "or use meta_learner_family='lightgbm'."
            ) from exc

        # Coerce labels to 0/1 integers for logistic regression.
        y_int = np.array([round(float(v)) for v in y], dtype=np.int64)
        # Need at least 2 classes; if degenerate, fall back to a
        # constant predictor wrapper.
        if len(set(y_int.tolist())) < 2:
            return _ConstantPredictor(float(y_int[0]))
        model = LogisticRegression(C=1e6, solver="lbfgs", max_iter=10000)
        model.fit(X, y_int)
        return model

    # -- ensemble metrics -------------------------------------------------

    def _compute_ensemble_metrics(self, preds: Any, y: Any) -> dict[str, float]:
        """Compute regression-style ensemble metrics.

        Returns a dict with ``"mse"``, ``"rmse"``, ``"mae"``, and
        ``"correlation"`` (Pearson). For a single-row input or constant
        predictions, ``"correlation"`` is ``0.0`` (undefined).
        """
        import numpy as np

        preds_arr = np.asarray(preds, dtype=np.float64).ravel()
        y_arr = np.asarray(y, dtype=np.float64).ravel()
        n = preds_arr.shape[0]
        if n == 0:
            return {"mse": 0.0, "rmse": 0.0, "mae": 0.0, "correlation": 0.0}
        residuals = preds_arr - y_arr
        mse = float(np.mean(residuals**2))
        rmse = float(math.sqrt(mse))
        mae = float(np.mean(np.abs(residuals)))
        if n < 2 or float(np.std(preds_arr)) == 0 or float(np.std(y_arr)) == 0:
            corr = 0.0
        else:
            corr = float(np.corrcoef(preds_arr, y_arr)[0, 1])
            if not math.isfinite(corr):
                corr = 0.0
        return {
            "mse": mse,
            "rmse": rmse,
            "mae": mae,
            "correlation": corr,
        }

    # -- calibration ------------------------------------------------------

    def _apply_calibration(self, meta_preds: list[float], y: Any) -> EnsembleCalibrationReport:
        """Apply calibration per the configured policy.

        Calibration is only attempted for **binary classification**
        (labels in ``{0, 1}``). For regression tasks, no calibration is
        performed and eligibility is determined by the policy:

        - ``required``: ineligible (no calibration result).
        - ``optional``: eligible.
        - ``none``: eligible.

        For binary classification:

        - ``required``: calibrate (Platt); eligible iff result present.
        - ``optional``: calibrate (Platt); always eligible.
        - ``none``: do not calibrate; eligible iff no result.

        Args:
            meta_preds: the meta-learner predictions.
            y: the label vector (numpy array).

        Returns:
            The :class:`EnsembleCalibrationReport`.
        """
        import numpy as np

        y_arr = np.asarray(y, dtype=np.float64).ravel()
        is_binary = _is_binary_labels(y_arr)
        policy = CalibrationPolicy(self.calibration_policy)

        if not is_binary:
            # Regression: no calibration possible.
            result: CalibrationResult | None = None
            is_eligible = check_calibration_eligibility(result, policy)
            return EnsembleCalibrationReport(
                calibration_result=result,
                is_eligible=is_eligible,
                policy=self.calibration_policy,
            )

        if policy is CalibrationPolicy.NONE:
            # Explicitly no calibration.
            result = None
            is_eligible = check_calibration_eligibility(result, policy)
            return EnsembleCalibrationReport(
                calibration_result=result,
                is_eligible=is_eligible,
                policy=self.calibration_policy,
            )

        # REQUIRED or OPTIONAL for binary: calibrate with Platt scaling.
        # The meta-learner predictions may be outside [0, 1] (e.g. a
        # LightGBM regressor); clip to [0, 1] for calibration input.
        raw_probs = [max(0.0, min(1.0, float(p))) for p in meta_preds]
        labels_list = [float(v) for v in y_arr]
        try:
            result = calibrate(
                raw_probs=raw_probs,
                labels=labels_list,
                method=CalibrationMethod.PLATT,
            )
        except Exception:
            # Calibration failed (e.g. degenerate data) — for OPTIONAL
            # we proceed without; for REQUIRED we mark ineligible.
            result = None

        is_eligible = check_calibration_eligibility(result, policy)
        return EnsembleCalibrationReport(
            calibration_result=result,
            is_eligible=is_eligible,
            policy=self.calibration_policy,
        )

    # -- manifest building ------------------------------------------------

    def _build_manifest(
        self,
        meta_learner: Any,
        meta_learner_artifact_path: str | None,
    ) -> EnsembleManifest:
        """Build the :class:`EnsembleManifest` with a deterministic hash.

        If ``meta_learner_artifact_path`` is given, the meta-learner is
        pickled there and its SHA-256 hash is computed. Otherwise a
        placeholder path + hash are used.
        """
        if meta_learner_artifact_path is not None:
            self._save_meta_learner(meta_learner, meta_learner_artifact_path)
            artifact_hash = _hash_file(meta_learner_artifact_path)
            artifact_path = meta_learner_artifact_path
        else:
            artifact_path = "<in-memory>"
            # Deterministic placeholder hash (64 hex chars).
            artifact_hash = "0" * 64

        created_at = datetime.now(UTC).isoformat()

        # Build the manifest data *without* ensemble_hash first, then
        # compute the hash, then construct the full manifest.
        manifest_data: dict[str, Any] = {
            "schema_version": 1,
            "base_models": [bm.model_dump(mode="json") for bm in self._base_specs],
            "meta_learner_family": self.meta_learner_family,
            "meta_learner_artifact_path": artifact_path,
            "meta_learner_artifact_hash": artifact_hash,
            "created_at": created_at,
        }
        ensemble_hash = compute_ensemble_hash(manifest_data)

        return EnsembleManifest(
            schema_version=1,
            base_models=list(self._base_specs),
            meta_learner_family=self.meta_learner_family,
            meta_learner_artifact_path=artifact_path,
            meta_learner_artifact_hash=artifact_hash,
            ensemble_hash=ensemble_hash,
            created_at=created_at,
        )

    def _save_meta_learner(self, meta_learner: Any, path: str) -> None:
        """Pickle the meta-learner to ``path`` (creating parent dirs)."""
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(meta_learner, fh)

    # -- prediction -------------------------------------------------------

    def predict(self, base_predictions: dict[str, list[float]]) -> list[float]:
        """Produce ensemble predictions from base-model predictions.

        Loads the meta-learner (from the in-memory model if available,
        otherwise from the manifest's artifact path) and aligns the
        base predictions by model family in deterministic (sorted)
        order. The aligned predictions form the feature matrix for the
        meta-learner.

        Args:
            base_predictions: a dict mapping model family -> prediction
                list. Every base model family must be present and all
                prediction lists must have the same length.

        Returns:
            The ensemble predictions (one per row).

        Raises:
            ValueError: if a base model family is missing, prediction
                lengths differ, or the meta-learner is unavailable.
            FileNotFoundError: if the meta-learner artifact path does
                not exist and no in-memory model is available.
        """
        import numpy as np

        # --- Validate base_predictions ----------------------------------
        if not isinstance(base_predictions, dict):
            raise TypeError(
                f"base_predictions must be a dict; got {type(base_predictions).__name__}"
            )
        expected_families = set(self._feature_names)
        provided_families = set(base_predictions.keys())
        missing = expected_families - provided_families
        if missing:
            raise ValueError(f"base_predictions is missing model families: {sorted(missing)!r}")
        extra = provided_families - expected_families
        if extra:
            raise ValueError(f"base_predictions has unexpected model families: {sorted(extra)!r}")

        # Align lengths.
        lengths = {fam: len(base_predictions[fam]) for fam in self._feature_names}
        unique_lengths = set(lengths.values())
        if len(unique_lengths) != 1:
            raise ValueError(f"base_predictions lists must all have the same length; got {lengths}")
        n_rows = unique_lengths.pop()
        if n_rows == 0:
            raise ValueError("base_predictions lists are empty")

        # Build feature matrix in deterministic (sorted) order.
        X = np.array(
            [base_predictions[fam] for fam in self._feature_names],
            dtype=np.float64,
        ).T  # shape (n_rows, n_models)

        # --- Load meta-learner ------------------------------------------
        meta_learner = self._load_meta_learner()
        preds = _safe_predict(meta_learner, X)
        return [float(p) for p in preds]

    def _load_meta_learner(self) -> Any:
        """Load the meta-learner from memory or the manifest artifact path."""
        if self._meta_learner is not None:
            return self._meta_learner
        if self._manifest is None:
            raise ValueError(
                "no meta-learner available — call train_meta_learner first or load a manifest"
            )
        path = self._manifest.meta_learner_artifact_path
        if path == "<in-memory>":
            raise ValueError(
                "meta-learner was not persisted (no artifact path was "
                "provided to train_meta_learner) and no in-memory model "
                "is available"
            )
        if not os.path.isfile(path):
            raise FileNotFoundError(f"meta-learner artifact not found: {path!r}")
        with open(path, "rb") as fh:
            return pickle.load(fh)  # noqa: S301

    # -- manifest save / load --------------------------------------------

    def save_manifest(self, path: str) -> None:
        """Save the ensemble manifest to ``path`` as JSON.

        The manifest must have been built (via
        :meth:`train_meta_learner`) before calling this method.

        Args:
            path: the file path to write the JSON manifest to.

        Raises:
            ValueError: if no manifest has been built yet.
        """
        if self._manifest is None:
            raise ValueError("no manifest to save — call train_meta_learner first")
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        payload = self._manifest.model_dump(mode="json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, indent=2)

    @staticmethod
    def load_manifest(path: str) -> EnsembleManifest:
        """Load and validate an ensemble manifest from ``path``.

        Reads the JSON file at ``path`` and parses it into an
        :class:`EnsembleManifest` (which runs all Pydantic validators).

        Args:
            path: the file path to read the manifest from.

        Returns:
            The validated :class:`EnsembleManifest`.

        Raises:
            FileNotFoundError: if ``path`` does not exist.
            ValueError: if the file is empty, the JSON is invalid, or
                the parsed object fails Pydantic validation.
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(f"manifest file not found: {path!r}")
        if os.path.getsize(path) == 0:
            raise ValueError(f"manifest file is empty (0 bytes): {path!r}")
        with open(path, encoding="utf-8") as fh:
            try:
                payload = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValueError(f"manifest at {path!r} is not valid JSON: {exc}") from exc
        return EnsembleManifest.model_validate(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConstantPredictor:
    """Fallback predictor for degenerate single-class logistic regression.

    Exposes ``predict`` (returns the constant) and ``coef_`` (zeros) so
    :func:`compute_contributions` can handle it uniformly.
    """

    def __init__(self, constant: float) -> None:
        self._constant = float(constant)

    def predict(self, X: Any) -> Any:
        import numpy as np

        X_arr = np.asarray(X, dtype=np.float64)
        if X_arr.ndim == 1:
            n = X_arr.shape[0]
        else:
            n = X_arr.shape[0]
        return np.full(n, self._constant, dtype=np.float64)

    @property
    def coef_(self) -> Any:
        import numpy as np

        # coef_ shape depends on X columns; return a 1-element zero
        # array as a placeholder. compute_contributions falls back to
        # permutation importance when the shape mismatches.
        return np.zeros(1, dtype=np.float64)


def _is_binary_labels(y: Any) -> bool:
    """Check whether ``y`` contains only binary (0/1) values."""
    import numpy as np

    y_arr = np.asarray(y, dtype=np.float64).ravel()
    if y_arr.shape[0] == 0:
        return False
    unique = set(y_arr.tolist())
    return unique.issubset({0.0, 1.0})


def _hash_file(path: str) -> str:
    """Compute the SHA-256 hash of a file's contents.

    Args:
        path: the file path.

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "SUPPORTED_CALIBRATION_POLICIES",
    "SUPPORTED_META_LEARNER_FAMILIES",
    "BaseModelSpec",
    "ContributionReport",
    "EnsembleCalibrationReport",
    "EnsembleManifest",
    "EnsembleResult",
    "StackedEnsemble",
    "compute_contributions",
    "compute_ensemble_hash",
]
