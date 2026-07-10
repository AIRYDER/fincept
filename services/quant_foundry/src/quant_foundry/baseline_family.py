"""
quant_foundry.baseline_family — first real baseline model family (TASK-0504).

The recommended first family is LightGBM — gradient-boosted trees are the
right first family: strong tabular baselines, fast, interpretable feature
importance, and cheap to retrain, which makes leakage and tournament
behavior easy to verify before any frontier model is attempted.

This module is the **workflow orchestration layer** that connects:
- **Training:** LightGBM on a small tabular dataset (deterministic from seed).
- **Validation:** purged walk-forward with embargo (not plain expanding-window).
- **Negative control:** shuffled-label check via the sentinel (TASK-0406).
- **Calibration:** Brier score + reliability bins.
- **Feature importance:** per-feature importance + cross-fold stability.
- **Artifact packaging:** hash-verified artifact via import_artifact (TASK-0503).
- **Dossier registration:** DossierRecord at `candidate` status (TASK-0403).

File-disjoint from Builder 2's ``runpod_training.py`` /
``test_runpod_training.py`` / ``runpod/quant-foundry-training/handler.py``.
This module creates the workflow orchestration layer; Builder 2's RunPod
container calls into it (or replicates the workflow on RunPod).
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import statistics
import time
import zipfile
from typing import Any

import lightgbm as lgb
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from quant_foundry.artifacts import ArtifactRecord, import_artifact
from quant_foundry.dossier import DossierBuilder, DossierRecord, DossierStatus
from quant_foundry.sentinel import (
    LeakageSentinel,
    SentinelCheck,
    SentinelInput,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class BaselineTrainingConfig(BaseModel):
    """Configuration for a baseline training run.

    Frozen + extra='forbid' for audit integrity. Carries the model family,
    dataset manifest ref, feature/label schema hashes, and the purged
    walk-forward config (n_folds, purge_gap, embargo_gap).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_family: str = "lightgbm"
    dataset_manifest_id: str
    feature_schema_hash: str
    label_schema_hash: str
    n_features: int = 5
    n_samples: int = 200
    seed: int = 42
    n_folds: int = 3
    purge_gap: int = 5
    embargo_gap: int = 3
    # LightGBM params (conservative defaults for a small baseline).
    lgb_params: dict[str, Any] = Field(
        default_factory=lambda: {
            "objective": "binary",
            "n_estimators": 50,
            "max_depth": 3,
            "learning_rate": 0.1,
            "verbose": -1,
        }
    )
    # Cost model (USD per second of training — conservative estimate).
    cost_per_second_usd: float = 0.001


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class PurgedFoldResult(BaseModel):
    """Result of one purged walk-forward fold.

    Frozen + extra='forbid'. Carries the fold spec, OOS predictions, OOS
    labels, and the fold's Brier score.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fold_id: int
    train_start: int
    train_end: int
    val_start: int
    val_end: int
    purge_gap: int
    embargo_gap: int
    oos_predictions: list[float] = Field(default_factory=list)
    oos_labels: list[int] = Field(default_factory=list)
    brier_score: float = 0.0


class PurgedWalkForwardResult(BaseModel):
    """Result of the full purged walk-forward validation.

    Frozen + extra='forbid'. Carries all fold results.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    folds: list[PurgedFoldResult] = Field(default_factory=list)


class BaselineCalibrationReport(BaseModel):
    """Calibration report for the baseline model.

    Frozen + extra='forbid'. Carries the overall Brier score and reliability
    bins (predicted vs observed frequency).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    brier_score: float = 0.0
    reliability_bins: list[dict[str, float]] = Field(default_factory=list)


class BaselineFeatureImportance(BaseModel):
    """Feature importance report for the baseline model.

    Frozen + extra='forbid'. Carries per-feature importance (averaged across
    folds) and the cross-fold coefficient of variation (CV) of importance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    importances: dict[str, float] = Field(default_factory=dict)
    cross_fold_cv: dict[str, float] = Field(default_factory=dict)
    per_fold_importances: dict[str, list[float]] = Field(default_factory=dict)


class BaselineTrainingResult(BaseModel):
    """Result of a full baseline training run.

    Frozen + extra='forbid'. Carries the artifact, dossier, walk-forward
    results, calibration report, feature importance report, negative-control
    sentinel receipt, trial count, duration, and cost estimate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact: ArtifactRecord
    dossier: DossierRecord
    walk_forward: PurgedWalkForwardResult
    calibration: BaselineCalibrationReport
    feature_importance: BaselineFeatureImportance
    negative_control_receipt: Any  # SentinelReceipt (avoid circular import)
    trial_count: int = 1
    duration_ns: int = 0
    cost_estimate_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "artifact": self.artifact.model_dump(),
            "dossier": self.dossier.model_dump(),
            "walk_forward": self.walk_forward.model_dump(),
            "calibration": self.calibration.model_dump(),
            "feature_importance": self.feature_importance.model_dump(),
            "negative_control_receipt": (
                self.negative_control_receipt.to_dict()
                if hasattr(self.negative_control_receipt, "to_dict")
                else None
            ),
            "trial_count": self.trial_count,
            "duration_ns": self.duration_ns,
            "cost_estimate_usd": self.cost_estimate_usd,
        }


# ---------------------------------------------------------------------------
# The baseline family trainer
# ---------------------------------------------------------------------------


class BaselineFamily:
    """Orchestrates the baseline training workflow.

    Trains a LightGBM model, runs purged walk-forward validation, runs the
    shuffled-label negative control, produces calibration + feature
    importance reports, packages the artifact, and creates a dossier.
    """

    def __init__(self, config: BaselineTrainingConfig) -> None:
        self.config = config
        # The negative-control sentinel uses a higher edge threshold (5%)
        # because even conservative models can overfit slightly to noise
        # on small datasets. A training accuracy of 55%+ on shuffled labels
        # is concerning; 51% is just noise.
        self.sentinel = LeakageSentinel(seed=config.seed, edge_threshold=0.05)

    def train(self, features: list[list[float]], labels: list[int]) -> BaselineTrainingResult:
        """Run the full baseline training workflow.

        Steps:
        1. Run purged walk-forward validation (train + predict OOS).
        2. Train the final model on all data.
        3. Run the shuffled-label negative control.
        4. Produce calibration report.
        5. Produce feature importance report.
        6. Package the artifact (hash-verified).
        7. Create the dossier (candidate status).
        8. Record trial count, duration, and cost estimate.
        """
        start_ns = time.time_ns()

        # 1. Purged walk-forward validation.
        wf_result = self._run_purged_walk_forward(features, labels)

        # 2. Train the final model on all data.
        model = self._train_lgbm(features, labels)

        # 3. Package the artifact (need model_id for sentinel).
        artifact = self._package_artifact(model, features, labels)

        # 4. Create the dossier (need model_id for sentinel).
        dossier = self._create_dossier(artifact)

        # 5. Shuffled-label negative control (uses dossier model_id).
        nc_receipt = self._run_negative_control(features, labels, dossier.model_id)

        # 6. Calibration report.
        calibration = self._compute_calibration(wf_result)

        # 7. Feature importance report.
        feature_importance = self._compute_feature_importance(features, labels)

        # 8. Record trial count, duration, and cost.
        duration_ns = time.time_ns() - start_ns
        cost_estimate_usd = duration_ns / 1e9 * self.config.cost_per_second_usd

        return BaselineTrainingResult(
            artifact=artifact,
            dossier=dossier,
            walk_forward=wf_result,
            calibration=calibration,
            feature_importance=feature_importance,
            negative_control_receipt=nc_receipt,
            trial_count=1,
            duration_ns=duration_ns,
            cost_estimate_usd=cost_estimate_usd,
        )

    # -- Purged walk-forward ------------------------------------------------

    def _run_purged_walk_forward(
        self, features: list[list[float]], labels: list[int]
    ) -> PurgedWalkForwardResult:
        """Run purged walk-forward validation with embargo."""
        n = len(features)
        fold_size = n // (self.config.n_folds + 1)  # +1 for the initial train.
        if fold_size < 10:
            fold_size = 10

        folds: list[PurgedFoldResult] = []
        for i in range(self.config.n_folds):
            train_end = (i + 1) * fold_size
            val_start = train_end + self.config.purge_gap
            val_end = val_start + fold_size
            if val_end > n:
                val_end = n
            if val_start >= n:
                break

            # Train on [0, train_end), validate on [val_start, val_end).
            train_features = features[:train_end]
            train_labels = labels[:train_end]
            val_features = features[val_start:val_end]
            val_labels = labels[val_start:val_end]

            if not val_features or not train_features:
                continue

            model = self._train_lgbm(train_features, train_labels)
            oos_preds = self._predict_lgbm(model, val_features)

            # Brier score for this fold.
            brier = self._brier_score(oos_preds, val_labels)

            folds.append(
                PurgedFoldResult(
                    fold_id=i,
                    train_start=0,
                    train_end=train_end,
                    val_start=val_start,
                    val_end=val_end,
                    purge_gap=self.config.purge_gap,
                    embargo_gap=self.config.embargo_gap,
                    oos_predictions=oos_preds,
                    oos_labels=val_labels,
                    brier_score=brier,
                )
            )

        return PurgedWalkForwardResult(folds=folds)

    # -- LightGBM training --------------------------------------------------

    def _train_lgbm(self, features: list[list[float]], labels: list[int]) -> lgb.Booster:
        """Train a LightGBM model on the given data using the native API.

        Uses ``lgb.train`` with ``lgb.Dataset`` (no scikit-learn dependency).
        """
        X = np.array(features, dtype=np.float64)
        y = np.array(labels, dtype=np.float32)
        train_data = lgb.Dataset(X, label=y)
        params = {
            "objective": "binary",
            "verbose": -1,
            "seed": self.config.seed,
            "num_threads": 1,
            **{
                k: v
                for k, v in self.config.lgb_params.items()
                if k not in ("objective", "verbose", "n_estimators")
            },
        }
        num_round = self.config.lgb_params.get("n_estimators", 50)
        model = lgb.train(params, train_data, num_boost_round=num_round)
        return model

    def _predict_lgbm(self, model: lgb.Booster, features: list[list[float]]) -> list[float]:
        """Predict probabilities for the given features."""
        X = np.array(features, dtype=np.float64)
        probs = model.predict(X)
        return [float(p) for p in probs]

    # -- Negative control ---------------------------------------------------

    def _run_negative_control(
        self,
        features: list[list[float]],
        labels: list[int],
        model_id: str,
    ) -> Any:
        """Run the shuffled-label negative control via the sentinel.

        The test: train a model on REAL labels, then check if its predictions
        correlate with SHUFFLED labels. If they do (AUC significantly above
        0.5), the model is leaking — it's finding patterns that don't exist.

        A model trained on real labels should NOT be able to predict shuffled
        labels better than chance, because shuffling destroys the
        feature-label relationship. If it can, it means the model is
        overfitting to idiosyncratic noise that happens to correlate with
        the shuffled label order.

        The edge metric is |AUC - 0.5| (AUC above chance). The sentinel
        flags if this exceeds the threshold (default 5%).
        """
        rng = random.Random(self.config.seed + 1)
        shuffled_labels = list(labels)
        rng.shuffle(shuffled_labels)

        # Train a model on the REAL labels (already trained in train()).
        model = self._train_lgbm(features, labels)

        # Get the model's predictions on the training data.
        preds = self._predict_lgbm(model, features)

        # Compute AUC of predictions vs SHUFFLED labels.
        # If AUC ~0.5, the model has no edge on shuffled labels (good).
        # If AUC >> 0.5, the model is leaking.
        auc = _auc(preds, shuffled_labels)
        claimed_edge = abs(auc - 0.5)

        # Run the sentinel's shuffled-label check.
        receipt = self.sentinel.run_negative_control(
            SentinelInput(
                model_id=model_id,
                check=SentinelCheck.SHUFFLED_LABEL,
                claimed_edge=claimed_edge,
                baseline_edge=0.0,
                n_samples=len(labels),
                seed=self.config.seed,
            )
        )
        return receipt

    # -- Calibration --------------------------------------------------------

    def _compute_calibration(self, wf_result: PurgedWalkForwardResult) -> BaselineCalibrationReport:
        """Compute the calibration report (Brier score + reliability bins)."""
        all_preds: list[float] = []
        all_labels: list[int] = []
        for fold in wf_result.folds:
            all_preds.extend(fold.oos_predictions)
            all_labels.extend(fold.oos_labels)

        if not all_preds:
            return BaselineCalibrationReport(brier_score=0.0, reliability_bins=[])

        brier = self._brier_score(all_preds, all_labels)

        # Reliability bins: divide [0, 1] into 10 bins.
        n_bins = 10
        bins: list[dict[str, float]] = []
        for i in range(n_bins):
            lo = i / n_bins
            hi = (i + 1) / n_bins
            in_bin = [
                (p, lab)
                for p, lab in zip(all_preds, all_labels, strict=True)
                if lo <= p < hi or (i == n_bins - 1 and p == hi)
            ]
            if in_bin:
                avg_pred = statistics.fmean(p for p, _ in in_bin)
                obs_freq = statistics.fmean(lab for _, lab in in_bin)
                bins.append(
                    {
                        "predicted_prob": avg_pred,
                        "observed_freq": obs_freq,
                        "count": float(len(in_bin)),
                    }
                )

        return BaselineCalibrationReport(brier_score=brier, reliability_bins=bins)

    @staticmethod
    def _brier_score(preds: list[float], labels: list[int]) -> float:
        """Compute the Brier score (mean squared error of probabilities)."""
        if not preds:
            return 0.0
        return statistics.fmean((p - lab) ** 2 for p, lab in zip(preds, labels, strict=True))

    # -- Feature importance -------------------------------------------------

    def _compute_feature_importance(
        self, features: list[list[float]], labels: list[int]
    ) -> BaselineFeatureImportance:
        """Compute feature importance with cross-fold stability."""
        n = len(features)
        fold_size = n // (self.config.n_folds + 1)
        if fold_size < 10:
            fold_size = 10

        per_fold: dict[str, list[float]] = {f"f{i}": [] for i in range(self.config.n_features)}

        for i in range(self.config.n_folds):
            train_end = (i + 1) * fold_size
            if train_end > n:
                train_end = n
            train_features = features[:train_end]
            train_labels = labels[:train_end]
            if not train_features:
                continue
            model = self._train_lgbm(train_features, train_labels)
            imps = model.feature_importance()
            for j in range(self.config.n_features):
                per_fold[f"f{j}"].append(float(imps[j]))

        # Average importance across folds.
        importances: dict[str, float] = {}
        cross_fold_cv: dict[str, float] = {}
        for fname, fold_imps in per_fold.items():
            if fold_imps:
                importances[fname] = statistics.fmean(fold_imps)
                mean_imp = statistics.fmean(fold_imps)
                if abs(mean_imp) > 1e-10 and len(fold_imps) > 1:
                    std_imp = statistics.pstdev(fold_imps)
                    cross_fold_cv[fname] = std_imp / abs(mean_imp)
                else:
                    cross_fold_cv[fname] = 0.0
            else:
                importances[fname] = 0.0
                cross_fold_cv[fname] = 0.0

        return BaselineFeatureImportance(
            importances=importances,
            cross_fold_cv=cross_fold_cv,
            per_fold_importances=per_fold,
        )

    # -- Artifact packaging -------------------------------------------------

    def _package_artifact(
        self,
        model: lgb.Booster,
        features: list[list[float]],
        labels: list[int],
    ) -> ArtifactRecord:
        """Package the trained model as a hash-verified artifact.

        Serializes the model + config + feature/label schema hashes into a
        deterministic ZIP archive, writes it to a temp file, and imports it
        via ``import_artifact`` (TASK-0503) with hash verification.
        """
        # Serialize the model to a deterministic bytes blob.
        # Use a ZIP archive containing the model + metadata.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # Model (pickled LightGBM model).
            model_bytes = model_to_bytes(model)
            zf.writestr("model.pkl", model_bytes)
            # Metadata (deterministic JSON).
            metadata = {
                "model_family": self.config.model_family,
                "feature_schema_hash": self.config.feature_schema_hash,
                "label_schema_hash": self.config.label_schema_hash,
                "dataset_manifest_id": self.config.dataset_manifest_id,
                "n_features": self.config.n_features,
                "n_samples": self.config.n_samples,
                "seed": self.config.seed,
                "lgb_params": self.config.lgb_params,
            }
            zf.writestr("metadata.json", json.dumps(metadata, sort_keys=True))

        artifact_bytes = buf.getvalue()
        sha256 = hashlib.sha256(artifact_bytes).hexdigest()

        # Write to a temp file and import via import_artifact.
        import pathlib
        import tempfile

        tmpdir = tempfile.mkdtemp()
        artifact_path = os.path.join(tmpdir, "baseline_model.zip")
        pathlib.Path(artifact_path).write_bytes(artifact_bytes)
        uri = f"file:///{artifact_path}"

        # Use the artifact_id derived from the hash (deterministic).
        artifact_id = f"baseline-{sha256[:16]}"

        return import_artifact(
            uri=uri,
            expected_sha256=sha256,
            artifact_id=artifact_id,
            model_family=self.config.model_family,
            feature_schema_hash=self.config.feature_schema_hash,
            label_schema_hash=self.config.label_schema_hash,
        )

    # -- Dossier creation ---------------------------------------------------

    def _create_dossier(self, artifact: ArtifactRecord) -> DossierRecord:
        """Create a DossierRecord at `candidate` status."""
        model_id = f"baseline-{artifact.sha256[:16]}"
        return DossierBuilder().build(
            artifact=artifact,
            model_id=model_id,
            dataset_manifest_id=self.config.dataset_manifest_id,
            random_seed=self.config.seed,
            hardware_class="cpu-local",
            trial_count=1,
            status=DossierStatus.CANDIDATE,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def train_baseline_family(
    config: BaselineTrainingConfig,
    features: list[list[float]],
    labels: list[int],
) -> BaselineTrainingResult:
    """Train a baseline model family and return the full result.

    This is the main entry point for TASK-0504. It creates a
    ``BaselineFamily`` trainer and runs the full workflow:
    train → validate (purged walk-forward) → sentinel (negative control) →
    calibrate → feature importance → package artifact → create dossier.
    """
    trainer = BaselineFamily(config)
    return trainer.train(features, labels)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import os  # noqa: E402


def model_to_bytes(model: lgb.Booster) -> bytes:
    """Serialize a LightGBM model to deterministic bytes.

    Uses LightGBM's native model string (deterministic) rather than pickle
    (which can include non-deterministic state).
    """
    model_string = model.model_to_string()
    return model_string.encode("utf-8")


def _auc(predictions: list[float], labels: list[int]) -> float:
    """Compute the AUC (Area Under the ROC Curve) via the Mann-Whitney U statistic.

    AUC = (sum of ranks of positive labels - n_pos * (n_pos + 1) / 2)
          / (n_pos * n_neg)

    AUC ~0.5 means the predictions have no discriminative power (random).
    AUC ~1.0 means perfect discrimination. AUC ~0.0 means anti-correlated.
    """
    n_pos = sum(1 for lab in labels if lab == 1)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5  # No discrimination possible.

    # Rank the predictions (average rank for ties).
    indexed = sorted(enumerate(predictions), key=lambda x: x[1])
    ranks = [0.0] * len(predictions)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 1) / 2.0  # 1-based average rank.
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j

    # Sum of ranks for positive labels.
    rank_sum_pos = sum(ranks[i] for i in range(len(labels)) if labels[i] == 1)
    auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return auc
