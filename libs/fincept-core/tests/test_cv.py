"""Tests for ``fincept_core.datasets.cv`` (todo 17).

These tests are a port of the ``make_folds`` / ``Fold`` coverage from
``services/backtester/tests/test_walk_forward.py`` (the pure index-math
tier -- no I/O, no LightGBM) plus dedicated coverage for the
nanosecond-resolution ``derive_walk_forward_window`` mirror of
``services/quant_foundry/src/quant_foundry/training_manifest.py``.

The goal is to lock the shared CV utility to the exact behaviour of the
originals so the backtester, agents trainer and quant_foundry manifest
builder can all delegate to it without behaviour drift.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fincept_core.datasets import (
    Fold,
    WalkForwardWindow,
    derive_walk_forward_window,
    fold_iter_to_dicts,
    make_cpcv_folds,
    make_folds,
)

# --------------------------------------------------------------------------- #
# make_folds                                                                   #
# --------------------------------------------------------------------------- #


class TestMakeFolds:
    def test_basic_three_folds_no_purge(self) -> None:
        folds = make_folds(
            n_bars=100,
            n_folds=3,
            train_min_bars=40,
            val_bars=10,
            purge_bars=0,
            embargo_bars=0,
        )
        assert len(folds) == 3
        # Expanding window: train_start always 0, train_end grows by val_bars.
        assert all(f.train_start == 0 for f in folds)
        assert [f.train_end for f in folds] == [40, 50, 60]
        # Val windows are contiguous when purge=embargo=0.
        assert [(f.val_start, f.val_end) for f in folds] == [
            (40, 50),
            (50, 60),
            (60, 70),
        ]

    def test_purge_creates_gap_between_train_and_val(self) -> None:
        folds = make_folds(
            n_bars=200,
            n_folds=2,
            train_min_bars=50,
            val_bars=20,
            purge_bars=5,
            embargo_bars=0,
        )
        f0, f1 = folds
        # Fold 0: train [0..50), val [55..75)
        assert (f0.train_end, f0.val_start, f0.val_end) == (50, 55, 75)
        # Fold 1: train_end = previous val_end + embargo (0) = 75; val [80..100)
        assert (f1.train_end, f1.val_start, f1.val_end) == (75, 80, 100)
        # Purge gap is exactly purge_bars between train_end and val_start.
        assert f0.val_start - f0.train_end == 5
        assert f1.val_start - f1.train_end == 5

    def test_embargo_creates_gap_between_folds(self) -> None:
        folds = make_folds(
            n_bars=200,
            n_folds=2,
            train_min_bars=50,
            val_bars=20,
            purge_bars=0,
            embargo_bars=10,
        )
        f0, f1 = folds
        # Embargo means fold 1's train_end starts after fold 0's val_end + 10.
        assert f1.train_end == f0.val_end + 10

    def test_val_windows_are_disjoint(self) -> None:
        folds = make_folds(
            n_bars=500,
            n_folds=5,
            train_min_bars=100,
            val_bars=30,
            purge_bars=2,
            embargo_bars=3,
        )
        ranges = [(f.val_start, f.val_end) for f in folds]
        for i in range(len(ranges) - 1):
            a_end = ranges[i][1]
            b_start = ranges[i + 1][0]
            assert a_end <= b_start, f"{a_end=} overlaps {b_start=}"

    def test_indices_are_monotonically_increasing(self) -> None:
        folds = make_folds(
            n_bars=300,
            n_folds=4,
            train_min_bars=50,
            val_bars=20,
            purge_bars=1,
            embargo_bars=2,
        )
        for f in folds:
            assert f.train_start < f.train_end < f.val_start < f.val_end
        for i in range(len(folds) - 1):
            prev, cur = folds[i], folds[i + 1]
            assert prev.train_end <= cur.train_end
            assert prev.val_start < cur.val_start

    def test_fold_indices_set_in_order(self) -> None:
        folds = make_folds(n_bars=200, n_folds=4, train_min_bars=50, val_bars=20)
        assert [f.index for f in folds] == [0, 1, 2, 3]

    def test_happy_path_plan_scenario(self) -> None:
        """The exact happy-path scenario from the plan QA section."""
        folds = make_folds(
            n_bars=1000,
            n_folds=5,
            train_min_bars=200,
            val_bars=100,
            purge_bars=20,
            embargo_bars=10,
        )
        assert len(folds) == 5
        train_ends = [f.train_end for f in folds]
        assert train_ends == sorted(train_ends)
        # First fold: train [0..200), purge 20, val [220..320).
        f0 = folds[0]
        assert (f0.train_start, f0.train_end, f0.val_start, f0.val_end) == (
            0,
            200,
            220,
            320,
        )
        # Each subsequent train_end grows by purge + val + embargo = 130.
        assert train_ends == [200, 330, 460, 590, 720]

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"n_folds": 0, "train_min_bars": 50, "val_bars": 10}, "n_folds"),
            (
                {"n_folds": 2, "train_min_bars": 0, "val_bars": 10},
                "train_min_bars",
            ),
            ({"n_folds": 2, "train_min_bars": 50, "val_bars": 0}, "val_bars"),
            (
                {
                    "n_folds": 2,
                    "train_min_bars": 50,
                    "val_bars": 10,
                    "purge_bars": -1,
                },
                "purge_bars",
            ),
            (
                {
                    "n_folds": 2,
                    "train_min_bars": 50,
                    "val_bars": 10,
                    "embargo_bars": -1,
                },
                "embargo_bars",
            ),
        ],
    )
    def test_rejects_invalid_args(self, kwargs: dict[str, int], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            make_folds(n_bars=500, **kwargs)

    def test_rejects_too_few_bars_exact_message(self) -> None:
        """The too-few-bars error text must match the original verbatim."""
        with pytest.raises(
            ValueError,
            match=(
                r"need at least 60 bars for 1 folds with "
                r"train_min=50, val=10, purge=0, embargo=0; got 30"
            ),
        ):
            # 30 bars can't fit 50 train + 1 fold * 10 val = 60.
            make_folds(n_bars=30, n_folds=1, train_min_bars=50, val_bars=10)

    def test_rejects_too_few_bars(self) -> None:
        with pytest.raises(ValueError, match="need at least"):
            make_folds(n_bars=30, n_folds=1, train_min_bars=50, val_bars=10)


# --------------------------------------------------------------------------- #
# Fold (frozen Pydantic model)                                                 #
# --------------------------------------------------------------------------- #


class TestFold:
    def test_fold_is_frozen(self) -> None:
        f = Fold(
            index=0,
            train_start=0,
            train_end=40,
            val_start=40,
            val_end=50,
        )
        with pytest.raises(ValidationError):
            f.train_end = 99  # type: ignore[misc]

    def test_fold_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            Fold(
                index=0,
                train_start=0,
                train_end=40,
                val_start=40,
                val_end=50,
                extra="nope",  # type: ignore[call-arg]
            )

    def test_fold_derived_properties(self) -> None:
        f = Fold(
            index=1,
            train_start=0,
            train_end=40,
            val_start=45,
            val_end=55,
        )
        assert f.train_bars == 40
        assert f.val_bars == 10


# --------------------------------------------------------------------------- #
# fold_iter_to_dicts                                                           #
# --------------------------------------------------------------------------- #


def test_fold_iter_to_dicts_roundtrip() -> None:
    folds = make_folds(
        n_bars=100,
        n_folds=2,
        train_min_bars=40,
        val_bars=10,
    )
    dicts = fold_iter_to_dicts(folds)
    assert isinstance(dicts, list)
    assert len(dicts) == 2
    assert dicts[0] == {
        "index": 0,
        "train_start": 0,
        "train_end": 40,
        "val_start": 40,
        "val_end": 50,
    }
    # Each entry is JSON-safe plain ints.
    for d in dicts:
        assert all(isinstance(v, int) for v in d.values())


def test_fold_iter_to_dicts_empty() -> None:
    assert fold_iter_to_dicts([]) == []


# --------------------------------------------------------------------------- #
# derive_walk_forward_window                                                   #
# --------------------------------------------------------------------------- #


class TestDeriveWalkForwardWindow:
    def test_basic_layout(self) -> None:
        # train=100, val=50, test=25, horizon=10, as_of=1000.
        w = derive_walk_forward_window(
            train_window_ns=100,
            val_window_ns=50,
            test_window_ns=25,
            label_horizon_ns=10,
            as_of_ts=1000,
        )
        assert w.test_end == 1000
        assert w.test_start == 975
        assert w.val_end == 975 - 10  # 965
        assert w.val_start == 965 - 50  # 915
        assert w.train_end == 915 - 10  # 905
        assert w.train_start == 905 - 100  # 805
        assert w.label_horizon_ns == 10
        # Ordering invariant from the docstring.
        assert w.train_start < w.train_end <= w.val_start < w.val_end <= w.test_start < w.test_end

    def test_to_dict_roundtrip(self) -> None:
        w = derive_walk_forward_window(
            train_window_ns=100,
            val_window_ns=50,
            test_window_ns=25,
            label_horizon_ns=10,
            as_of_ts=1000,
        )
        d = w.to_dict()
        assert d == {
            "train_start": 805,
            "train_end": 905,
            "val_start": 915,
            "val_end": 965,
            "test_start": 975,
            "test_end": 1000,
            "label_horizon_ns": 10,
        }

    def test_rejects_zero_or_negative_windows(self) -> None:
        with pytest.raises(ValueError, match="all window lengths must be > 0"):
            derive_walk_forward_window(
                train_window_ns=0,
                val_window_ns=50,
                test_window_ns=25,
                label_horizon_ns=10,
                as_of_ts=1000,
            )
        with pytest.raises(ValueError, match="all window lengths must be > 0"):
            derive_walk_forward_window(
                train_window_ns=100,
                val_window_ns=-1,
                test_window_ns=25,
                label_horizon_ns=10,
                as_of_ts=1000,
            )

    def test_rejects_non_positive_label_horizon(self) -> None:
        with pytest.raises(ValueError, match="label_horizon_ns must be > 0"):
            derive_walk_forward_window(
                train_window_ns=100,
                val_window_ns=50,
                test_window_ns=25,
                label_horizon_ns=0,
                as_of_ts=1000,
            )
        with pytest.raises(ValueError, match="label_horizon_ns must be > 0"):
            derive_walk_forward_window(
                train_window_ns=100,
                val_window_ns=50,
                test_window_ns=25,
                label_horizon_ns=-5,
                as_of_ts=1000,
            )

    def test_rejects_train_start_negative(self) -> None:
        # as_of_ts too small to accommodate the windows + horizons.
        with pytest.raises(ValueError, match="train_window_ns is too long"):
            derive_walk_forward_window(
                train_window_ns=10_000,
                val_window_ns=50,
                test_window_ns=25,
                label_horizon_ns=10,
                as_of_ts=1000,
            )

    def test_window_is_frozen(self) -> None:
        w = derive_walk_forward_window(
            train_window_ns=100,
            val_window_ns=50,
            test_window_ns=25,
            label_horizon_ns=10,
            as_of_ts=1000,
        )
        with pytest.raises(ValidationError):
            w.train_start = 0  # type: ignore[misc]

    def test_window_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            WalkForwardWindow(
                train_start=0,
                train_end=10,
                val_start=20,
                val_end=30,
                test_start=40,
                test_end=50,
                label_horizon_ns=5,
                extra="nope",  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------- #
# make_cpcv_folds (Combinatorial Purged Cross-Validation)                     #
# --------------------------------------------------------------------------- #


class TestMakeCPCVFolds:
    """Tests for CPCV fold generation (Tier 2.1)."""

    def test_count_is_combination(self) -> None:
        """C(N, P) folds are generated."""
        folds = make_cpcv_folds(120, n_groups=6, n_val_groups=2, purge_bars=0)
        # C(6, 2) = 15
        assert len(folds) == 15

    def test_count_n_val_1(self) -> None:
        """N folds when P=1 (leave-one-group-out)."""
        folds = make_cpcv_folds(60, n_groups=6, n_val_groups=1, purge_bars=0)
        assert len(folds) == 6

    def test_val_groups_are_unique_combinations(self) -> None:
        """Each fold has a distinct val_groups tuple."""
        folds = make_cpcv_folds(120, n_groups=6, n_val_groups=2, purge_bars=0)
        val_tuples = {f.val_groups for f in folds}
        assert len(val_tuples) == len(folds)

    def test_no_train_val_overlap(self) -> None:
        """Training and validation ranges must never overlap."""
        folds = make_cpcv_folds(100, n_groups=5, n_val_groups=2, purge_bars=3)
        for f in folds:
            train_bars = set()
            for s, e in f.train_ranges:
                train_bars.update(range(s, e))
            val_bars = set()
            for s, e in f.val_ranges:
                val_bars.update(range(s, e))
            assert not train_bars & val_bars, f"overlap in fold {f.index}"

    def test_purge_removes_bars_at_boundary(self) -> None:
        """Purge_bars removes bars from training adjacent to validation."""
        folds = make_cpcv_folds(60, n_groups=4, n_val_groups=1, purge_bars=5)
        # With P=1, fold 0 has val_groups=(0,), training = groups 1,2,3
        # Group 0 is [0, 15), group 1 starts at 15.
        # Purge 5 bars from start of group 1 → training starts at 20.
        f0 = folds[0]
        assert f0.val_groups == (0,)
        first_train_start = f0.train_ranges[0][0]
        assert first_train_start == 20  # 15 + 5 purge

    def test_purge_at_end_of_training_block(self) -> None:
        """Purge removes bars at the end of a training block before val."""
        folds = make_cpcv_folds(60, n_groups=4, n_val_groups=1, purge_bars=5)
        # Fold 3 has val_groups=(3,), training = groups 0,1,2
        # Group 2 ends at 45, group 3 starts at 45.
        # Purge 5 bars from end of group 2 → training ends at 40.
        f3 = folds[3]
        assert f3.val_groups == (3,)
        last_train_end = f3.train_ranges[-1][1]
        assert last_train_end == 40  # 45 - 5 purge

    def test_scattered_val_groups(self) -> None:
        """Non-adjacent val groups produce non-contiguous training."""
        folds = make_cpcv_folds(60, n_groups=4, n_val_groups=2, purge_bars=0)
        # Find fold with val_groups=(0, 2) — training is groups 1 and 3
        # (two separate ranges).
        target = next(f for f in folds if f.val_groups == (0, 2))
        assert len(target.train_ranges) == 2

    def test_adjacent_training_ranges_merge(self) -> None:
        """Adjacent training blocks merge into a single range."""
        folds = make_cpcv_folds(60, n_groups=4, n_val_groups=1, purge_bars=0)
        # Fold 0: val=(0,), training = groups 1,2,3 (all contiguous)
        f0 = folds[0]
        assert len(f0.train_ranges) == 1
        assert f0.train_ranges[0] == (15, 60)

    def test_train_bars_property(self) -> None:
        """train_bars sums all training range lengths."""
        folds = make_cpcv_folds(100, n_groups=5, n_val_groups=2, purge_bars=3)
        for f in folds:
            expected = sum(e - s for s, e in f.train_ranges)
            assert f.train_bars == expected

    def test_val_bars_property(self) -> None:
        """val_bars sums all validation range lengths."""
        folds = make_cpcv_folds(100, n_groups=5, n_val_groups=2, purge_bars=3)
        for f in folds:
            expected = sum(e - s for s, e in f.val_ranges)
            assert f.val_bars == expected

    def test_every_bar_in_val_exactly_once_per_group(self) -> None:
        """Each bar appears in validation in exactly C(N-1, P-1) folds."""
        n_groups, n_val_groups = 5, 2
        folds = make_cpcv_folds(100, n_groups=n_groups, n_val_groups=n_val_groups, purge_bars=0)
        from math import comb

        expected_count = comb(n_groups - 1, n_val_groups - 1)
        bar_val_count: dict[int, int] = {}
        for f in folds:
            for s, e in f.val_ranges:
                for b in range(s, e):
                    bar_val_count[b] = bar_val_count.get(b, 0) + 1
        # Every bar should appear in exactly expected_count folds
        for b, count in bar_val_count.items():
            assert count == expected_count, f"bar {b} in {count} folds, expected {expected_count}"

    def test_invalid_n_groups(self) -> None:
        with pytest.raises(ValueError, match="n_groups must be >= 2"):
            make_cpcv_folds(100, n_groups=1, n_val_groups=1)

    def test_invalid_n_val_groups_zero(self) -> None:
        with pytest.raises(ValueError, match="n_val_groups must be in"):
            make_cpcv_folds(100, n_groups=4, n_val_groups=0)

    def test_invalid_n_val_groups_equal(self) -> None:
        """n_val_groups must be < n_groups."""
        with pytest.raises(ValueError, match="n_val_groups must be in"):
            make_cpcv_folds(100, n_groups=4, n_val_groups=4)

    def test_invalid_purge_negative(self) -> None:
        with pytest.raises(ValueError, match="purge_bars must be >= 0"):
            make_cpcv_folds(100, n_groups=4, n_val_groups=1, purge_bars=-1)

    def test_too_few_bars(self) -> None:
        with pytest.raises(ValueError, match="need at least"):
            make_cpcv_folds(3, n_groups=4, n_val_groups=1)

    def test_fold_is_frozen(self) -> None:
        """CPCVFold is immutable."""
        folds = make_cpcv_folds(60, n_groups=4, n_val_groups=1, purge_bars=0)
        with pytest.raises(Exception):
            folds[0].index = 99  # type: ignore[misc]

    def test_fold_index_sequential(self) -> None:
        """Fold indices are 0, 1, 2, ... in combination order."""
        folds = make_cpcv_folds(60, n_groups=4, n_val_groups=2, purge_bars=0)
        assert [f.index for f in folds] == list(range(len(folds)))
