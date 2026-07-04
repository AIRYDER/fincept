"""Tests for Phase 8 / T-8.2 — LambdaRank / Cross-Sectional Ranker metrics.

Covers:
- ``rank_ic`` — Spearman rank IC (perfect, anti-perfect, random, ties,
  multi-group aggregation, zero-variance skip).
- ``ndcg_at_k`` — known relevance scores, perfect ranking, worst ranking,
  k larger than group, ties.
- ``top_k_spread`` — known spreads, mean/std aggregation, small groups.
- ``turnover`` — consecutive periods, identical portfolios (zero turnover),
  full rotation, deployment cost, item_ids tracking.
- ``cost_adjusted_long_short_return`` — known cost rate, cumulative +
  per-period, gross/net decomposition.
- ``max_drawdown`` — known equity curves, monotonic, peak-to-trough.
- ``RankReport`` — frozen, extra=forbid, field validation.
- ``compute_rank_metrics`` — fail-closed (missing groups), small-group
  warning, determinism, end-to-end aggregation.
- Edge cases: single group, small groups, ties in predictions.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest
from quant_foundry.rank_metrics import (
    RankReport,
    compute_rank_metrics,
    cost_adjusted_long_short_return,
    max_drawdown,
    ndcg_at_k,
    rank_ic,
    top_k_spread,
    turnover,
)

# ---------------------------------------------------------------------------
# rank_ic
# ---------------------------------------------------------------------------


def test_rank_ic_perfect_prediction_single_group():
    """Perfect monotonic prediction -> rank IC == 1.0."""
    preds = np.array([1.0, 2.0, 3.0, 4.0])
    labels = np.array([10.0, 20.0, 30.0, 40.0])
    groups = np.array([0, 0, 0, 0])
    mean, std = rank_ic(preds, labels, groups)
    assert mean == pytest.approx(1.0)
    assert std == pytest.approx(0.0)


def test_rank_ic_anti_perfect_prediction_single_group():
    """Anti-perfect (reversed) prediction -> rank IC == -1.0."""
    preds = np.array([4.0, 3.0, 2.0, 1.0])
    labels = np.array([10.0, 20.0, 30.0, 40.0])
    groups = np.array([0, 0, 0, 0])
    mean, std = rank_ic(preds, labels, groups)
    assert mean == pytest.approx(-1.0)
    assert std == pytest.approx(0.0)


def test_rank_ic_random_uncorrelated():
    """A non-monotonic prediction has |IC| < 1."""
    preds = np.array([1.0, 3.0, 2.0, 4.0])
    labels = np.array([10.0, 20.0, 30.0, 40.0])
    groups = np.array([0, 0, 0, 0])
    mean, _ = rank_ic(preds, labels, groups)
    assert -1.0 < mean < 1.0
    assert mean != pytest.approx(1.0)


def test_rank_ic_multi_group_aggregation():
    """Mean and std aggregate across multiple groups."""
    # Group 0: perfect (IC=1), Group 1: anti-perfect (IC=-1).
    preds = np.array([1.0, 2.0, 3.0, 4.0, 4.0, 3.0, 2.0, 1.0])
    labels = np.array([10.0, 20.0, 30.0, 40.0, 10.0, 20.0, 30.0, 40.0])
    groups = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    mean, std = rank_ic(preds, labels, groups)
    assert mean == pytest.approx(0.0)
    assert std == pytest.approx(1.0)


def test_rank_ic_ties_in_predictions():
    """Ties in predictions are handled by average ranking (no crash)."""
    preds = np.array([1.0, 1.0, 3.0, 4.0])
    labels = np.array([10.0, 20.0, 30.0, 40.0])
    groups = np.array([0, 0, 0, 0])
    mean, std = rank_ic(preds, labels, groups)
    # Finite, within range.
    assert np.isfinite(mean)
    assert -1.0 <= mean <= 1.0
    assert std == pytest.approx(0.0)


def test_rank_ic_zero_variance_group_skipped():
    """A group whose labels are all equal (zero variance) is skipped."""
    preds = np.array([1.0, 2.0, 3.0, 4.0])
    labels = np.array([5.0, 5.0, 5.0, 5.0])
    groups = np.array([0, 0, 0, 0])
    mean, std = rank_ic(preds, labels, groups)
    assert mean == pytest.approx(0.0)
    assert std == pytest.approx(0.0)


def test_rank_ic_single_item_group_skipped():
    """A group with a single item cannot produce a correlation -> skipped."""
    preds = np.array([1.0, 2.0])
    labels = np.array([10.0, 20.0])
    groups = np.array([0, 1])
    mean, std = rank_ic(preds, labels, groups)
    assert mean == pytest.approx(0.0)
    assert std == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ndcg_at_k
# ---------------------------------------------------------------------------


def test_ndcg_perfect_ranking():
    """Predictions ordered exactly by relevance -> NDCG == 1.0."""
    preds = np.array([4.0, 3.0, 2.0, 1.0])
    labels = np.array([40.0, 30.0, 20.0, 10.0])
    groups = np.array([0, 0, 0, 0])
    assert ndcg_at_k(preds, labels, groups, k=2) == pytest.approx(1.0)


def test_ndcg_worst_ranking():
    """Predictions in reverse order of relevance -> NDCG < 1 (and bounded >= 0)."""
    preds = np.array([1.0, 2.0, 3.0, 4.0])
    labels = np.array([40.0, 30.0, 20.0, 10.0])
    score = ndcg_at_k(preds, labels, groups=np.array([0, 0, 0, 0]), k=2)
    assert 0.0 <= score < 1.0


def test_ndcg_known_value():
    """NDCG with a hand-computed DCG/IDCG.

    preds   = [5.0, 4.0, 1.0, 0.5, 2.0, 0.0]
    labels  = [3.0, 2.0, 3.0, 0.0, 1.0, 2.0]
    Sort by prediction (desc): indices 0, 1, 4, 2, 3, 5
    Top-3 relevances (by prediction) = [3, 2, 1]
    DCG  = 3/log2(2) + 2/log2(3) + 1/log2(4) = 3 + 1.2619 + 0.5 = 4.7619
    Ideal top-3 relevances = [3, 3, 2]
    IDCG = 3/log2(2) + 3/log2(3) + 2/log2(4) = 3 + 1.8928 + 1 = 5.8928
    NDCG = 4.7619 / 5.8928 ~ 0.8081
    """
    preds = np.array([5.0, 4.0, 1.0, 0.5, 2.0, 0.0])
    labels = np.array([3.0, 2.0, 3.0, 0.0, 1.0, 2.0])
    groups = np.array([0, 0, 0, 0, 0, 0])
    score = ndcg_at_k(preds, labels, groups, k=3)
    dcg = 3.0 / np.log2(2) + 2.0 / np.log2(3) + 1.0 / np.log2(4)
    idcg = 3.0 / np.log2(2) + 3.0 / np.log2(3) + 2.0 / np.log2(4)
    expected = dcg / idcg
    assert score == pytest.approx(expected)


def test_ndcg_k_larger_than_group():
    """k larger than the group size is clamped to the group size."""
    preds = np.array([1.0, 2.0, 3.0])
    labels = np.array([10.0, 20.0, 30.0])
    groups = np.array([0, 0, 0])
    # Perfect ranking, k=10 -> clamped to 3, still perfect.
    assert ndcg_at_k(preds, labels, groups, k=10) == pytest.approx(1.0)


def test_ndcg_all_zero_relevance():
    """All-zero relevance -> IDCG=0 -> NDCG defined as 0.0."""
    preds = np.array([1.0, 2.0, 3.0])
    labels = np.array([0.0, 0.0, 0.0])
    groups = np.array([0, 0, 0])
    assert ndcg_at_k(preds, labels, groups, k=2) == pytest.approx(0.0)


def test_ndcg_multi_group_mean():
    """NDCG is averaged across groups."""
    preds = np.array([2.0, 1.0, 1.0, 2.0])
    labels = np.array([20.0, 10.0, 10.0, 20.0])
    groups = np.array([0, 0, 1, 1])
    # Both groups perfect -> mean 1.0.
    assert ndcg_at_k(preds, labels, groups, k=1) == pytest.approx(1.0)


def test_ndcg_invalid_k():
    """k <= 0 raises ValueError."""
    with pytest.raises(ValueError):
        ndcg_at_k(np.array([1.0]), np.array([1.0]), np.array([0]), k=0)


# ---------------------------------------------------------------------------
# top_k_spread
# ---------------------------------------------------------------------------


def test_top_k_spread_known_value():
    """Top-2 minus bottom-2 spread for a single group."""
    preds = np.array([4.0, 3.0, 2.0, 1.0])
    labels = np.array([40.0, 30.0, 20.0, 10.0])
    groups = np.array([0, 0, 0, 0])
    mean, std = top_k_spread(preds, labels, groups, k=2)
    # top-2 mean = 35, bottom-2 mean = 15 -> spread = 20.
    assert mean == pytest.approx(20.0)
    assert std == pytest.approx(0.0)


def test_top_k_spread_multi_group_mean_std():
    """Mean and std of spread across two groups."""
    preds = np.array([4.0, 3.0, 2.0, 1.0, 4.0, 3.0, 2.0, 1.0])
    labels = np.array([40.0, 30.0, 20.0, 10.0, 20.0, 10.0, 0.0, -10.0])
    groups = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    mean, std = top_k_spread(preds, labels, groups, k=2)
    # Group 0 spread = 20, group 1 spread = 20 -> mean 20, std 0.
    assert mean == pytest.approx(20.0)
    assert std == pytest.approx(0.0)


def test_top_k_spread_negative_when_predictions_wrong():
    """If the model ranks the worst items as top, spread is negative."""
    preds = np.array([1.0, 2.0, 3.0, 4.0])  # worst items predicted high
    labels = np.array([40.0, 30.0, 20.0, 10.0])
    groups = np.array([0, 0, 0, 0])
    mean, _ = top_k_spread(preds, labels, groups, k=2)
    # top-2 by pred = labels[2,3] = 15, bottom-2 = labels[0,1] = 35 -> -20.
    assert mean == pytest.approx(-20.0)


def test_top_k_spread_small_group_uses_half():
    """A group with fewer than 2*k items uses min(k, n//2)."""
    preds = np.array([3.0, 1.0, 2.0])
    labels = np.array([30.0, 10.0, 20.0])
    groups = np.array([0, 0, 0])
    # n=3, k=2 -> kk = min(2, 1) = 1. top-1 = 30, bottom-1 = 10 -> 20.
    mean, std = top_k_spread(preds, labels, groups, k=2)
    assert mean == pytest.approx(20.0)
    assert std == pytest.approx(0.0)


def test_top_k_spread_invalid_k():
    """k <= 0 raises ValueError."""
    with pytest.raises(ValueError):
        top_k_spread(np.array([1.0]), np.array([1.0]), np.array([0]), k=0)


# ---------------------------------------------------------------------------
# turnover
# ---------------------------------------------------------------------------


def test_turnover_first_period_deployment_cost():
    """The first period's turnover equals sum(|w|)/2 = 1.0 for a balanced L/S."""
    preds = np.array([4.0, 3.0, 2.0, 1.0])
    groups = np.array([0, 0, 0, 0])
    mean, per_period = turnover(preds, groups, timestamps=None, k=2)
    assert per_period.shape[0] == 1
    assert per_period[0] == pytest.approx(1.0)
    assert mean == pytest.approx(1.0)


def test_turnover_identical_portfolios_zero():
    """Two consecutive periods with the same portfolio -> 2nd turnover is 0."""
    # Same predictions across two periods -> same top/bottom.
    preds = np.array([4.0, 3.0, 2.0, 1.0, 4.0, 3.0, 2.0, 1.0])
    groups = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    item_ids = np.array([10, 11, 12, 13, 10, 11, 12, 13])
    mean, per_period = turnover(preds, groups, timestamps=None, item_ids=item_ids, k=2)
    assert per_period[0] == pytest.approx(1.0)  # deployment
    assert per_period[1] == pytest.approx(0.0)  # no change
    assert mean == pytest.approx(0.5)


def test_turnover_full_rotation():
    """Completely swapping the long and short legs -> turnover = 1.0 (after deploy)."""
    # Period 0: item 0 long, item 1 short. Period 1: item 1 long, item 0 short.
    preds = np.array([2.0, 1.0, 1.0, 2.0])
    groups = np.array([0, 0, 1, 1])
    item_ids = np.array([0, 1, 0, 1])
    mean, per_period = turnover(preds, groups, timestamps=None, item_ids=item_ids, k=1)
    # deploy = 1.0; rotation: w0 {0:+1, 1:-1}, w1 {0:-1, 1:+1} -> |−2|+|2| /2 = 2.0
    assert per_period[0] == pytest.approx(1.0)
    assert per_period[1] == pytest.approx(2.0)


def test_turnover_partial_change():
    """One item leaves the long leg -> turnover = 1/k."""
    # k=2, period 0 long {0,1} short {2,3}; period 1 long {0,4} short {2,3}.
    preds = np.array([5.0, 4.0, 2.0, 1.0, 5.0, 4.0, 2.0, 1.0])
    groups = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    item_ids = np.array([0, 1, 2, 3, 0, 4, 2, 3])
    mean, per_period = turnover(preds, groups, timestamps=None, item_ids=item_ids, k=2)
    # deploy 1.0; change: item1 0->-0.5 (diff 0.5), item4 0->+0.5 (diff 0.5)
    # sum|diff|/2 = (0.5+0.5)/2 = 0.5
    assert per_period[1] == pytest.approx(0.5)


def test_turnover_timestamps_ordering():
    """Groups are ordered by timestamp, not by appearance."""
    preds = np.array([2.0, 1.0, 2.0, 1.0])
    groups = np.array([1, 1, 0, 0])  # group 1 appears first but is later
    timestamps = np.array([200, 200, 100, 100])
    item_ids = np.array([0, 1, 0, 1])
    mean, per_period = turnover(preds, groups, timestamps=timestamps, item_ids=item_ids, k=1)
    # Ordered: group 0 (ts=100) then group 1 (ts=200). Both identical portfolio.
    assert per_period[0] == pytest.approx(1.0)
    assert per_period[1] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# cost_adjusted_long_short_return
# ---------------------------------------------------------------------------


def test_cost_adjusted_return_known_cost():
    """Gross return minus cost with a known cost rate."""
    # Single period, perfect prediction, k=2.
    preds = np.array([4.0, 3.0, 2.0, 1.0])
    labels = np.array([0.04, 0.03, 0.02, 0.01])
    groups = np.array([0, 0, 0, 0])
    cum, net, turn = cost_adjusted_long_short_return(
        preds,
        labels,
        groups,
        timestamps=None,
        k=2,
        cost_per_turnover=0.001,
    )
    # gross = mean(0.04,0.03) - mean(0.02,0.01) = 0.035 - 0.015 = 0.02
    # turn = 1.0 (deploy), cost = 0.001, net = 0.019
    assert net[0] == pytest.approx(0.02 - 0.001)
    assert cum == pytest.approx(0.019)
    assert turn[0] == pytest.approx(1.0)


def test_cost_adjusted_return_multi_period_cumulative():
    """Cumulative return is the sum of per-period net returns."""
    preds = np.array([4.0, 3.0, 2.0, 1.0, 4.0, 3.0, 2.0, 1.0])
    labels = np.array([0.04, 0.03, 0.02, 0.01, 0.04, 0.03, 0.02, 0.01])
    groups = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    item_ids = np.array([0, 1, 2, 3, 0, 1, 2, 3])
    cum, net, turn = cost_adjusted_long_short_return(
        preds,
        labels,
        groups,
        timestamps=None,
        item_ids=item_ids,
        k=2,
        cost_per_turnover=0.0,
    )
    # cost=0 -> net = gross = 0.02 each period -> cum = 0.04.
    assert net[0] == pytest.approx(0.02)
    assert net[1] == pytest.approx(0.02)
    assert cum == pytest.approx(0.04)


def test_cost_adjusted_return_with_rotation_cost():
    """A full rotation incurs cost in the second period."""
    preds = np.array([2.0, 1.0, 1.0, 2.0])
    labels = np.array([0.02, 0.01, 0.01, 0.02])
    groups = np.array([0, 0, 1, 1])
    item_ids = np.array([0, 1, 0, 1])
    cum, net, turn = cost_adjusted_long_short_return(
        preds,
        labels,
        groups,
        timestamps=None,
        item_ids=item_ids,
        k=1,
        cost_per_turnover=0.01,
    )
    # Period 0: gross = 0.02-0.01 = 0.01, turn=1.0, cost=0.01, net=0.0
    # Period 1: gross = 0.02-0.01 = 0.01, turn=2.0, cost=0.02, net=-0.01
    assert net[0] == pytest.approx(0.0)
    assert net[1] == pytest.approx(-0.01)
    assert cum == pytest.approx(-0.01)


def test_cost_adjusted_return_zero_cost_equals_gross():
    """With cost_per_turnover=0, net equals gross long-short return."""
    preds = np.array([3.0, 1.0, 2.0])
    labels = np.array([0.03, 0.01, 0.02])
    groups = np.array([0, 0, 0])
    cum, net, _ = cost_adjusted_long_short_return(
        preds,
        labels,
        groups,
        timestamps=None,
        k=1,
        cost_per_turnover=0.0,
    )
    # k=1, n=3 -> kk=1. top-1=0.03, bottom-1=0.01 -> gross=0.02.
    assert net[0] == pytest.approx(0.02)
    assert cum == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# max_drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_monotonic_increasing():
    """A strictly increasing curve has zero drawdown."""
    equity = np.array([0.01, 0.02, 0.03, 0.04])
    assert max_drawdown(equity) == pytest.approx(0.0)


def test_max_drawdown_known_peak_to_trough():
    """Peak 0.05 then trough -0.02 -> drawdown 0.07."""
    equity = np.array([0.01, 0.05, -0.02, 0.0])
    assert max_drawdown(equity) == pytest.approx(0.07)


def test_max_drawdown_single_period():
    """A single-element series has zero drawdown."""
    assert max_drawdown(np.array([0.05])) == pytest.approx(0.0)


def test_max_drawdown_empty():
    """An empty series has zero drawdown."""
    assert max_drawdown(np.array([])) == pytest.approx(0.0)


def test_max_drawdown_recover_then_new_low():
    """Drawdown is the max peak-to-trough even after partial recovery."""
    equity = np.array([0.10, -0.05, 0.02, -0.10])
    # peak 0.10, trough -0.10 -> dd 0.20.
    assert max_drawdown(equity) == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# RankReport
# ---------------------------------------------------------------------------


def test_rank_report_frozen():
    """RankReport must be immutable."""
    report = RankReport(
        rank_ic_mean=0.1,
        rank_ic_std=0.05,
        ndcg_at_k=0.8,
        top_k_spread_mean=0.02,
        top_k_spread_std=0.01,
        turnover_mean=0.5,
        cost_adjusted_ls_return=0.1,
        max_drawdown=0.05,
        n_groups=3,
        n_periods=3,
    )
    with pytest.raises(Exception):
        report.rank_ic_mean = 0.9  # type: ignore[misc]


def test_rank_report_extra_forbid():
    """RankReport rejects unknown fields."""
    with pytest.raises(Exception):
        RankReport(
            rank_ic_mean=0.1,
            rank_ic_std=0.05,
            ndcg_at_k=0.8,
            top_k_spread_mean=0.02,
            top_k_spread_std=0.01,
            turnover_mean=0.5,
            cost_adjusted_ls_return=0.1,
            max_drawdown=0.05,
            n_groups=3,
            n_periods=3,
            unknown="bad",  # type: ignore[call-arg]
        )


def test_rank_report_negative_count_rejected():
    """n_groups / n_periods must be >= 0."""
    with pytest.raises(Exception):
        RankReport(
            rank_ic_mean=0.0,
            rank_ic_std=0.0,
            ndcg_at_k=0.0,
            top_k_spread_mean=0.0,
            top_k_spread_std=0.0,
            turnover_mean=0.0,
            cost_adjusted_ls_return=0.0,
            max_drawdown=0.0,
            n_groups=-1,
            n_periods=0,
        )


def test_rank_report_field_values_preserved():
    """All fields round-trip through construction."""
    report = RankReport(
        rank_ic_mean=0.123,
        rank_ic_std=0.045,
        ndcg_at_k=0.91,
        top_k_spread_mean=0.02,
        top_k_spread_std=0.008,
        turnover_mean=0.33,
        cost_adjusted_ls_return=0.15,
        max_drawdown=0.07,
        n_groups=5,
        n_periods=5,
    )
    assert report.rank_ic_mean == pytest.approx(0.123)
    assert report.ndcg_at_k == pytest.approx(0.91)
    assert report.n_groups == 5
    assert report.n_periods == 5


# ---------------------------------------------------------------------------
# compute_rank_metrics — fail-closed + end-to-end
# ---------------------------------------------------------------------------


def test_compute_rank_metrics_fail_closed_none_groups():
    """None groups -> ValueError with the required message."""
    with pytest.raises(ValueError, match="ranking metrics require groups"):
        compute_rank_metrics(
            np.array([1.0, 2.0]),
            np.array([1.0, 2.0]),
            groups=None,
        )


def test_compute_rank_metrics_fail_closed_empty_groups():
    """Empty groups -> ValueError with the required message."""
    with pytest.raises(ValueError, match="ranking metrics require groups"):
        compute_rank_metrics(
            np.array([]),
            np.array([]),
            groups=np.array([]),
        )


def test_compute_rank_metrics_invalid_top_k():
    """top_k <= 0 -> ValueError."""
    with pytest.raises(ValueError):
        compute_rank_metrics(
            np.array([1.0, 2.0]),
            np.array([1.0, 2.0]),
            groups=np.array([0, 0]),
            top_k=0,
        )


def test_compute_rank_metrics_length_mismatch():
    """Mismatched prediction/label/group lengths -> ValueError."""
    with pytest.raises(ValueError):
        compute_rank_metrics(
            np.array([1.0, 2.0, 3.0]),
            np.array([1.0, 2.0]),
            groups=np.array([0, 0]),
        )


def test_compute_rank_metrics_small_group_warns():
    """A group smaller than 2*top_k emits a warning but does not fail."""
    preds = np.array([1.0, 2.0, 3.0])
    labels = np.array([0.01, 0.02, 0.03])
    groups = np.array([0, 0, 0])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = compute_rank_metrics(preds, labels, groups, top_k=10)
        assert any(issubclass(w.category, UserWarning) for w in caught)
    assert report.n_groups == 1


def test_compute_rank_metrics_end_to_end():
    """End-to-end report contains all expected fields with sane values."""
    rng = np.random.default_rng(42)
    n = 60
    groups = np.repeat(np.arange(3), 20)
    preds = rng.standard_normal(n)
    labels = preds * 0.5 + rng.standard_normal(n) * 0.1  # correlated
    timestamps = np.repeat(np.arange(3), 20) * 100
    item_ids = np.tile(np.arange(20), 3)
    report = compute_rank_metrics(
        preds,
        labels,
        groups,
        timestamps=timestamps,
        top_k=5,
        cost_per_turnover=0.001,
        item_ids=item_ids,
    )
    assert isinstance(report, RankReport)
    assert report.n_groups == 3
    assert report.n_periods == 3
    # Positive IC expected (predictions correlated with labels).
    assert report.rank_ic_mean > 0.0
    assert 0.0 <= report.ndcg_at_k <= 1.0
    assert report.max_drawdown >= 0.0


def test_compute_rank_metrics_deterministic():
    """Same inputs -> identical report (determinism)."""
    rng = np.random.default_rng(7)
    n = 40
    groups = np.repeat(np.arange(2), 20)
    preds = rng.standard_normal(n)
    labels = rng.standard_normal(n)
    item_ids = np.tile(np.arange(20), 2)
    r1 = compute_rank_metrics(preds, labels, groups, top_k=5, item_ids=item_ids)
    r2 = compute_rank_metrics(preds, labels, groups, top_k=5, item_ids=item_ids)
    assert r1.model_dump() == r2.model_dump()


def test_compute_rank_metrics_single_group():
    """A single group is a valid input."""
    preds = np.array([4.0, 3.0, 2.0, 1.0])
    labels = np.array([0.04, 0.03, 0.02, 0.01])
    groups = np.array([0, 0, 0, 0])
    report = compute_rank_metrics(preds, labels, groups, top_k=2)
    assert report.n_groups == 1
    assert report.n_periods == 1
    assert report.rank_ic_mean == pytest.approx(1.0)
    assert report.top_k_spread_mean == pytest.approx(0.02)


def test_compute_rank_metrics_ties_in_predictions():
    """Tied predictions do not crash and produce a finite report."""
    preds = np.array([1.0, 1.0, 1.0, 1.0])
    labels = np.array([0.04, 0.03, 0.02, 0.01])
    groups = np.array([0, 0, 0, 0])
    report = compute_rank_metrics(preds, labels, groups, top_k=2)
    # All-equal predictions -> IC undefined (skipped) -> 0.0.
    assert report.rank_ic_mean == pytest.approx(0.0)
    assert np.isfinite(report.ndcg_at_k)
    assert np.isfinite(report.cost_adjusted_ls_return)


def test_compute_rank_metrics_timestamps_ordering():
    """Periods are ordered by timestamp for the return series."""
    preds = np.array([4.0, 3.0, 2.0, 1.0, 4.0, 3.0, 2.0, 1.0])
    labels = np.array([0.04, 0.03, 0.02, 0.01, 0.01, 0.02, 0.03, 0.04])
    groups = np.array([1, 1, 1, 1, 0, 0, 0, 0])  # group 1 first but later
    timestamps = np.array([200, 200, 200, 200, 100, 100, 100, 100])
    item_ids = np.array([0, 1, 2, 3, 0, 1, 2, 3])
    report = compute_rank_metrics(
        preds,
        labels,
        groups,
        timestamps=timestamps,
        top_k=2,
        cost_per_turnover=0.0,
        item_ids=item_ids,
    )
    # Ordered: group 0 (ts=100) gross +0.02, group 1 (ts=200) gross -0.02.
    # cum = 0.0 (with zero cost).
    assert report.cost_adjusted_ls_return == pytest.approx(0.0)


def test_compute_rank_metrics_recompute_top_k_spread():
    """Top-k spread recomputed from a prediction artifact matches the report.

    Acceptance criterion: given predictions + labels + groups, recompute
    the top-k spread and confirm it equals the report's spread.
    """
    rng = np.random.default_rng(123)
    n = 40
    groups = np.repeat(np.arange(2), 20)
    preds = rng.standard_normal(n)
    labels = rng.standard_normal(n)
    report = compute_rank_metrics(preds, labels, groups, top_k=5)
    sp_mean, _ = top_k_spread(preds, labels, groups, k=5)
    assert report.top_k_spread_mean == pytest.approx(sp_mean)


def test_compute_rank_metrics_no_item_ids_uses_position():
    """Without item_ids, turnover uses within-group position (aligned universe)."""
    preds = np.array([4.0, 3.0, 2.0, 1.0, 4.0, 3.0, 2.0, 1.0])
    labels = np.array([0.04, 0.03, 0.02, 0.01, 0.04, 0.03, 0.02, 0.01])
    groups = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    # No item_ids: positions align -> identical portfolio -> 2nd turnover 0.
    report = compute_rank_metrics(preds, labels, groups, top_k=2, cost_per_turnover=0.0)
    # turnover_mean = (1.0 + 0.0)/2 = 0.5
    assert report.turnover_mean == pytest.approx(0.5)
