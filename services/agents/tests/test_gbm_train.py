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
)


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
