"""Tests for agents.gbm_predictor.train."""

from __future__ import annotations

import json
import pathlib

import lightgbm as lgb
import numpy as np
import polars as pl
import pytest

from agents.gbm_predictor.train import (
    build_dataset,
    save_artifacts,
    train_booster,
    walk_forward_splits,
)
from fincept_core.datasets import make_folds


def _synthetic_frame(rows: int = 1000, *, seed: int = 0) -> pl.DataFrame:
    """Build a polars frame with a 'close' column + 10 features.

    Close is a slow-moving random walk so forward returns are real;
    features are random noise so the trained model has no real edge -
    we only test the *shape* of training output, not its accuracy."""
    rng = np.random.default_rng(seed)
    feature_cols = {
        name: rng.normal(0, 1, rows)
        for name in [
            "ret_1m",
            "ret_5m",
            "ret_15m",
            "ret_60m",
            "rv_5m",
            "rv_30m",
            "mom_z_30m",
            "mom_z_240m",
            "book_imbalance_1",
            "spread_bps",
        ]
    }
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, rows)))
    return pl.DataFrame({"close": close, **feature_cols})


# ---------------------------------------------------------------------------
# build_dataset
# ---------------------------------------------------------------------------


def test_build_dataset_shapes_match() -> None:
    df = _synthetic_frame(rows=500)
    feature_names = ["ret_1m", "ret_5m", "rv_5m"]
    X, y = build_dataset(df, horizon_bars=5, feature_names=feature_names)
    assert X.shape[0] == y.shape[0]
    assert X.shape[1] == len(feature_names)
    assert set(np.unique(y)) <= {0, 1}


def test_build_dataset_drops_horizon_tail() -> None:
    """Last horizon_bars rows have null forward returns -> dropped."""
    df = _synthetic_frame(rows=100)
    feature_names = ["ret_1m"]
    X, y = build_dataset(df, horizon_bars=10, feature_names=feature_names)
    assert X.shape[0] == 90  # 100 - 10


def test_build_dataset_rejects_missing_close_column() -> None:
    df = _synthetic_frame(rows=100).drop("close")
    with pytest.raises(ValueError, match="close"):
        build_dataset(df, horizon_bars=5, feature_names=["ret_1m"])


def test_build_dataset_rejects_missing_feature_columns() -> None:
    df = _synthetic_frame(rows=100)
    with pytest.raises(ValueError, match="not_a_feature"):
        build_dataset(df, horizon_bars=5, feature_names=["not_a_feature"])


# ---------------------------------------------------------------------------
# train_booster
# ---------------------------------------------------------------------------


def test_train_booster_produces_loadable_model() -> None:
    rng = np.random.default_rng(0)
    X = rng.normal(0, 1, (500, 5))
    y = (X[:, 0] > 0).astype(int)  # easy linear signal

    model, meta = train_booster(
        X, y, num_boost_round=50, early_stopping_rounds=10, val_fraction=0.2
    )
    assert isinstance(model, lgb.Booster)
    assert meta["train_rows"] == 400
    assert meta["val_rows"] == 100
    assert meta["best_iter"] >= 1
    # AUC on a clean signal should beat random.
    assert (meta["best_auc"] or 0) > 0.6


def test_train_booster_rejects_invalid_val_fraction() -> None:
    X = np.zeros((10, 2))
    y = np.zeros(10, dtype=int)
    with pytest.raises(ValueError, match="val_fraction"):
        train_booster(X, y, val_fraction=0.0)
    with pytest.raises(ValueError, match="val_fraction"):
        train_booster(X, y, val_fraction=1.0)


# ---------------------------------------------------------------------------
# save_artifacts
# ---------------------------------------------------------------------------


def test_save_artifacts_writes_model_and_meta(tmp_path: pathlib.Path) -> None:
    rng = np.random.default_rng(0)
    X = rng.normal(0, 1, (200, 3))
    y = (X[:, 0] > 0).astype(int)
    model, train_meta = train_booster(X, y, num_boost_round=20, early_stopping_rounds=5)

    out_dir = tmp_path / "model_out"
    save_artifacts(
        model,
        out_dir=out_dir,
        feature_names=["a", "b", "c"],
        horizon_bars=15,
        bar_seconds=60,
        extra_meta=train_meta,
    )

    assert (out_dir / "model.txt").is_file()
    assert (out_dir / "meta.json").is_file()

    meta = json.loads((out_dir / "meta.json").read_text())
    assert meta["features"] == ["a", "b", "c"]
    assert meta["horizon_bars"] == 15
    assert meta["bar_seconds"] == 60
    # 15 bars * 60s * 1e9 ns
    assert meta["horizon_ns"] == 15 * 60 * 1_000_000_000
    assert "trained_at" in meta
    assert meta["train_rows"] == train_meta["train_rows"]

    # Reload the model and check it's usable.
    reloaded = lgb.Booster(model_file=str(out_dir / "model.txt"))
    preds = reloaded.predict(X[:5])
    assert len(preds) == 5
    assert all(0.0 <= p <= 1.0 for p in preds)


# --------------------------------------------------------------------------- #
# walk_forward_splits -> shared make_folds delegation (todo 18)               #
# --------------------------------------------------------------------------- #


def _original_walk_forward_splits(
    n_rows: int,
    *,
    n_folds: int,
    purge_bars: int = 0,
    embargo_bars: int = 0,
    min_train_rows: int = 1,
) -> list[tuple[slice, slice]]:
    """Verbatim copy of the pre-migration ``walk_forward_splits`` math.

    Kept here as a golden reference so the regression test can prove the
    migration to ``fincept_core.datasets.cv.make_folds`` did not change
    any fold boundary.  This function is intentionally *not* imported
    from the module under test -- it is the frozen original.
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

    splits: list[tuple[slice, slice]] = []
    for i in range(n_folds):
        val_start = (i + 1) * fold_size
        val_end = val_start + fold_size if i < n_folds - 1 else n_rows
        train_end = val_start - purge_bars
        if train_end < min_train_rows or val_end <= val_start:
            continue
        splits.append((slice(0, train_end), slice(val_start, val_end)))
    if not splits:
        raise ValueError(
            f"no usable folds (n_rows={n_rows}, n_folds={n_folds}, purge_bars={purge_bars}, "
            f"min_train_rows={min_train_rows})"
        )
    return splits


def _splits_to_boundaries(
    splits: list[tuple[slice, slice]],
) -> list[tuple[int, int, int, int]]:
    """Reduce (train_slice, val_slice) to comparable (0, train_end, val_start, val_end)."""
    return [(s.start, s.stop, v.start, v.stop) for s, v in splits]


def test_walk_forward_splits_matches_shared_make_folds() -> None:
    """The delegated ``walk_forward_splits`` produces identical fold
    boundaries to the original hand-rolled math for the canonical
    trainer fixture (n=10000, n_folds=5, purge_bars=15)."""
    n_rows, n_folds, purge_bars = 10000, 5, 15

    reference = _original_walk_forward_splits(
        n_rows, n_folds=n_folds, purge_bars=purge_bars
    )
    delegated = walk_forward_splits(n_rows, n_folds=n_folds, purge_bars=purge_bars)

    assert _splits_to_boundaries(delegated) == _splits_to_boundaries(reference)
    # Sanity: 5 folds, anchored at 0, val windows at multiples of fold_size.
    fold_size = n_rows // (n_folds + 1)
    assert len(delegated) == n_folds
    for i, (train_slice, val_slice) in enumerate(delegated):
        assert train_slice.start == 0
        assert val_slice.start == (i + 1) * fold_size
        assert train_slice.stop == val_slice.start - purge_bars
        if i < n_folds - 1:
            assert val_slice.stop == val_slice.start + fold_size
        else:
            assert val_slice.stop == n_rows

    # The delegated boundaries must also line up with the raw Fold
    # objects produced by ``make_folds`` (purge applied locally, last
    # fold extended to n_rows) -- this is the core delegation contract.
    folds = make_folds(
        n_rows,
        n_folds=n_folds,
        train_min_bars=fold_size,
        val_bars=fold_size,
        purge_bars=0,
        embargo_bars=0,
    )
    for fold, (train_slice, val_slice) in zip(folds, delegated):
        assert val_slice.start == fold.val_start
        assert train_slice.stop == fold.val_start - purge_bars


def test_walk_forward_splits_embargo_is_noop() -> None:
    """``embargo_bars`` is documented as a no-op for the anchored
    expansion (train.py:186-189).  Passing a non-zero embargo must not
    change any fold boundary -- the shared ``make_folds`` accepts it but
    the trainer's local logic still ignores it (preserved behavior)."""
    n_rows, n_folds, purge_bars = 10000, 5, 15
    without = walk_forward_splits(n_rows, n_folds=n_folds, purge_bars=purge_bars, embargo_bars=0)
    with_embargo = walk_forward_splits(
        n_rows, n_folds=n_folds, purge_bars=purge_bars, embargo_bars=25
    )
    assert _splits_to_boundaries(with_embargo) == _splits_to_boundaries(without)


def test_walk_forward_splits_too_few_rows_raises_same_valueerror() -> None:
    """Failure path: n_bars < required raises the same ValueError
    (string match) before and after the migration."""
    with pytest.raises(ValueError, match="insufficient rows"):
        walk_forward_splits(3, n_folds=10, purge_bars=0)
    # The original math raises the identical message.
    with pytest.raises(ValueError, match="insufficient rows"):
        _original_walk_forward_splits(3, n_folds=10, purge_bars=0)
