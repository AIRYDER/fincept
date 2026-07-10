"""
agents.gbm_predictor.train - offline LightGBM trainer.

Run::

  python -m agents.gbm_predictor.train \\
      --input data/bars_with_features.parquet \\
      --horizon-bars 15 \\
      --bar-seconds 60 \\
      --out-dir models/gbm_predictor

Input parquet must have a ``close`` column (for label construction) and
all of ``FEATURES``.  Label is the sign of the forward return over
``horizon_bars`` bars (1 = up, 0 = down).

Output:
  - ``model.txt``  LightGBM Booster save format (text; portable across
                   versions; loads via ``lgb.Booster(model_file=...)``).
  - ``meta.json``  Records ``features``, ``horizon_bars``,
                   ``horizon_ns``, ``trained_at``, ``train_rows``,
                   ``val_rows``, ``best_iter``, ``best_auc``.  The
                   inference loop reads ``features`` (so the input
                   vector order is recovered) and ``horizon_ns`` (so
                   emitted Predictions carry the right horizon).

Two evaluation modes are supported:

1. ``--cv-folds 0`` (default, back-compat): single time-ordered 80/20
   holdout with early stopping.  Quick sanity check; not a production
   model.

2. ``--cv-folds N`` (recommended, TASK-023): expanding-window
   walk-forward CV with a purge gap of ``--purge-bars`` (default =
   horizon-bars) between train and val to eliminate label leakage from
   horizon overlap, plus an optional ``--embargo-bars`` gap after each
   val window.  Reports mean/std/min/max AUC across folds.  Final
   model is then refit on the full series for ``median(best_iter)``
   rounds (no holdout, no early stopping) so the production model
   sees every bar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import time
from typing import Any

import lightgbm as lgb
import numpy as np
import polars as pl
from fincept_core.datasets import ArtifactManifest, make_folds

from agents.gbm_predictor.features import FEATURES, _compute_feature_schema_hash


def build_dataset(
    df: pl.DataFrame,
    *,
    horizon_bars: int,
    feature_names: list[str],
    close_column: str = "close",
) -> tuple[np.ndarray, np.ndarray]:
    """Construct (X, y) arrays from a parquet frame.

    Label is the sign of forward return over ``horizon_bars``.  Rows
    where the forward return is null (the last ``horizon_bars`` rows)
    or any feature is null are dropped.
    """
    if close_column not in df.columns:
        raise ValueError(f"input dataframe is missing required column {close_column!r}")
    missing = [f for f in feature_names if f not in df.columns]
    if missing:
        raise ValueError(f"input dataframe is missing feature columns: {missing}")

    forward = (pl.col(close_column).shift(-horizon_bars) / pl.col(close_column)) - 1
    df = df.with_columns(forward.alias("__forward__")).drop_nulls(
        ["__forward__", *feature_names]
    )

    if df.is_empty():
        raise ValueError("dataset is empty after dropping nulls")

    y = (df["__forward__"] > 0).to_numpy().astype(int)
    X = df.select(feature_names).to_numpy()
    return X, y


def train_booster(
    X: np.ndarray,
    y: np.ndarray,
    *,
    num_boost_round: int = 500,
    early_stopping_rounds: int = 30,
    params: dict[str, Any] | None = None,
    val_fraction: float = 0.2,
) -> tuple[lgb.Booster, dict[str, Any]]:
    """Fit a binary classifier with a time-ordered holdout split.

    Returns the trained booster + a dict of training metadata
    (``train_rows``, ``val_rows``, ``best_iter``, ``best_auc``).
    """
    if not 0 < val_fraction < 1:
        raise ValueError(f"val_fraction must be in (0, 1); got {val_fraction}")

    split = int(len(X) * (1 - val_fraction))
    if split < 1 or split >= len(X):
        raise ValueError(f"insufficient rows for split: total={len(X)}, split={split}")

    dtrain = lgb.Dataset(X[:split], y[:split])
    dval = lgb.Dataset(X[split:], y[split:], reference=dtrain)

    final_params: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 63,
        "verbose": -1,
    }
    if params:
        final_params.update(params)

    model = lgb.train(
        final_params,
        dtrain,
        num_boost_round=num_boost_round,
        valid_sets=[dval],
        callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
    )

    best_score = model.best_score.get("valid_0", {}).get("auc")
    return model, {
        "train_rows": int(split),
        "val_rows": int(len(X) - split),
        "best_iter": int(model.best_iteration or num_boost_round),
        "best_auc": float(best_score) if best_score is not None else None,
    }


def walk_forward_splits(
    n_rows: int,
    *,
    n_folds: int,
    purge_bars: int = 0,
    embargo_bars: int = 0,
    min_train_rows: int = 1,
) -> list[tuple[slice, slice]]:
    """Anchored expanding-window splits with purge + embargo.

    Layout for ``n_folds`` folds on a row-ordered series of length
    ``n_rows`` (uses ``fold_size = n_rows // (n_folds + 1)`` so every
    fold sees the same amount of validation data and the first fold
    has at least one ``fold_size`` block to train on)::

        fold i: train = [0,                              val_start_i - purge_bars)
                val   = [val_start_i,                     val_start_i + fold_size)
        where val_start_i = (i + 1) * fold_size           for i in range(n_folds)

    The ``embargo_bars`` argument is reserved for sliding-window
    setups; it widens the no-train zone AFTER each validation block.
    For the anchored expansion above it has no effect on subsequent
    folds (training always starts at row 0) but the contract is kept
    so callers can switch to a sliding window without an API change.

    Returns a list of ``(train_slice, val_slice)`` ready to index a
    NumPy array.  Folds whose train slice would have fewer than
    ``min_train_rows`` are dropped.
    """
    if n_folds < 2:
        raise ValueError(f"n_folds must be >= 2 for walk-forward CV; got {n_folds}")
    if purge_bars < 0 or embargo_bars < 0:
        raise ValueError("purge_bars and embargo_bars must be non-negative")
    fold_size = n_rows // (n_folds + 1)
    if fold_size <= 0:
        raise ValueError(
            f"insufficient rows for {n_folds} folds: need at least {n_folds + 1}, got {n_rows}"
        )

    # Delegate the fold-position math to the shared CV utility
    # (``fincept_core.datasets.cv.make_folds``).  We ask for ``n_folds``
    # equal-size validation windows of ``fold_size`` bars with no
    # inter-window purge or embargo -- the purge is applied per-fold
    # between ``train_end`` and ``val_start`` during the translation
    # below, and ``embargo_bars`` remains a no-op for the anchored
    # expansion (training always starts at row 0).  Passing
    # ``purge_bars=0`` / ``embargo_bars=0`` here keeps the canonical
    # fold positions identical to the previous hand-rolled layout
    # (contiguous validation windows at multiples of ``fold_size``).
    folds = make_folds(
        n_rows,
        n_folds=n_folds,
        train_min_bars=fold_size,
        val_bars=fold_size,
        purge_bars=0,
        embargo_bars=0,
    )

    splits: list[tuple[slice, slice]] = []
    for fold in folds:
        val_start = fold.val_start
        # The last fold absorbs the remainder (n_rows % (n_folds + 1))
        # exactly as the previous implementation did.
        val_end = fold.val_end if fold.index < n_folds - 1 else n_rows
        train_end = val_start - purge_bars
        if train_end < min_train_rows or val_end <= val_start:
            continue
        # embargo_bars is reserved for sliding-window setups; for the
        # anchored expansion it has no effect on subsequent folds
        # (training always starts at row 0).  The shared ``make_folds``
        # accepts ``embargo_bars`` but we deliberately pass 0 above so
        # the canonical fold positions are unaffected -- the trainer's
        # local logic still ignores it (preserved behavior, not a bug).
        splits.append((slice(0, train_end), slice(val_start, val_end)))
    _ = embargo_bars  # silence "unused arg" lint; the contract keeps it.
    if not splits:
        raise ValueError(
            f"no usable folds (n_rows={n_rows}, n_folds={n_folds}, purge_bars={purge_bars}, "
            f"min_train_rows={min_train_rows})"
        )
    return splits


def walk_forward_cv(
    X: np.ndarray,
    y: np.ndarray,
    *,
    n_folds: int,
    purge_bars: int,
    embargo_bars: int = 0,
    num_boost_round: int = 500,
    early_stopping_rounds: int = 30,
    params: dict[str, Any] | None = None,
    checkpoint_dir: pathlib.Path | None = None,
    resume_from_fold: int | None = None,
) -> list[dict[str, Any]]:
    """Run expanding-window walk-forward CV; return per-fold metrics.

    Each fold trains a fresh booster on the train slice with early
    stopping against the val slice and records ``train_rows``,
    ``val_rows``, ``best_iter``, ``best_auc``.  No model state leaks
    between folds (every booster is local).

    When ``checkpoint_dir`` is set, each fold's booster is saved to
    ``checkpoint_dir/fold_<idx>_model.txt`` alongside a
    ``fold_<idx>_meta.json`` so an interrupted run can be resumed via
    ``resume_from_fold`` (folds below that index are loaded from disk
    instead of retrained).

    The aggregate caller (in :func:`main`) uses these to (a) report
    AUC stability across regimes and (b) pick a stable
    ``median(best_iter)`` for the final full-data refit.
    """
    splits = walk_forward_splits(
        len(X),
        n_folds=n_folds,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
    )
    final_params: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 63,
        "verbose": -1,
    }
    if params:
        final_params.update(params)

    fold_metrics: list[dict[str, Any]] = []
    for fold_idx, (train_slice, val_slice) in enumerate(splits):
        X_tr, y_tr = X[train_slice], y[train_slice]
        X_va, y_va = X[val_slice], y[val_slice]
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_va)) < 2:
            # Degenerate fold (one class only); record a None AUC and
            # move on - aggregator will skip these for mean/std but
            # surface them in the fold list.
            fold_metrics.append(
                {
                    "fold": fold_idx,
                    "train_rows": len(X_tr),
                    "val_rows": len(X_va),
                    "best_iter": None,
                    "best_auc": None,
                    "reason_skipped": "single-class fold",
                }
            )
            continue
        if resume_from_fold is not None and fold_idx < resume_from_fold:
            assert (
                checkpoint_dir is not None
            )  # resume_from_fold implies checkpoint_dir is set
            ckpt_path = checkpoint_dir / f"fold_{fold_idx}_model.txt"
            if ckpt_path.exists():
                booster = lgb.Booster(model_file=str(ckpt_path))
                # Still record metrics from saved meta
                meta_path = checkpoint_dir / f"fold_{fold_idx}_meta.json"
                if meta_path.exists():
                    saved_meta = json.loads(meta_path.read_text())
                    fold_metrics.append(
                        {
                            "fold": fold_idx,
                            "train_rows": saved_meta["train_rows"],
                            "val_rows": saved_meta["val_rows"],
                            "best_iter": saved_meta["best_iter"],
                            "best_auc": saved_meta["best_auc"],
                            "resumed": True,
                        }
                    )
                    continue
            # If checkpoint doesn't exist, fall through to normal training
        dtrain = lgb.Dataset(X_tr, y_tr)
        dval = lgb.Dataset(X_va, y_va, reference=dtrain)
        booster = lgb.train(
            final_params,
            dtrain,
            num_boost_round=num_boost_round,
            valid_sets=[dval],
            callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
        )
        best_score = booster.best_score.get("valid_0", {}).get("auc")
        if checkpoint_dir is not None:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            ckpt_path = checkpoint_dir / f"fold_{fold_idx}_model.txt"
            booster.save_model(str(ckpt_path))
            # Also save fold metadata
            meta_path = checkpoint_dir / f"fold_{fold_idx}_meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "fold": fold_idx,
                        "train_rows": len(X_tr),
                        "val_rows": len(X_va),
                        "best_iter": int(booster.best_iteration or num_boost_round),
                        "best_auc": float(best_score)
                        if best_score is not None
                        else None,
                        "checkpoint_path": str(ckpt_path),
                    },
                    indent=2,
                )
            )
        fold_metrics.append(
            {
                "fold": fold_idx,
                "train_rows": len(X_tr),
                "val_rows": len(X_va),
                "best_iter": int(booster.best_iteration or num_boost_round),
                "best_auc": float(best_score) if best_score is not None else None,
            }
        )
    return fold_metrics


def summarize_cv(folds: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-fold AUC + best_iter into mean/std/min/max stats.

    Folds with ``best_auc is None`` (single-class skips) are excluded
    from numeric stats but their count is reported as ``skipped``.
    """
    aucs = [f["best_auc"] for f in folds if f["best_auc"] is not None]
    iters = [f["best_iter"] for f in folds if f["best_iter"] is not None]
    summary: dict[str, Any] = {
        "n_folds": len(folds),
        "n_scored": len(aucs),
        "n_skipped": len(folds) - len(aucs),
    }
    if aucs:
        summary.update(
            {
                "mean_auc": float(np.mean(aucs)),
                "std_auc": float(np.std(aucs, ddof=1)) if len(aucs) > 1 else 0.0,
                "min_auc": float(np.min(aucs)),
                "max_auc": float(np.max(aucs)),
            }
        )
    if iters:
        summary["median_best_iter"] = int(np.median(iters))
    return summary


def train_full(
    X: np.ndarray,
    y: np.ndarray,
    *,
    num_boost_round: int,
    params: dict[str, Any] | None = None,
) -> lgb.Booster:
    """Refit on the full series with a fixed round count.

    Used after walk-forward CV: we already chose the right number of
    rounds via early stopping per fold, so the production model can
    train on ALL data without a holdout (more data = better, and the
    round count is no longer a tunable so over-fitting risk is
    bounded).
    """
    final_params: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 63,
        "verbose": -1,
    }
    if params:
        final_params.update(params)
    dtrain = lgb.Dataset(X, y)
    return lgb.train(final_params, dtrain, num_boost_round=num_boost_round)


def save_artifacts(
    model: lgb.Booster,
    *,
    out_dir: pathlib.Path,
    feature_names: list[str],
    horizon_bars: int,
    bar_seconds: int,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    """Write ``model.txt`` + ``meta.json`` into ``out_dir``.

    The ``meta.json`` always carries a ``promotion_pipeline`` field:
    - ``"operator_trusted"`` (default): the model was trained via Path A
      (dashboard / direct CLI) and bypasses the tournament scoring,
      dossier, and promotion gate. The operator accepts responsibility
      for the model's quality. See docs/TRAINING_ANALYSIS.md finding F5.
    - ``"tournament_gated"``: set when ``--create-dossier`` is used and
      the dossier JSON is written alongside the model artifacts. The
      model can then be imported into the DossierStore and scored by the
      tournament.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_dir / "model.txt"))
    horizon_ns = horizon_bars * bar_seconds * 1_000_000_000
    meta: dict[str, Any] = {
        "features": feature_names,
        "horizon_bars": horizon_bars,
        "bar_seconds": bar_seconds,
        "horizon_ns": horizon_ns,
        "trained_at": int(time.time()),
        "promotion_pipeline": "operator_trusted",
    }
    if extra_meta:
        meta.update(extra_meta)
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def _compute_model_sha256(model: lgb.Booster) -> str:
    """Compute a SHA256 hash of the model bytes (pickle-based).

    This mirrors the RunPod real trainer's approach. The hash is
    container-pinned (not cross-container reproducible) — see
    docs/TRAINING_ANALYSIS.md finding F6.
    """
    import hashlib
    import pickle

    model_bytes = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
    return hashlib.sha256(model_bytes).hexdigest()


def _compute_schema_hashes(
    feature_names: list[str],
    *,
    dataset_ref: str,
    n_features: int,
) -> tuple[str, str]:
    """Compute feature + label schema hashes matching the RunPod convention."""
    import hashlib

    feature_hash = hashlib.sha256(
        f"{dataset_ref}:n_features={n_features}".encode(),
    ).hexdigest()[:16]
    label_hash = hashlib.sha256(
        f"{dataset_ref}:label=binary".encode(),
    ).hexdigest()[:16]
    return feature_hash, label_hash


def create_dossier(
    model: lgb.Booster,
    *,
    out_dir: pathlib.Path,
    feature_names: list[str],
    horizon_bars: int,
    bar_seconds: int,
    training_meta: dict[str, Any],
    input_path: str,
    model_id: str | None = None,
) -> pathlib.Path:
    """Build a ``DossierRecord`` JSON from the training output.

    This is the F5 bridge: it lets a Path A (dashboard / direct CLI)
    model enter the tournament pipeline by producing a dossier JSON
    that can be imported into the ``DossierStore``.

    The dossier is written to ``out_dir / "dossier.json"``. The
    ``meta.json`` ``promotion_pipeline`` field is updated to
    ``"tournament_gated"``.

    Uses a lazy import of ``quant_foundry.dossier`` so the agents
    package does not have a hard dependency on quant_foundry. If
    quant_foundry is not installed, a plain JSON dossier stub is
    written instead (same fields, no Pydantic validation).

    Args:
        model: the trained LightGBM booster.
        out_dir: the model output directory (where ``model.txt`` and
            ``meta.json`` already live).
        feature_names: the canonical feature list.
        horizon_bars: the forward-return horizon in bars.
        bar_seconds: the bar frequency in seconds.
        training_meta: the training metadata dict (from walk_forward_cv
            or train_booster).
        input_path: the dataset path (for the dataset_manifest_id).
        model_id: optional model ID; defaults to ``model:<out_dir.name>``.

    Returns:
        The path to the written dossier JSON.
    """
    import hashlib

    model_name = pathlib.Path(out_dir).name
    mid = model_id or f"model:{model_name}"
    sha256 = _compute_model_sha256(model)
    feature_hash, label_hash = _compute_schema_hashes(
        feature_names,
        dataset_ref=input_path,
        n_features=len(feature_names),
    )
    artifact_id = f"artifact:{model_name}:{sha256[:16]}"

    # Build training_metrics dict (floats only, matching DossierRecord schema).
    cv_summary = training_meta.get("cv_summary", {})
    best_auc = training_meta.get("best_auc")
    training_metrics: dict[str, float] = {}
    if best_auc is not None:
        training_metrics["best_auc"] = float(best_auc)
    if "mean_auc" in cv_summary:
        training_metrics["mean_auc"] = float(cv_summary["mean_auc"])
    if "std_auc" in cv_summary:
        training_metrics["std_auc"] = float(cv_summary["std_auc"])
    training_metrics["train_rows"] = float(
        training_meta.get("final_train_rows", training_meta.get("train_rows", 0))
    )
    training_metrics["val_rows"] = float(training_meta.get("val_rows", 0))

    # Try to build a proper DossierRecord via quant_foundry.
    try:
        from quant_foundry.artifacts import ArtifactRecord
        from quant_foundry.dossier import DossierBuilder, DossierStatus

        artifact = ArtifactRecord(
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=len(model.model_to_string()),
            model_family="gbm",
            created_at_ns=time.time_ns(),
            feature_schema_hash=feature_hash,
            label_schema_hash=label_hash,
            code_git_sha="local",
            lockfile_hash="local",
            container_image_digest="local",
        )
        builder = DossierBuilder()
        dossier = builder.build(
            artifact=artifact,
            model_id=mid,
            dataset_manifest_id=input_path,
            dataset_manifest_ref=input_path,
            random_seed=training_meta.get("seed"),
            hardware_class="local",
            trial_count=1,
            training_metrics=training_metrics,
            status=DossierStatus.CANDIDATE,
        )
        dossier_path = out_dir / "dossier.json"
        dossier_path.write_text(dossier.model_dump_json(indent=2))
    except ImportError:
        # quant_foundry not installed — write a plain JSON dossier stub
        # with the same fields so it can be imported later.
        dossier_stub: dict[str, Any] = {
            "schema_version": 1,
            "model_id": mid,
            "artifact_manifest_id": artifact_id,
            "artifact_sha256": sha256,
            "dataset_manifest_id": input_path,
            "dataset_manifest_ref": input_path,
            "feature_schema_hash": feature_hash,
            "label_schema_hash": label_hash,
            "code_git_sha": "local",
            "lockfile_hash": "local",
            "container_image_digest": "local",
            "random_seed": training_meta.get("seed"),
            "hardware_class": "local",
            "trial_count": 1,
            "training_metrics": training_metrics,
            "status": "candidate",
            "settlement_evidence_refs": [],
            "shadow_prediction_refs": [],
            "blocking_issues": [],
            "content_hash": hashlib.sha256(
                json.dumps(
                    {
                        "model_id": mid,
                        "artifact_sha256": sha256,
                        "dataset_manifest_id": input_path,
                    },
                    sort_keys=True,
                ).encode(),
            ).hexdigest(),
        }
        dossier_path = out_dir / "dossier.json"
        dossier_path.write_text(json.dumps(dossier_stub, indent=2))

    # Update meta.json to mark the model as tournament-gated.
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["promotion_pipeline"] = "tournament_gated"
        meta["dossier_path"] = str(dossier_path.name)
        meta_path.write_text(json.dumps(meta, indent=2))

    return dossier_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        help="Parquet with a 'close' column + all FEATURES columns.",
    )
    parser.add_argument("--horizon-bars", type=int, default=15)
    parser.add_argument(
        "--bar-seconds",
        type=int,
        default=60,
        help="Duration of one bar in seconds; used to derive horizon_ns.",
    )
    parser.add_argument("--out-dir", default="models/gbm_predictor")
    parser.add_argument("--num-boost-round", type=int, default=500)
    parser.add_argument("--early-stopping-rounds", type=int, default=30)
    parser.add_argument(
        "--cv-folds",
        type=int,
        default=0,
        help=(
            "If > 0, run expanding-window walk-forward CV with this many folds "
            "and refit the final model on all rows for median(best_iter) rounds. "
            "If 0 (default), use the legacy 80/20 holdout split."
        ),
    )
    parser.add_argument(
        "--purge-bars",
        type=int,
        default=-1,
        help=(
            "Bars to drop between train end and val start (anti-leakage for "
            "forward-return labels).  -1 (default) means use --horizon-bars."
        ),
    )
    parser.add_argument(
        "--embargo-bars",
        type=int,
        default=0,
        help="Bars to skip after each validation window (reserved for sliding CV).",
    )
    parser.add_argument(
        "--create-dossier",
        action="store_true",
        default=False,
        help=(
            "If set, build a DossierRecord JSON (dossier.json) alongside "
            "the model artifacts so the model can be imported into the "
            "DossierStore and scored by the tournament. Without this flag "
            "(default), the model is 'operator_trusted' — it deploys "
            "directly to the agent without tournament gating. See "
            "docs/TRAINING_ANALYSIS.md finding F5."
        ),
    )
    parser.add_argument(
        "--checkpoint-dir",
        default=None,
        help="Directory to save per-fold model checkpoints (default: <out-dir>/checkpoints)",
    )
    parser.add_argument(
        "--resume-from-fold",
        type=int,
        default=None,
        help="Resume CV from this fold index (skips folds 0..N-1, loads fold N-1 checkpoint)",
    )
    args = parser.parse_args(argv)

    df = pl.read_parquet(args.input)
    X, y = build_dataset(df, horizon_bars=args.horizon_bars, feature_names=FEATURES)
    training_request = {
        "model_name": pathlib.Path(args.out_dir).name,
        "input_path": args.input,
        "horizon_bars": int(args.horizon_bars),
        "bar_seconds": int(args.bar_seconds),
        "cv_folds": int(args.cv_folds),
        "purge_bars": int(args.purge_bars),
        "embargo_bars": int(args.embargo_bars),
        "num_boost_round": int(args.num_boost_round),
        "early_stopping_rounds": int(args.early_stopping_rounds),
    }

    if args.cv_folds > 0:
        purge_bars = args.purge_bars if args.purge_bars >= 0 else args.horizon_bars
        out_dir = pathlib.Path(args.out_dir)
        checkpoint_dir = (
            pathlib.Path(args.checkpoint_dir)
            if args.checkpoint_dir is not None
            else out_dir / "checkpoints"
        )
        folds = walk_forward_cv(
            X,
            y,
            n_folds=args.cv_folds,
            purge_bars=purge_bars,
            embargo_bars=args.embargo_bars,
            num_boost_round=args.num_boost_round,
            early_stopping_rounds=args.early_stopping_rounds,
            checkpoint_dir=checkpoint_dir,
            resume_from_fold=args.resume_from_fold,
        )
        cv_summary = summarize_cv(folds)
        median_iter = cv_summary.get("median_best_iter", args.num_boost_round)
        model = train_full(X, y, num_boost_round=median_iter)
        train_meta: dict[str, Any] = {
            "eval_mode": "walk_forward",
            "cv_folds": folds,
            "cv_summary": cv_summary,
            "final_train_rows": len(X),
            "final_num_boost_round": int(median_iter),
            "purge_bars": int(purge_bars),
            "embargo_bars": int(args.embargo_bars),
            "training_input_path": args.input,
            "training_request": training_request,
        }
        save_artifacts(
            model,
            out_dir=out_dir,
            feature_names=FEATURES,
            horizon_bars=args.horizon_bars,
            bar_seconds=args.bar_seconds,
            extra_meta=train_meta,
        )
        if args.create_dossier:
            dossier_path = create_dossier(
                model,
                out_dir=out_dir,
                feature_names=FEATURES,
                horizon_bars=args.horizon_bars,
                bar_seconds=args.bar_seconds,
                training_meta=train_meta,
                input_path=args.input,
            )
            print(f"Dossier written to {dossier_path}")
        model_path = out_dir / "model.txt"
        artifact_manifest = ArtifactManifest(
            artifact_id=f"gbm-{out_dir.name}",
            sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
            size_bytes=model_path.stat().st_size,
            uri=str(model_path),
            model_family="gbm",
            created_at_ns=time.time_ns(),
            feature_schema_hash=_compute_feature_schema_hash(FEATURES),
            label_schema_hash=hashlib.sha256(
                f"binary_forward_return_{args.horizon_bars}bars".encode()
            ).hexdigest(),
            code_git_sha=None,  # filled by CI if available
        )
        artifact_manifest_path = out_dir / "artifact_manifest.json"
        artifact_manifest_path.write_text(artifact_manifest.model_dump_json(indent=2))
        print(
            f"Saved {args.out_dir} "
            f"(eval=walk_forward, n_folds={cv_summary.get('n_folds')}, "
            f"mean_auc={cv_summary.get('mean_auc')}, std_auc={cv_summary.get('std_auc')}, "
            f"final_rows={len(X)}, final_rounds={median_iter})"
        )
        return

    # Legacy single-holdout path (back-compat).
    model, holdout_meta = train_booster(
        X,
        y,
        num_boost_round=args.num_boost_round,
        early_stopping_rounds=args.early_stopping_rounds,
    )
    holdout_meta["eval_mode"] = "holdout_80_20"
    holdout_meta["training_input_path"] = args.input
    holdout_meta["training_request"] = training_request
    save_artifacts(
        model,
        out_dir=pathlib.Path(args.out_dir),
        feature_names=FEATURES,
        horizon_bars=args.horizon_bars,
        bar_seconds=args.bar_seconds,
        extra_meta=holdout_meta,
    )
    if args.create_dossier:
        dossier_path = create_dossier(
            model,
            out_dir=pathlib.Path(args.out_dir),
            feature_names=FEATURES,
            horizon_bars=args.horizon_bars,
            bar_seconds=args.bar_seconds,
            training_meta=holdout_meta,
            input_path=args.input,
        )
        print(f"Dossier written to {dossier_path}")
    print(
        f"Saved {args.out_dir} "
        f"(eval=holdout_80_20, train_rows={holdout_meta['train_rows']}, "
        f"val_rows={holdout_meta['val_rows']}, best_iter={holdout_meta['best_iter']}, "
        f"best_auc={holdout_meta['best_auc']})"
    )


if __name__ == "__main__":
    main()
