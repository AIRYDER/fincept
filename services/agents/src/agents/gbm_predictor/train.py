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
import json
import pathlib
import time
from typing import Any

import lightgbm as lgb
import numpy as np
import polars as pl

from agents.gbm_predictor.features import FEATURES
from fincept_core.datasets import make_folds


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
) -> list[dict[str, Any]]:
    """Run expanding-window walk-forward CV; return per-fold metrics.

    Each fold trains a fresh booster on the train slice with early
    stopping against the val slice and records ``train_rows``,
    ``val_rows``, ``best_iter``, ``best_auc``.  No model state leaks
    between folds (every booster is local).

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
                    "train_rows": int(len(X_tr)),
                    "val_rows": int(len(X_va)),
                    "best_iter": None,
                    "best_auc": None,
                    "reason_skipped": "single-class fold",
                }
            )
            continue
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
        fold_metrics.append(
            {
                "fold": fold_idx,
                "train_rows": int(len(X_tr)),
                "val_rows": int(len(X_va)),
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
    """Write ``model.txt`` + ``meta.json`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_dir / "model.txt"))
    horizon_ns = horizon_bars * bar_seconds * 1_000_000_000
    meta: dict[str, Any] = {
        "features": feature_names,
        "horizon_bars": horizon_bars,
        "bar_seconds": bar_seconds,
        "horizon_ns": horizon_ns,
        "trained_at": int(time.time()),
    }
    if extra_meta:
        meta.update(extra_meta)
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))


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
        folds = walk_forward_cv(
            X,
            y,
            n_folds=args.cv_folds,
            purge_bars=purge_bars,
            embargo_bars=args.embargo_bars,
            num_boost_round=args.num_boost_round,
            early_stopping_rounds=args.early_stopping_rounds,
        )
        cv_summary = summarize_cv(folds)
        median_iter = cv_summary.get("median_best_iter", args.num_boost_round)
        model = train_full(X, y, num_boost_round=median_iter)
        train_meta: dict[str, Any] = {
            "eval_mode": "walk_forward",
            "cv_folds": folds,
            "cv_summary": cv_summary,
            "final_train_rows": int(len(X)),
            "final_num_boost_round": int(median_iter),
            "purge_bars": int(purge_bars),
            "embargo_bars": int(args.embargo_bars),
            "training_input_path": args.input,
            "training_request": training_request,
        }
        save_artifacts(
            model,
            out_dir=pathlib.Path(args.out_dir),
            feature_names=FEATURES,
            horizon_bars=args.horizon_bars,
            bar_seconds=args.bar_seconds,
            extra_meta=train_meta,
        )
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
    print(
        f"Saved {args.out_dir} "
        f"(eval=holdout_80_20, train_rows={holdout_meta['train_rows']}, "
        f"val_rows={holdout_meta['val_rows']}, best_iter={holdout_meta['best_iter']}, "
        f"best_auc={holdout_meta['best_auc']})"
    )


if __name__ == "__main__":
    main()
