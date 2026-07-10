"""
quant_foundry.significance — statistical significance for the tournament (TASK-0404).

Two primitives that the tournament ranks on (cross-cutting rigor §2):

1. **Deflated Sharpe Ratio (DSR)** — discounts the raw Sharpe for
   (a) the number of trials already run for the model family
   (multiple-comparisons correction) and (b) return non-normality
   (skew + excess kurtosis). A model that looks good only because we tried
   many configurations, or because of a few lucky outliers, must NOT rank as
   high as a model with the same raw Sharpe but fewer trials / cleaner returns.

   The deflation follows the Bailey & Lopez de Prado (2014) form. For the MVP
   skeleton we use a deterministic, conservative approximation: the
   multiple-trials penalty is ``sqrt(2 * ln(trial_count))`` scaled by the
   per-period standard error, and the non-normality term is the standard
   DSR skew/kurtosis adjustment. This is intentionally simple and auditable;
   a fuller implementation can swap in the exact DSR formula later without
   changing the public surface.

2. **Stationary / block bootstrap p-value** — tests whether the model's
   out-of-sample net edge is statistically different from a baseline, WITHOUT
   assuming IID returns. Returns overlap across horizons (a 5-day prediction
   made on day t and day t+1 share 4 days of return), so an IID t-test would
   understate the variance and overstate significance. The stationary
   bootstrap (Politis & Romano, 1994) resamples blocks of random length
   (geometrically distributed with expected length 1/p) so the autocorrelation
   structure is preserved. The p-value is the fraction of bootstrap resamples
   where the baseline matches or beats the model.

Both primitives are deterministic given a fixed seed (tests must be
reproducible). No external dependencies beyond the stdlib — the bootstrap
uses a seeded ``random.Random`` so there is no numpy/scipy coupling and the
skeleton stays portable.

File-disjoint from all active builders (see BUILDER3.md). Does NOT import
``outcomes.py`` / ``settlement.py`` / ``dossier.py`` — operates on plain
``list[float]`` return series.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeflatedSharpeResult:
    """Result of the Deflated Sharpe Ratio computation.

    Fields:
    - ``raw_sharpe``: the un-deflated (per-period) Sharpe ratio of the OOS
      returns. Annualization is the caller's concern; the tournament ranks on
      the per-period figure so the horizon is consistent across models.
    - ``deflated_sharpe``: the Sharpe after discounting for trial count
      (multiple comparisons) and return non-normality (skew + kurtosis).
      Always <= ``raw_sharpe``.
    - ``trial_count``: the number of trials for the model family (carried
      through so the rank is auditable).
    - ``skew``: sample skewness of the OOS returns.
    - ``kurtosis``: sample (non-excess) kurtosis of the OOS returns.
    - ``multiple_trials_penalty``: the multiple-comparisons discount applied
      (a function of trial_count). Recorded for auditability.
    - ``non_normality_penalty``: the skew/kurtosis discount applied. Recorded
      for auditability.
    """

    raw_sharpe: float
    deflated_sharpe: float
    trial_count: int
    skew: float
    kurtosis: float
    multiple_trials_penalty: float
    non_normality_penalty: float


def _sample_skew(returns: list[float], mean: float, std: float) -> float:
    """Sample skewness (Fisher-Pearson, biased). 0 if std is 0."""
    if std == 0.0 or len(returns) < 3:
        return 0.0
    n = len(returns)
    return sum((r - mean) ** 3 for r in returns) / (n * std**3)


def _sample_kurtosis(returns: list[float], mean: float, std: float) -> float:
    """Sample (non-excess) kurtosis. 3.0 (Gaussian) if std is 0."""
    if std == 0.0 or len(returns) < 4:
        return 3.0
    n = len(returns)
    return sum((r - mean) ** 4 for r in returns) / (n * std**4)


def deflated_sharpe_ratio(
    oos_returns: list[float],
    trial_count: int,
) -> DeflatedSharpeResult:
    """Compute the Deflated Sharpe Ratio for an OOS return series.

    The deflation has two parts:

    1. **Multiple-trials penalty**: the expected max Sharpe under
       ``trial_count`` independent trials grows as
       ``sqrt(2 * ln(max(trial_count, 1)))`` (extreme-value approximation).
       We subtract this (scaled to per-period) from the raw Sharpe.

    2. **Non-normality penalty**: the standard DSR skew/kurtosis adjustment.
       Applied multiplicatively so the direction is always conservative
       (DSR <= raw Sharpe).

    The result is always <= ``raw_sharpe`` (deflation only discounts). For
    a zero-mean series the raw Sharpe is 0 and the DSR is <= 0.
    """
    n = len(oos_returns)
    if n == 0:
        return DeflatedSharpeResult(
            raw_sharpe=0.0,
            deflated_sharpe=0.0,
            trial_count=trial_count,
            skew=0.0,
            kurtosis=3.0,
            multiple_trials_penalty=0.0,
            non_normality_penalty=0.0,
        )
    mean = statistics.fmean(oos_returns)
    if n >= 2:
        var = sum((r - mean) ** 2 for r in oos_returns) / n
        std = math.sqrt(var)
    else:
        std = 0.0

    raw_sharpe = (mean / std) if std > 0.0 else 0.0
    skew = _sample_skew(oos_returns, mean, std)
    kurt = _sample_kurtosis(oos_returns, mean, std)

    tc = max(trial_count, 1)
    multiple_trials_penalty = math.sqrt(2.0 * math.log(tc)) / math.sqrt(max(n, 1))

    if n >= 2 and std > 0.0:
        s_term = raw_sharpe / math.sqrt(n - 1)
        denom = 1.0 - (kurt - 1.0) / 4.0 * s_term**2
        if denom <= 0.0:
            non_normality_factor = 0.0
        else:
            non_normality_factor = math.sqrt(
                max(0.0, 1.0 - skew * s_term + (kurt - 1.0) / 4.0 * s_term**2)
            ) / math.sqrt(denom)
            non_normality_factor = min(non_normality_factor, 1.0)
    else:
        non_normality_factor = 1.0

    non_normality_penalty = 1.0 - non_normality_factor

    deflated = (raw_sharpe - multiple_trials_penalty) * non_normality_factor
    if deflated > raw_sharpe:
        deflated = raw_sharpe

    return DeflatedSharpeResult(
        raw_sharpe=raw_sharpe,
        deflated_sharpe=deflated,
        trial_count=trial_count,
        skew=skew,
        kurtosis=kurt,
        multiple_trials_penalty=multiple_trials_penalty,
        non_normality_penalty=non_normality_penalty,
    )


# ---------------------------------------------------------------------------
# Stationary / block bootstrap p-value
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapPValueResult:
    """Result of the stationary bootstrap significance test.

    Fields:
    - ``p_value``: the fraction of bootstrap resamples where the baseline's
      mean edge is >= the model's mean edge. Small => the model significantly
      beats the baseline. Range [0, 1].
    - ``trial_count``: carried through for auditability.
    - ``n_bootstrap``: the number of bootstrap resamples used.
    - ``model_mean``: the mean of the model's OOS returns.
    - ``baseline_mean``: the mean of the baseline's OOS returns.
    - ``observed_diff``: ``model_mean - baseline_mean``.
    """

    p_value: float
    trial_count: int
    n_bootstrap: int
    model_mean: float
    baseline_mean: float
    observed_diff: float


def _stationary_bootstrap_indices(
    n: int, n_bootstrap: int, p: float, rng: random.Random
) -> list[list[int]]:
    """Generate ``n_bootstrap`` stationary-bootstrap index resamples of length ``n``.

    The stationary bootstrap (Politis & Romano, 1994) resamples blocks of
    random length drawn from a geometric distribution with success
    probability ``p`` (expected block length = 1/p). Blocks wrap around
    (circular) so the resample is always length ``n``. This preserves the
    autocorrelation structure of the original series, unlike an IID
    bootstrap which would destroy it.
    """
    if n == 0:
        return [[] for _ in range(n_bootstrap)]
    resamples: list[list[int]] = []
    for _ in range(n_bootstrap):
        idx: list[int] = []
        pos = rng.randrange(n)
        while len(idx) < n:
            block_len = 0
            while True:
                block_len += 1
                if rng.random() < p or block_len >= n:
                    break
            for _b in range(block_len):
                if len(idx) >= n:
                    break
                idx.append(pos % n)
                pos += 1
            pos = rng.randrange(n)
        resamples.append(idx[:n])
    return resamples


def stationary_bootstrap_pvalue(
    model_returns: list[float],
    baseline_returns: list[float],
    trial_count: int,
    n_bootstrap: int = 500,
    seed: int = 0,
    expected_block_len: int | None = None,
) -> BootstrapPValueResult:
    """Stationary-block-bootstrap p-value of model edge vs. baseline.

    The null hypothesis is that the model's mean OOS edge is NOT greater than
    the baseline's. The test statistic is the difference in means
    (model_mean - baseline_mean). We resample the EDGE series (model -
    baseline) directly so the paired structure is preserved (both series are
    aligned in time — same OOS horizons).

    The p-value is the fraction of resamples where the resampled edge mean
    <= 0 (model does NOT beat baseline under resampling). Small p => the
    model significantly beats the baseline.

    Deterministic given a fixed ``seed`` (uses ``random.Random(seed)``).
    """
    n = len(model_returns)
    if n == 0 or len(baseline_returns) != n:
        raise ValueError("model_returns and baseline_returns must be non-empty and equal length")
    if n_bootstrap <= 0:
        raise ValueError("n_bootstrap must be positive")

    rng = random.Random(seed)
    if expected_block_len is None:
        expected_block_len = max(2, n // 10)
    p = 1.0 / max(expected_block_len, 1)

    model_mean = statistics.fmean(model_returns)
    baseline_mean = statistics.fmean(baseline_returns)
    observed_diff = model_mean - baseline_mean

    edge_series = [model_returns[i] - baseline_returns[i] for i in range(n)]

    resamples = _stationary_bootstrap_indices(n, n_bootstrap, p, rng)
    ge_zero = 0
    for idx in resamples:
        if not idx:
            continue
        resampled_edge_mean = statistics.fmean(edge_series[i] for i in idx)
        if resampled_edge_mean <= 0.0:
            ge_zero += 1
    p_value = ge_zero / n_bootstrap

    return BootstrapPValueResult(
        p_value=p_value,
        trial_count=trial_count,
        n_bootstrap=n_bootstrap,
        model_mean=model_mean,
        baseline_mean=baseline_mean,
        observed_diff=observed_diff,
    )
