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

Walk-forward / purged CV is intentionally NOT implemented here - that's
TASK-023.  This trainer does a simple time-ordered 80/20 holdout split,
suitable for a sanity-check baseline but not a production model.  Use
the walk-forward pipeline once it lands.
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
    args = parser.parse_args(argv)

    df = pl.read_parquet(args.input)
    X, y = build_dataset(df, horizon_bars=args.horizon_bars, feature_names=FEATURES)
    model, train_meta = train_booster(
        X,
        y,
        num_boost_round=args.num_boost_round,
        early_stopping_rounds=args.early_stopping_rounds,
    )
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
        f"(train_rows={train_meta['train_rows']}, val_rows={train_meta['val_rows']}, "
        f"best_iter={train_meta['best_iter']}, best_auc={train_meta['best_auc']})"
    )


if __name__ == "__main__":
    main()
