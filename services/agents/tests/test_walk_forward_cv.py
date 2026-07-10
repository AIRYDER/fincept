"""Tests for walk-forward CV splitter + summary aggregator.

Covers:
  * fold count + monotonic chronological order
  * purge gap actually drops bars between train and val
  * purge=0 keeps train end touching val start (back-compat sanity)
  * insufficient rows / bad args raise
  * summarize_cv handles all-good, mixed, and all-skipped fold lists
  * end-to-end walk_forward_cv on a deterministic synthetic series
    confirms folds are scored and best_iter is recorded
"""

from __future__ import annotations

import numpy as np
import pytest

from agents.gbm_predictor.train import (
    summarize_cv,
    walk_forward_cv,
    walk_forward_splits,
)

# --------------------------------------------------------------------------- #
# walk_forward_splits                                                         #
# --------------------------------------------------------------------------- #


class TestWalkForwardSplits:
    def test_basic_layout(self) -> None:
        """6 folds on 600 rows -> fold_size=85, val_starts at 85,170,255,..."""
        n_rows, n_folds = 600, 6
        splits = walk_forward_splits(n_rows, n_folds=n_folds, purge_bars=0)
        assert len(splits) == n_folds
        fold_size = n_rows // (n_folds + 1)  # 85
        for i, (train_slice, val_slice) in enumerate(splits):
            assert train_slice.start == 0
            assert val_slice.start == (i + 1) * fold_size
            # Last fold extends to end-of-series.
            if i < n_folds - 1:
                assert val_slice.stop == val_slice.start + fold_size
            else:
                assert val_slice.stop == n_rows

    def test_purge_drops_train_end(self) -> None:
        """purge_bars=15 means train_end = val_start - 15."""
        splits = walk_forward_splits(600, n_folds=5, purge_bars=15)
        for train_slice, val_slice in splits:
            assert train_slice.stop == val_slice.start - 15
            # No overlap, no leakage zone touched by either side.
            assert train_slice.stop < val_slice.start

    def test_purge_zero_train_touches_val(self) -> None:
        splits = walk_forward_splits(600, n_folds=5, purge_bars=0)
        for train_slice, val_slice in splits:
            assert train_slice.stop == val_slice.start

    def test_chronological_monotonic(self) -> None:
        """Each successive fold's val window starts after the previous one."""
        splits = walk_forward_splits(600, n_folds=5, purge_bars=10)
        prev_val_start = -1
        for _, val_slice in splits:
            assert val_slice.start > prev_val_start
            prev_val_start = val_slice.start

    def test_train_window_grows(self) -> None:
        """Anchored expansion: every fold's train window is at least as
        large as the previous one."""
        splits = walk_forward_splits(600, n_folds=5, purge_bars=10)
        prev_train_len = 0
        for train_slice, _ in splits:
            train_len = train_slice.stop - train_slice.start
            assert train_len > prev_train_len
            prev_train_len = train_len

    def test_n_folds_too_small_rejected(self) -> None:
        with pytest.raises(ValueError, match="n_folds must be >= 2"):
            walk_forward_splits(100, n_folds=1, purge_bars=0)

    def test_too_few_rows_rejected(self) -> None:
        with pytest.raises(ValueError, match="insufficient rows"):
            walk_forward_splits(3, n_folds=10, purge_bars=0)

    def test_negative_purge_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            walk_forward_splits(600, n_folds=5, purge_bars=-1)

    def test_purge_too_large_drops_folds(self) -> None:
        """If purge_bars >= fold_size on the first fold, it's dropped."""
        # fold_size = 100 // 6 = 16; purge=20 wipes fold 0's train.
        splits = walk_forward_splits(100, n_folds=5, purge_bars=20)
        # Fold 0 had train_end = 16 - 20 = -4 < min_train_rows=1, dropped.
        assert len(splits) == 4

    def test_all_folds_purged_raises(self) -> None:
        """If purge is so big nothing is usable, raise."""
        with pytest.raises(ValueError, match="no usable folds"):
            walk_forward_splits(50, n_folds=5, purge_bars=100)


# --------------------------------------------------------------------------- #
# summarize_cv                                                                #
# --------------------------------------------------------------------------- #


class TestSummarizeCv:
    def test_basic_summary(self) -> None:
        folds = [
            {
                "fold": 0,
                "best_auc": 0.50,
                "best_iter": 100,
                "train_rows": 1,
                "val_rows": 1,
            },
            {
                "fold": 1,
                "best_auc": 0.60,
                "best_iter": 80,
                "train_rows": 1,
                "val_rows": 1,
            },
            {
                "fold": 2,
                "best_auc": 0.55,
                "best_iter": 120,
                "train_rows": 1,
                "val_rows": 1,
            },
        ]
        s = summarize_cv(folds)
        assert s["n_folds"] == 3
        assert s["n_scored"] == 3
        assert s["n_skipped"] == 0
        assert s["mean_auc"] == pytest.approx(0.55)
        assert s["min_auc"] == pytest.approx(0.50)
        assert s["max_auc"] == pytest.approx(0.60)
        assert s["std_auc"] > 0
        assert s["median_best_iter"] == 100

    def test_skipped_folds_excluded_from_stats(self) -> None:
        folds = [
            {
                "fold": 0,
                "best_auc": 0.55,
                "best_iter": 100,
                "train_rows": 1,
                "val_rows": 1,
            },
            {
                "fold": 1,
                "best_auc": None,
                "best_iter": None,
                "train_rows": 1,
                "val_rows": 1,
            },
        ]
        s = summarize_cv(folds)
        assert s["n_folds"] == 2
        assert s["n_scored"] == 1
        assert s["n_skipped"] == 1
        assert s["mean_auc"] == pytest.approx(0.55)
        # std is 0 for one sample (we use ddof=1 only if >1 fold).
        assert s["std_auc"] == 0.0

    def test_all_skipped(self) -> None:
        folds = [
            {
                "fold": 0,
                "best_auc": None,
                "best_iter": None,
                "train_rows": 1,
                "val_rows": 1,
            },
            {
                "fold": 1,
                "best_auc": None,
                "best_iter": None,
                "train_rows": 1,
                "val_rows": 1,
            },
        ]
        s = summarize_cv(folds)
        assert s["n_folds"] == 2
        assert s["n_scored"] == 0
        assert s["n_skipped"] == 2
        assert "mean_auc" not in s
        assert "median_best_iter" not in s


# --------------------------------------------------------------------------- #
# walk_forward_cv end-to-end (synthetic)                                      #
# --------------------------------------------------------------------------- #


class TestWalkForwardCvEndToEnd:
    @pytest.fixture
    def synthetic_xy(self) -> tuple[np.ndarray, np.ndarray]:
        """A simple linearly-separable dataset.

        Two informative features + 8 noise features so LightGBM has to
        do a tiny bit of work but every fold is healthy.
        """
        rng = np.random.default_rng(42)
        n = 400
        X = rng.normal(size=(n, 10))
        # Label = sign(X[:, 0] + X[:, 1] + small noise) > 0
        z = X[:, 0] + X[:, 1] + rng.normal(scale=0.5, size=n)
        y = (z > 0).astype(int)
        return X, y

    def test_runs_and_records_per_fold_metrics(
        self, synthetic_xy: tuple[np.ndarray, np.ndarray]
    ) -> None:
        X, y = synthetic_xy
        folds = walk_forward_cv(
            X,
            y,
            n_folds=4,
            purge_bars=5,
            num_boost_round=50,
            early_stopping_rounds=10,
        )
        assert len(folds) == 4
        for f in folds:
            # Real (non-skipped) folds populate every metric.
            assert f["best_auc"] is not None
            assert 0.0 <= f["best_auc"] <= 1.0
            assert f["best_iter"] is not None and f["best_iter"] > 0
            assert f["train_rows"] > 0 and f["val_rows"] > 0

    def test_summary_aggregates_real_run(
        self, synthetic_xy: tuple[np.ndarray, np.ndarray]
    ) -> None:
        X, y = synthetic_xy
        folds = walk_forward_cv(
            X,
            y,
            n_folds=4,
            purge_bars=5,
            num_boost_round=50,
            early_stopping_rounds=10,
        )
        s = summarize_cv(folds)
        # Linearly-separable signal -> mean_auc clearly > 0.5.
        assert s["mean_auc"] > 0.7
        # All four folds scored.
        assert s["n_scored"] == 4
        assert "median_best_iter" in s

    def test_single_class_fold_recorded_as_skipped(self) -> None:
        """If one fold's val window is entirely one class, it's recorded
        as skipped (best_auc=None) and the run continues."""
        # Construct y such that the LAST 1/5 is all-zeros; that fold's
        # val window will hit that block.
        n = 600
        rng = np.random.default_rng(0)
        X = rng.normal(size=(n, 10))
        y = (rng.uniform(size=n) > 0.5).astype(int)
        # Force the last 100 rows to be all-zero label.
        y[-100:] = 0
        folds = walk_forward_cv(
            X,
            y,
            n_folds=5,
            purge_bars=0,
            num_boost_round=20,
            early_stopping_rounds=5,
        )
        # At least the last fold should be skipped.
        skipped = [f for f in folds if f.get("best_auc") is None]
        assert len(skipped) >= 1
        assert all(f.get("reason_skipped") == "single-class fold" for f in skipped)
