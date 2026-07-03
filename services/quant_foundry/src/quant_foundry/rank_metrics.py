"""
quant_foundry.rank_metrics — cross-sectional ranking metrics for LambdaRank.

Phase 8 / T-8.2: LambdaRank / Cross-Sectional Ranker metrics.

This module implements the *portfolio-relevant* evaluation metrics that a
ranking model (LightGBM ``lambdarank`` objective) is judged on after
training. All functions are deterministic and side-effect free so reruns
produce identical results (cross-cutting quant rigor §1).

Metrics implemented:
- **Rank IC** — Spearman rank correlation between predicted and actual
  returns within each cross-section (group), aggregated with mean and std.
- **NDCG@k** — Normalized Discounted Cumulative Gain for the top-k items
  within each group, using the actual returns as relevance scores.
- **Top-k Spread** — Difference in actual returns between the top-k and
  bottom-k predicted items within each group (mean and std across groups).
- **Turnover** — Portfolio stability: ``sum(|w_t - w_{t-1}|) / 2`` where
  ``w`` are the long-short portfolio weights across consecutive periods.
- **Cost-Adjusted Long-Short Return** — Long top-k, short bottom-k, net of
  transaction costs (configurable cost per unit turnover). Cumulative +
  per-period.
- **Drawdown** — Maximum peak-to-trough decline of the cumulative
  long-short return series.

Fail-closed invariants (enforced + tested):
- ``groups`` is required. ``None`` or empty raises
  ``ValueError("ranking metrics require groups")``.
- A group with fewer than ``2 * top_k`` items emits a warning but does NOT
  fail (the metric is still computed with ``min(k, group_size)``).

Design notes:
- Inputs are 1-D numpy arrays of equal length: ``predictions``,
  ``labels`` (actual returns), ``groups`` (cross-section id), and
  ``timestamps`` (period ordering). ``item_ids`` is optional and used to
  track portfolio membership across periods for turnover; when omitted,
  the within-group positional index is used as a pseudo-id (assumes an
  aligned, same-size universe across consecutive periods).
- All metrics are deterministic given the same inputs.
- Pydantic v2 ``RankReport`` is ``frozen=True, extra="forbid"`` for audit
  integrity (same pattern as ``dataset_manifest`` / ``training_manifest``).
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    "RankReport",
    "compute_rank_metrics",
    "rank_ic",
    "ndcg_at_k",
    "top_k_spread",
    "turnover",
    "cost_adjusted_long_short_return",
    "max_drawdown",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_float_array(x: Any, name: str) -> np.ndarray:
    """Coerce ``x`` to a 1-D float64 numpy array.

    Args:
        x: array-like input.
        name: parameter name for error messages.

    Returns:
        1-D ``float64`` numpy array.

    Raises:
        ValueError: if ``x`` cannot be converted to a 1-D float array.
    """
    if x is None:
        raise ValueError(f"{name} must not be None")
    arr = np.asarray(x, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be 1-D; got {arr.ndim}D")
    return arr


def _as_array(x: Any, name: str) -> np.ndarray:
    """Coerce ``x`` to a 1-D numpy array (any dtype, not converted)."""
    if x is None:
        raise ValueError(f"{name} must not be None")
    arr = np.asarray(x)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be 1-D; got {arr.ndim}D")
    return arr


def _rank_average(a: np.ndarray) -> np.ndarray:
    """Return ranks of ``a`` with ties broken by the *average* method.

    This is the Spearman ranking convention: tied values receive the
    mean of the ranks they would have occupied. The smallest value gets
    rank 1.

    Args:
        a: 1-D array of values.

    Returns:
        1-D float64 array of average ranks (1-based).
    """
    a = np.asarray(a, dtype=np.float64)
    n = a.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.float64)
    # Order indices by value (stable sort keeps original order for ties).
    order = np.argsort(a, kind="stable")
    ranks = np.empty(n, dtype=np.float64)
    i = 0
    sorted_a = a[order]
    while i < n:
        j = i
        # Find the run of tied values.
        while j + 1 < n and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based average rank
        for idx in range(i, j + 1):
            ranks[order[idx]] = avg_rank
        i = j + 1
    return ranks


def _group_indices(groups: np.ndarray) -> dict[Any, np.ndarray]:
    """Return a mapping of unique group id -> row indices (in input order).

    Args:
        groups: 1-D array of group identifiers.

    Returns:
        Dict mapping each unique group id to a 1-D int array of row
        indices, in the order the group first appears in ``groups``.
    """
    seen: list[Any] = []
    idx_map: dict[Any, list[int]] = {}
    for i, g in enumerate(groups):
        key = g.item() if isinstance(g, np.generic) else g
        if key not in idx_map:
            idx_map[key] = []
            seen.append(key)
        idx_map[key].append(i)
    return {k: np.asarray(idx_map[k], dtype=np.int64) for k in seen}


# ---------------------------------------------------------------------------
# Individual metrics
# ---------------------------------------------------------------------------


def rank_ic(
    predictions: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
) -> tuple[float, float]:
    """Rank Information Coefficient (Spearman) aggregated across groups.

    For each cross-section (group), computes the Spearman rank correlation
    between predictions and labels (actual returns), then aggregates with
    mean and std across groups. Groups with fewer than 2 items or zero
    variance in either predictions or labels are skipped (they produce an
    undefined correlation).

    Args:
        predictions: 1-D array of predicted scores.
        labels: 1-D array of actual returns / relevance.
        groups: 1-D array of group ids (one per row).

    Returns:
        ``(rank_ic_mean, rank_ic_std)``. Both are ``0.0`` when no group
        yields a valid correlation.
    """
    preds = _as_float_array(predictions, "predictions")
    labs = _as_float_array(labels, "labels")
    grp = _as_array(groups, "groups")
    ics: list[float] = []
    for _, idx in _group_indices(grp).items():
        if idx.shape[0] < 2:
            continue
        p = preds[idx]
        l = labs[idx]
        rp = _rank_average(p)
        rl = _rank_average(l)
        rp_c = rp - rp.mean()
        rl_c = rl - rl.mean()
        denom = np.sqrt(np.sum(rp_c**2) * np.sum(rl_c**2))
        if denom == 0.0:
            continue
        ics.append(float(np.sum(rp_c * rl_c) / denom))
    if not ics:
        return 0.0, 0.0
    arr = np.asarray(ics, dtype=np.float64)
    return float(arr.mean()), float(arr.std(ddof=0))


def ndcg_at_k(
    predictions: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    k: int = 10,
) -> float:
    """Mean NDCG@k across groups, using labels as relevance scores.

    DCG@k  = sum_{i=1..k} rel_i / log2(i + 1)
    IDCG@k = DCG@k of the ideal (relevance-descending) ranking
    NDCG@k = DCG@k / IDCG@k  (0.0 when IDCG@k == 0)

    Args:
        predictions: 1-D array of predicted scores.
        labels: 1-D array of actual returns used as relevance.
        groups: 1-D array of group ids.
        k: number of top items to consider.

    Returns:
        Mean NDCG@k across groups (``0.0`` when there are no groups).
    """
    if k <= 0:
        raise ValueError(f"k must be > 0; got {k}")
    preds = _as_float_array(predictions, "predictions")
    labs = _as_float_array(labels, "labels")
    grp = _as_array(groups, "groups")
    scores: list[float] = []
    for _, idx in _group_indices(grp).items():
        rel = labs[idx]
        pred = preds[idx]
        n = rel.shape[0]
        kk = min(k, n)
        if kk == 0:
            continue
        # Ranking by prediction (descending).
        pred_order = np.argsort(-pred, kind="stable")
        dcg = float(np.sum(rel[pred_order[:kk]] / np.log2(np.arange(2, kk + 2))))
        # Ideal ranking by relevance (descending).
        ideal_order = np.argsort(-rel, kind="stable")
        idcg = float(np.sum(rel[ideal_order[:kk]] / np.log2(np.arange(2, kk + 2))))
        if idcg == 0.0:
            scores.append(0.0)
        else:
            scores.append(min(dcg / idcg, 1.0))
    if not scores:
        return 0.0
    return float(np.mean(scores))


def top_k_spread(
    predictions: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    k: int = 10,
) -> tuple[float, float]:
    """Top-k minus bottom-k actual-return spread, aggregated across groups.

    For each group, sorts items by prediction (descending), then computes
    ``mean(labels of top-k) - mean(labels of bottom-k)``. Reports the mean
    and std of this spread across groups.

    Args:
        predictions: 1-D array of predicted scores.
        labels: 1-D array of actual returns.
        groups: 1-D array of group ids.
        k: number of top / bottom items.

    Returns:
        ``(top_k_spread_mean, top_k_spread_std)``. ``0.0`` for both when
        no group has at least 2 items.
    """
    if k <= 0:
        raise ValueError(f"k must be > 0; got {k}")
    preds = _as_float_array(predictions, "predictions")
    labs = _as_float_array(labels, "labels")
    grp = _as_array(groups, "groups")
    spreads: list[float] = []
    for _, idx in _group_indices(grp).items():
        n = idx.shape[0]
        if n < 2:
            continue
        kk = min(k, n // 2)
        if kk < 1:
            continue
        pred = preds[idx]
        rel = labs[idx]
        order = np.argsort(-pred, kind="stable")
        top = float(np.mean(rel[order[:kk]]))
        bottom = float(np.mean(rel[order[-kk:]]))
        spreads.append(top - bottom)
    if not spreads:
        return 0.0, 0.0
    arr = np.asarray(spreads, dtype=np.float64)
    return float(arr.mean()), float(arr.std(ddof=0))


def _portfolio_weights(
    pred: np.ndarray,
    k: int,
) -> np.ndarray:
    """Equal-weight long-short weights for a single cross-section.

    Long the top-k predictions (+1/k each), short the bottom-k predictions
    (-1/k each), zero for the rest. If the group has fewer than ``2*k``
    items, ``k`` is halved to ``min(k, n//2)`` so long and short legs are
    balanced.

    Args:
        pred: 1-D array of predicted scores for one group.
        k: target top/bottom count.

    Returns:
        1-D float64 array of weights aligned with ``pred``.
    """
    n = pred.shape[0]
    kk = min(k, n // 2) if n >= 2 else 0
    w = np.zeros(n, dtype=np.float64)
    if kk < 1:
        return w
    order = np.argsort(-pred, kind="stable")
    w[order[:kk]] = 1.0 / kk
    w[order[-kk:]] = -1.0 / kk
    return w


def turnover(
    predictions: np.ndarray,
    groups: np.ndarray,
    timestamps: np.ndarray | None,
    item_ids: np.ndarray | None = None,
    k: int = 10,
) -> tuple[float, np.ndarray]:
    """Portfolio turnover across consecutive periods.

    For each period (group), the portfolio is long top-k / short bottom-k
    (equal weight). Turnover at period ``t`` is
    ``sum(|w_t - w_{t-1}|) / 2`` over all item ids; the first period's
    turnover is the deployment cost ``sum(|w_0|) / 2`` (= 1.0 for a
    balanced long-short book).

    Item identity across periods is taken from ``item_ids`` when provided;
    otherwise the within-group positional index is used (assumes an
    aligned, same-size universe across consecutive periods).

    Args:
        predictions: 1-D array of predicted scores.
        groups: 1-D array of group ids (one period per unique group).
        timestamps: 1-D array of period timestamps used to order groups.
            If ``None``, groups are ordered by first appearance.
        item_ids: optional 1-D array of item identifiers (one per row).
        k: top/bottom count.

    Returns:
        ``(turnover_mean, per_period_turnover)`` where
        ``per_period_turnover`` is a 1-D array with one entry per period
        (in chronological order).
    """
    if k <= 0:
        raise ValueError(f"k must be > 0; got {k}")
    preds = _as_float_array(predictions, "predictions")
    grp = _as_array(groups, "groups")
    gmap = _group_indices(grp)
    # Order groups chronologically.
    ordered_keys = _ordered_group_keys(gmap, grp, timestamps)
    per_period = np.zeros(len(ordered_keys), dtype=np.float64)
    prev_w: dict[Any, float] = {}
    for pi, key in enumerate(ordered_keys):
        idx = gmap[key]
        pred = preds[idx]
        w = _portfolio_weights(pred, k)
        if item_ids is not None:
            ids = item_ids[idx]
        else:
            ids = np.arange(idx.shape[0], dtype=np.int64)
        cur_w: dict[Any, float] = {}
        for iid, ww in zip(ids, w):
            iid_key = iid.item() if isinstance(iid, np.generic) else iid
            cur_w[iid_key] = cur_w.get(iid_key, 0.0) + float(ww)
        if pi == 0:
            t = sum(abs(v) for v in cur_w.values()) / 2.0
        else:
            all_keys = set(prev_w) | set(cur_w)
            t = sum(abs(cur_w.get(kk, 0.0) - prev_w.get(kk, 0.0)) for kk in all_keys) / 2.0
        per_period[pi] = t
        prev_w = cur_w
    mean = float(per_period.mean()) if per_period.shape[0] > 0 else 0.0
    return mean, per_period


def cost_adjusted_long_short_return(
    predictions: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    timestamps: np.ndarray | None,
    item_ids: np.ndarray | None = None,
    k: int = 10,
    cost_per_turnover: float = 0.001,
) -> tuple[float, np.ndarray, np.ndarray]:
    """Cost-adjusted long-short return series.

    Per-period gross long-short return = ``mean(top-k labels) -
    mean(bottom-k labels)`` (equal weight). Transaction cost at period
    ``t`` = ``cost_per_turnover * turnover_t``. Net return = gross - cost.
    Cumulative return is the running sum of net returns (additive equity
    curve).

    Args:
        predictions: 1-D array of predicted scores.
        labels: 1-D array of actual returns.
        groups: 1-D array of group ids.
        timestamps: 1-D array of period timestamps (for ordering).
        item_ids: optional item identifiers for turnover tracking.
        k: top/bottom count.
        cost_per_turnover: cost charged per unit of turnover.

    Returns:
        ``(cumulative_return, per_period_net, per_period_turnover)``.
    """
    if k <= 0:
        raise ValueError(f"k must be > 0; got {k}")
    preds = _as_float_array(predictions, "predictions")
    labs = _as_float_array(labels, "labels")
    grp = _as_array(groups, "groups")
    gmap = _group_indices(grp)
    ordered_keys = _ordered_group_keys(gmap, grp, timestamps)
    n_periods = len(ordered_keys)
    gross = np.zeros(n_periods, dtype=np.float64)
    prev_w: dict[Any, float] = {}
    turn = np.zeros(n_periods, dtype=np.float64)
    for pi, key in enumerate(ordered_keys):
        idx = gmap[key]
        pred = preds[idx]
        rel = labs[idx]
        n = idx.shape[0]
        kk = min(k, n // 2) if n >= 2 else 0
        if kk >= 1:
            order = np.argsort(-pred, kind="stable")
            top = float(np.mean(rel[order[:kk]]))
            bottom = float(np.mean(rel[order[-kk:]]))
            gross[pi] = top - bottom
        else:
            gross[pi] = 0.0
        # Turnover.
        w = _portfolio_weights(pred, k)
        if item_ids is not None:
            ids = item_ids[idx]
        else:
            ids = np.arange(idx.shape[0], dtype=np.int64)
        cur_w: dict[Any, float] = {}
        for iid, ww in zip(ids, w):
            iid_key = iid.item() if isinstance(iid, np.generic) else iid
            cur_w[iid_key] = cur_w.get(iid_key, 0.0) + float(ww)
        if pi == 0:
            turn[pi] = sum(abs(v) for v in cur_w.values()) / 2.0
        else:
            all_keys = set(prev_w) | set(cur_w)
            turn[pi] = sum(abs(cur_w.get(kk_, 0.0) - prev_w.get(kk_, 0.0)) for kk_ in all_keys) / 2.0
        prev_w = cur_w
    cost = cost_per_turnover * turn
    net = gross - cost
    cumulative = float(np.cumsum(net)[-1]) if n_periods > 0 else 0.0
    return cumulative, net, turn


def max_drawdown(cumulative_series: np.ndarray) -> float:
    """Maximum peak-to-trough decline of a cumulative return series.

    The series is interpreted as an additive equity curve (running sum of
    per-period returns). Drawdown at index ``t`` = ``peak_up_to_t - value_t``
    where ``peak_up_to_t`` is the running maximum. Returns the largest such
    decline (``>= 0``).

    Args:
        cumulative_series: 1-D array representing the cumulative return
            equity curve. If this is a per-period (non-cumulative) series,
            pass ``np.cumsum(series)`` instead.

    Returns:
        The maximum drawdown as a non-negative float. ``0.0`` for an empty
        or single-element series.
    """
    arr = np.asarray(cumulative_series, dtype=np.float64).ravel()
    if arr.shape[0] == 0:
        return 0.0
    running_max = np.maximum.accumulate(arr)
    dd = running_max - arr
    return float(np.max(dd))


def _ordered_group_keys(
    gmap: dict[Any, np.ndarray],
    groups: np.ndarray,
    timestamps: np.ndarray | None,
) -> list[Any]:
    """Return group keys in chronological order.

    If ``timestamps`` is provided, each group's timestamp is the timestamp
    of its first row, and groups are sorted by that timestamp (ties broken
    by first-appearance order). If ``timestamps`` is ``None``, groups are
    ordered by first appearance.
    """
    keys = list(gmap.keys())
    if timestamps is None:
        return keys
    ts = np.asarray(timestamps)
    first_ts: dict[Any, Any] = {}
    for key, idx in gmap.items():
        first_ts[key] = ts[idx[0]]
    # Stable sort by timestamp preserves first-appearance order on ties.
    return sorted(keys, key=lambda key: (first_ts[key],))


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


class RankReport(BaseModel):
    """Aggregate cross-sectional ranking metrics report.

    Frozen + ``extra="forbid"`` (audit integrity). All fields are floats
    except ``n_groups`` and ``n_periods`` (ints).

    Fields:
        rank_ic_mean: mean Spearman rank IC across groups.
        rank_ic_std: population std of per-group rank IC.
        ndcg_at_k: mean NDCG@k across groups.
        top_k_spread_mean: mean top-k minus bottom-k actual-return spread.
        top_k_spread_std: population std of the per-group spread.
        turnover_mean: mean per-period portfolio turnover.
        cost_adjusted_ls_return: cumulative cost-adjusted long-short
            return (final value of the additive equity curve).
        max_drawdown: maximum peak-to-trough decline of the cumulative
            long-short return series.
        n_groups: number of distinct cross-sections (groups).
        n_periods: number of distinct periods (chronological groups).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rank_ic_mean: float
    rank_ic_std: float
    ndcg_at_k: float
    top_k_spread_mean: float
    top_k_spread_std: float
    turnover_mean: float
    cost_adjusted_ls_return: float
    max_drawdown: float
    n_groups: int
    n_periods: int

    @field_validator("n_groups", "n_periods")
    @classmethod
    def _non_negative_int(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"count must be >= 0; got {v}")
        return v


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def compute_rank_metrics(
    predictions: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray | None,
    timestamps: np.ndarray | None = None,
    *,
    top_k: int = 10,
    cost_per_turnover: float = 0.001,
    item_ids: np.ndarray | None = None,
) -> RankReport:
    """Compute the full cross-sectional ranking metric report.

    Args:
        predictions: 1-D array of predicted scores (one per row).
        labels: 1-D array of actual returns / relevance (one per row).
        groups: 1-D array of cross-section (group) ids. **Required** —
            ``None`` or empty raises ``ValueError("ranking metrics require
            groups")`` (fail-closed).
        timestamps: 1-D array of period timestamps used to order groups
            chronologically. If ``None``, groups are ordered by first
            appearance.
        top_k: number of top / bottom items for NDCG, spread, and the
            long-short portfolio. Must be ``> 0``.
        cost_per_turnover: transaction cost charged per unit of turnover.
        item_ids: optional 1-D array of item identifiers used to track
            portfolio membership across periods for turnover. When
            omitted, the within-group positional index is used.

    Returns:
        A frozen :class:`RankReport` with all metrics.

    Raises:
        ValueError: if ``groups`` is ``None`` or empty, if ``top_k`` is
            not positive, or if input lengths do not match.

    Warns:
        UserWarning: if any group has fewer than ``2 * top_k`` items
            (metric still computed with ``min(k, n//2)``).
    """
    if groups is None:
        raise ValueError("ranking metrics require groups")
    grp = _as_array(groups, "groups")
    if grp.shape[0] == 0:
        raise ValueError("ranking metrics require groups")
    if top_k <= 0:
        raise ValueError(f"top_k must be > 0; got {top_k}")

    preds = _as_float_array(predictions, "predictions")
    labs = _as_float_array(labels, "labels")
    if preds.shape[0] != labs.shape[0] or preds.shape[0] != grp.shape[0]:
        raise ValueError(
            "predictions, labels, and groups must have equal length; "
            f"got {preds.shape[0]}, {labs.shape[0]}, {grp.shape[0]}"
        )
    if timestamps is not None:
        ts = _as_array(timestamps, "timestamps")
        if ts.shape[0] != grp.shape[0]:
            raise ValueError(
                "timestamps must have the same length as groups; "
                f"got {ts.shape[0]} vs {grp.shape[0]}"
            )
    if item_ids is not None:
        ids = _as_array(item_ids, "item_ids")
        if ids.shape[0] != grp.shape[0]:
            raise ValueError(
                "item_ids must have the same length as groups; "
                f"got {ids.shape[0]} vs {grp.shape[0]}"
            )

    gmap = _group_indices(grp)
    n_groups = len(gmap)

    # Warn (do not fail) on small groups.
    for key, idx in gmap.items():
        if idx.shape[0] < 2 * top_k:
            warnings.warn(
                f"group {key!r} has {idx.shape[0]} items, fewer than "
                f"2*top_k={2 * top_k}; metrics will use "
                f"min(k, n//2) where applicable",
                UserWarning,
                stacklevel=2,
            )

    ic_mean, ic_std = rank_ic(preds, labs, grp)
    ndcg = ndcg_at_k(preds, labs, grp, k=top_k)
    sp_mean, sp_std = top_k_spread(preds, labs, grp, k=top_k)

    cum, _net, _turn = cost_adjusted_long_short_return(
        preds,
        labs,
        grp,
        timestamps if timestamps is not None else None,
        item_ids=item_ids,
        k=top_k,
        cost_per_turnover=cost_per_turnover,
    )
    turn_mean, _ = turnover(
        preds,
        grp,
        timestamps if timestamps is not None else None,
        item_ids=item_ids,
        k=top_k,
    )

    # Drawdown over the cumulative equity curve.
    if n_groups > 0:
        equity = np.cumsum(_net)
        dd = max_drawdown(equity)
    else:
        dd = 0.0

    n_periods = n_groups  # one period per group (cross-section)

    return RankReport(
        rank_ic_mean=ic_mean,
        rank_ic_std=ic_std,
        ndcg_at_k=ndcg,
        top_k_spread_mean=sp_mean,
        top_k_spread_std=sp_std,
        turnover_mean=turn_mean,
        cost_adjusted_ls_return=cum,
        max_drawdown=dd,
        n_groups=n_groups,
        n_periods=n_periods,
    )
