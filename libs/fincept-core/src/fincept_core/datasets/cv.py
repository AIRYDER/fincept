"""Cross-validation utilities for the ML dataset evidence spine.

This module is a verbatim port of the walk-forward fold math that lived
in ``services/backtester/src/backtester/walk_forward.py`` (``Fold`` and
``make_folds``) plus the nanosecond-resolution walk-forward window
derivation from ``services/quant_foundry/src/quant_foundry/training_manifest.py``
(``WalkForwardWindow`` and ``derive_walk_forward_window``).

It is the *single* shared home for these algorithms so that the
backtester, the agents trainer and the quant_foundry manifest builder
all agree on what a "purged + embargoed expanding-window fold" means.

Design notes:

  * ``Fold`` and ``WalkForwardWindow`` are frozen Pydantic v2 models
    (the original implementations used ``@dataclass(frozen=True)``; we
    use Pydantic here for consistency with the rest of
    ``fincept_core.datasets`` and to gain JSON-schema support for
    free).
  * Validation is tightened so every guard raises ``ValueError`` (the
    originals raised ``ValueError`` already for ``make_folds``; the
    plan asks us to keep that).  No ``RuntimeError`` is raised.
  * No imports from ``services/backtester`` or
    ``services/quant_foundry`` -- this module is dependency-free apart
    from Pydantic.
"""

from __future__ import annotations

from itertools import combinations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "CPCVFold",
    "Fold",
    "WalkForwardWindow",
    "derive_walk_forward_window",
    "fold_iter_to_dicts",
    "make_cpcv_folds",
    "make_folds",
]


# --------------------------------------------------------------------------- #
# Fold splitting (bar-index space)                                            #
# --------------------------------------------------------------------------- #


class Fold(BaseModel):
    """Half-open index ranges into the canonical timestamp grid.

    All ranges are ``[start, end)``; ``end`` is exclusive so concatenation
    works cleanly with Python slicing.  Mirrors
    ``services/backtester/src/backtester/walk_forward.py:72-86``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    train_start: int
    train_end: int  # exclusive
    val_start: int
    val_end: int  # exclusive

    @property
    def train_bars(self) -> int:
        return self.train_end - self.train_start

    @property
    def val_bars(self) -> int:
        return self.val_end - self.val_start


def make_folds(
    n_bars: int,
    *,
    n_folds: int,
    train_min_bars: int,
    val_bars: int,
    purge_bars: int = 0,
    embargo_bars: int = 0,
) -> list[Fold]:
    """Build ``n_folds`` expanding-window folds over ``n_bars`` timestamps.

    Constraints checked up-front (so callers see a clean error before
    spending minutes training models):

      - ``n_folds >= 1``
      - ``train_min_bars >= 1``
      - ``val_bars >= 1``
      - ``purge_bars >= 0``, ``embargo_bars >= 0``
      - total bars must accommodate ``train_min_bars + n_folds *
        (purge_bars + val_bars) + (n_folds - 1) * embargo_bars``

    Returns folds ordered by ascending ``train_end``.  Successive folds
    share the same training start (``0``) and grow by ``val_bars +
    embargo_bars`` per step.
    """
    if n_folds < 1:
        raise ValueError(f"n_folds must be >= 1, got {n_folds}")
    if train_min_bars < 1:
        raise ValueError(f"train_min_bars must be >= 1, got {train_min_bars}")
    if val_bars < 1:
        raise ValueError(f"val_bars must be >= 1, got {val_bars}")
    if purge_bars < 0:
        raise ValueError(f"purge_bars must be >= 0, got {purge_bars}")
    if embargo_bars < 0:
        raise ValueError(f"embargo_bars must be >= 0, got {embargo_bars}")

    required = train_min_bars + n_folds * (purge_bars + val_bars) + (n_folds - 1) * embargo_bars
    if n_bars < required:
        raise ValueError(
            f"need at least {required} bars for {n_folds} folds with "
            f"train_min={train_min_bars}, val={val_bars}, purge={purge_bars}, "
            f"embargo={embargo_bars}; got {n_bars}"
        )

    folds: list[Fold] = []
    train_end = train_min_bars
    for k in range(n_folds):
        val_start = train_end + purge_bars
        val_end = val_start + val_bars
        if val_end > n_bars:
            raise ValueError(
                f"fold {k} val_end={val_end} exceeds n_bars={n_bars} "
                "(internal arithmetic error — please file a bug)"
            )
        folds.append(
            Fold(
                index=k,
                train_start=0,
                train_end=train_end,
                val_start=val_start,
                val_end=val_end,
            )
        )
        train_end = val_end + embargo_bars
    return folds


def fold_iter_to_dicts(folds: list[Fold]) -> list[dict[str, int]]:
    """Convert a list of :class:`Fold` objects to plain ``dict``s.

    Convenience helper for serialising folds into JSON-safe payloads
    (e.g. for embedding in a training manifest or evidence receipt).
    """
    return [f.model_dump() for f in folds]


# --------------------------------------------------------------------------- #
# Combinatorial Purged Cross-Validation (CPCV)                                #
# --------------------------------------------------------------------------- #
#
# CPCV (López de Prado, "Advances in Financial Machine Learning", ch. 12)
# splits the data into N contiguous groups, then for every combination of
# P groups chosen as validation, creates a fold where:
#   - validation = the P groups (contiguous or scattered)
#   - training   = the remaining N-P groups, with bars within
#     ``purge_bars`` of any validation boundary removed
#
# This produces C(N, P) folds, each with a *non-contiguous* training set.
# Unlike expanding-window walk-forward, every bar appears in validation
# exactly once (across all folds that share a group), and the combinatorial
# structure provides a much stronger overfitting signal: the PBO (CSCV)
# estimator from Bailey, Borwein, López de Prado & Zhu (2017) can be
# applied directly to the per-fold IS/OOS Sharpe ratios.


class CPCVFold(BaseModel):
    """A single CPCV fold with non-contiguous training ranges.

    ``train_ranges`` and ``val_ranges`` are lists of ``[start, end)``
    half-open index ranges into the canonical timestamp grid. Training
    ranges are already purged (bars within ``purge_bars`` of any
    validation boundary are excluded).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    val_groups: tuple[int, ...]
    train_ranges: tuple[tuple[int, int], ...]
    val_ranges: tuple[tuple[int, int], ...]

    @property
    def train_bars(self) -> int:
        return sum(end - start for start, end in self.train_ranges)

    @property
    def val_bars(self) -> int:
        return sum(end - start for start, end in self.val_ranges)


def make_cpcv_folds(
    n_bars: int,
    *,
    n_groups: int,
    n_val_groups: int,
    purge_bars: int = 0,
) -> list[CPCVFold]:
    """Build combinatorial purged cross-validation folds.

    Splits ``n_bars`` into ``n_groups`` contiguous blocks of equal size
    (the last block absorbs any remainder), then for every combination
    of ``n_val_groups`` blocks chosen as validation, creates a
    :class:`CPCVFold` where:

      * validation = the chosen blocks
      * training   = all other blocks, with ``purge_bars`` removed on
        each side of every validation boundary

    Args:
        n_bars: total number of bars in the dataset.
        n_groups: number of contiguous groups to split into (N ≥ 2).
        n_val_groups: number of groups to hold out as validation per
            fold (P, 1 ≤ P < N).
        purge_bars: number of bars to remove from training on each side
            of every validation block boundary (prevents label leakage
            from forward-return labels that straddle the boundary).

    Returns:
        ``C(N, P)`` :class:`CPCVFold` objects, ordered by the
        lexicographic order of the validation group tuples.

    Raises:
        ValueError: if any constraint is violated.
    """
    if n_groups < 2:
        raise ValueError(f"n_groups must be >= 2, got {n_groups}")
    if n_val_groups < 1 or n_val_groups >= n_groups:
        raise ValueError(f"n_val_groups must be in [1, {n_groups - 1}], got {n_val_groups}")
    if purge_bars < 0:
        raise ValueError(f"purge_bars must be >= 0, got {purge_bars}")
    if n_bars < n_groups:
        raise ValueError(f"need at least {n_groups} bars for {n_groups} groups; got {n_bars}")

    # Split into n_groups contiguous blocks. The last block absorbs the
    # remainder so every bar is covered.
    base = n_bars // n_groups
    rem = n_bars % n_groups
    boundaries: list[tuple[int, int]] = []
    start = 0
    for g in range(n_groups):
        size = base + (1 if g < rem else 0)
        boundaries.append((start, start + size))
        start += size

    folds: list[CPCVFold] = []
    for idx, val_tuple in enumerate(combinations(range(n_groups), n_val_groups)):
        val_set = set(val_tuple)
        val_ranges_raw = [boundaries[g] for g in val_tuple]

        # Build training ranges from non-validation blocks, then purge
        # bars within purge_bars of any validation boundary.
        train_ranges: list[tuple[int, int]] = []
        for g in range(n_groups):
            if g in val_set:
                continue
            blk_start, blk_end = boundaries[g]

            # Purge at the start of this training block if the previous
            # group is a validation group.
            if g > 0 and (g - 1) in val_set:
                blk_start += purge_bars

            # Purge at the end of this training block if the next group
            # is a validation group.
            if g < n_groups - 1 and (g + 1) in val_set:
                blk_end -= purge_bars

            if blk_end > blk_start:
                # Merge with previous range if adjacent (no gap between
                # consecutive training blocks).
                if train_ranges and train_ranges[-1][1] == blk_start:
                    train_ranges[-1] = (train_ranges[-1][0], blk_end)
                else:
                    train_ranges.append((blk_start, blk_end))

        folds.append(
            CPCVFold(
                index=idx,
                val_groups=val_tuple,
                train_ranges=tuple(train_ranges),
                val_ranges=tuple(val_ranges_raw),
            )
        )

    return folds


# --------------------------------------------------------------------------- #
# Walk-forward window (nanosecond space)                                      #
# --------------------------------------------------------------------------- #


class WalkForwardWindow(BaseModel):
    """A single (train, val, test) triple in nanoseconds since epoch.

    Boundaries are inclusive of start, exclusive of end. The three
    windows do not overlap; ``train_end <= val_start <= val_end <=
    test_start``.  The label horizon must be shorter than the gap
    between train_end and val_start (and val_end and test_start) so the
    label window of a train row does not bleed into validation or test.

    Mirrors ``services/quant_foundry/src/quant_foundry/training_manifest.py:315-343``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    train_start: int
    train_end: int
    val_start: int
    val_end: int
    test_start: int
    test_end: int
    label_horizon_ns: int

    def to_dict(self) -> dict[str, int]:
        return {
            "train_start": self.train_start,
            "train_end": self.train_end,
            "val_start": self.val_start,
            "val_end": self.val_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "label_horizon_ns": self.label_horizon_ns,
        }


def derive_walk_forward_window(
    *,
    train_window_ns: int,
    val_window_ns: int,
    test_window_ns: int,
    label_horizon_ns: int,
    as_of_ts: int,
) -> WalkForwardWindow:
    """Derive a single (train, val, test) triple ending at ``as_of_ts``.

    Layout (oldest → newest):
        [train_start  train_end][gap = label_horizon][val_start  val_end]
        [gap = label_horizon][test_start  test_end == as_of_ts]

    The label horizon acts as an embargo between consecutive windows so a
    training row's label does not overlap validation or test.
    """
    if label_horizon_ns <= 0:
        raise ValueError("label_horizon_ns must be > 0")
    if train_window_ns <= 0 or val_window_ns <= 0 or test_window_ns <= 0:
        raise ValueError("all window lengths must be > 0")

    test_end = as_of_ts
    test_start = test_end - test_window_ns
    val_end = test_start - label_horizon_ns
    val_start = val_end - val_window_ns
    train_end = val_start - label_horizon_ns
    train_start = train_end - train_window_ns

    if train_start < 0:
        raise ValueError(
            "train_window_ns is too long for the given as_of_ts; "
            f"train_start would be {train_start} (< 0)"
        )
    return WalkForwardWindow(
        train_start=train_start,
        train_end=train_end,
        val_start=val_start,
        val_end=val_end,
        test_start=test_start,
        test_end=test_end,
        label_horizon_ns=label_horizon_ns,
    )
