"""
quant_foundry.modules.benchmark.significance — statistical significance tests.

This module provides the statistical rigor layer for the benchmark
system.  The functions here answer questions that the raw
:class:`ComparisonReport` rankings cannot:

- **Is model A really better than model B, or is the difference within
  noise?**  The :func:`diebold_mariano_test` compares forecast accuracy
  of two models on the same test set with a proper hypothesis test.
- **What is the uncertainty around the deflated Sharpe ratio?**
  :func:`bootstrap_sharpe_ci` resamples the Sharpe estimates to produce
  a confidence interval.
- **Is the difference in Sharpe between two configs statistically
  significant?**  :func:`bootstrap_sharpe_difference_ci` bootstraps the
  *difference* in Sharpe and checks whether 0 is inside the CI.

All functions use pure Python + the :mod:`math` module (no numpy/scipy
at module level).  Bootstrap and permutation routines use
``random.Random(seed)`` for reproducibility.

Usage::

    from quant_foundry.modules.benchmark.significance import (
        diebold_mariano_test,
        bootstrap_sharpe_ci,
        bootstrap_sharpe_difference_ci,
    )

    # Compare two models' forecast accuracy
    dm = diebold_mariano_test(errors_a, errors_b, horizon=5)

    # CI for a single config's Sharpe across seeds
    ci = bootstrap_sharpe_ci([1.2, 1.1, 1.3, 1.0, 1.2])

    # CI for the difference between two configs
    diff = bootstrap_sharpe_difference_ci(sharpe_a, sharpe_b)
    if diff["significant_at_5pct"]:
        print("Config A is significantly different from Config B")
"""

from __future__ import annotations

import math
import random
from typing import Any

__all__ = [
    "bootstrap_sharpe_ci",
    "bootstrap_sharpe_difference_ci",
    "diebold_mariano_test",
]


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function approximation."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _variance(values: list[float], *, ddof: int = 1) -> float:
    if len(values) <= ddof:
        return 0.0
    mu = _mean(values)
    return sum((v - mu) ** 2 for v in values) / (len(values) - ddof)


def _std(values: list[float], *, ddof: int = 1) -> float:
    return math.sqrt(_variance(values, ddof=ddof))


# --------------------------------------------------------------------------- #
# Diebold-Mariano test                                                         #
# --------------------------------------------------------------------------- #


def diebold_mariano_test(
    errors_a: list[float],
    errors_b: list[float],
    *,
    horizon: int = 1,
) -> dict[str, Any]:
    """Diebold-Mariano test for comparing forecast accuracy of two models.

    Compares the forecast errors of two models on the *same* test set.
    A negative DM statistic means model A has smaller errors (is
    better); a positive statistic means model B is better.

    Args:
        errors_a: Forecast errors (actual - predicted) from model A.
        errors_b: Forecast errors (actual - predicted) from model B,
            aligned with ``errors_a`` (same test set, same order).
        horizon: Forecast horizon in steps.  Adjusts the variance via a
            Newey-West style correction for h-step-ahead forecasts.

    Returns:
        A dict with keys:
            - ``dm_stat``: the DM statistic.
            - ``p_value``: two-sided p-value (normal approximation).
            - ``significant_at_5pct``: whether ``p_value < 0.05``.
            - ``better_model``: ``"a"``, ``"b"``, or ``"tie"``.

    Raises:
        ValueError: If the error lists have different lengths or are
            too short.
    """
    if len(errors_a) != len(errors_b):
        raise ValueError(
            f"error lists must have the same length; got {len(errors_a)} and {len(errors_b)}",
        )
    n = len(errors_a)
    if n < 2:
        raise ValueError(f"need at least 2 error pairs; got {n}")

    # Loss differential: d = |e_a| - |e_b|
    # (absolute error loss; could also use squared error)
    d = [abs(ea) - abs(eb) for ea, eb in zip(errors_a, errors_b, strict=True)]

    d_mean = _mean(d)

    # Newey-West style variance estimator for h-step-ahead forecasts.
    # For horizon h, account for autocorrelation up to lag h-1.
    h = max(1, horizon)
    n_eff = len(d)

    # Base variance (sample variance of d)
    base_var = _variance(d, ddof=1)

    # Newey-West correction: add autocovariance terms for lags 1..h-1
    nw_correction = 0.0
    for lag in range(1, h):
        weight = 1.0 - lag / h  # Bartlett kernel weight
        cov = 0.0
        count = n_eff - lag
        if count > 0:
            for i in range(count):
                cov += (d[i] - d_mean) * (d[i + lag] - d_mean)
            cov /= n_eff
        nw_correction += weight * cov

    var_d = (base_var + 2.0 * nw_correction) / n_eff

    if var_d <= 0.0:
        # No variance in differentials — models are statistically
        # indistinguishable on this metric.
        dm_stat = 0.0
        p_value = 1.0
    else:
        dm_stat = d_mean / math.sqrt(var_d)
        # Two-sided p-value via normal approximation
        p_value = 2.0 * (1.0 - _normal_cdf(abs(dm_stat)))

    significant = p_value < 0.05

    if not significant:
        better_model = "tie"
    elif dm_stat < 0:
        # d = |e_a| - |e_b| < 0 → A has smaller errors → A is better
        better_model = "a"
    else:
        better_model = "b"

    return {
        "dm_stat": float(dm_stat),
        "p_value": float(p_value),
        "significant_at_5pct": bool(significant),
        "better_model": better_model,
    }


# --------------------------------------------------------------------------- #
# Bootstrap Sharpe CI                                                          #
# --------------------------------------------------------------------------- #


def bootstrap_sharpe_ci(
    sharpe_ratios: list[float],
    *,
    n_bootstrap: int = 10000,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, Any]:
    """Bootstrap confidence interval for the Sharpe ratio.

    Resamples the input Sharpe ratios with replacement and computes the
    mean of each bootstrap sample.  The CI is computed via the
    percentile method.

    Args:
        sharpe_ratios: List of Sharpe ratio estimates (e.g. from
            multiple seeds).
        n_bootstrap: Number of bootstrap resamples.
        confidence: Confidence level (e.g. 0.95 for 95% CI).
        seed: Random seed for reproducibility.

    Returns:
        A dict with keys:
            - ``mean``: mean of the input Sharpe ratios.
            - ``std``: bootstrap standard error of the mean.
            - ``lower``: lower bound of the CI.
            - ``upper``: upper bound of the CI.
            - ``confidence``: the confidence level used.

    Raises:
        ValueError: If ``sharpe_ratios`` is empty.
    """
    if not sharpe_ratios:
        raise ValueError("sharpe_ratios must not be empty")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1); got {confidence}")

    rng = random.Random(seed)
    n = len(sharpe_ratios)

    bootstrap_means: list[float] = []
    for _ in range(n_bootstrap):
        sample = [sharpe_ratios[rng.randrange(n)] for _ in range(n)]
        bootstrap_means.append(_mean(sample))

    bootstrap_means.sort()

    # Percentile method
    alpha = 1.0 - confidence
    lower_idx = max(0, int(math.floor((alpha / 2.0) * n_bootstrap)))
    upper_idx = min(n_bootstrap - 1, int(math.ceil((1.0 - alpha / 2.0) * n_bootstrap)) - 1)

    return {
        "mean": float(_mean(sharpe_ratios)),
        "std": float(_std(bootstrap_means, ddof=1)),
        "lower": float(bootstrap_means[lower_idx]),
        "upper": float(bootstrap_means[upper_idx]),
        "confidence": float(confidence),
    }


# --------------------------------------------------------------------------- #
# Bootstrap Sharpe difference CI                                               #
# --------------------------------------------------------------------------- #


def bootstrap_sharpe_difference_ci(
    sharpe_a: list[float],
    sharpe_b: list[float],
    *,
    n_bootstrap: int = 10000,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, Any]:
    """Bootstrap CI for the difference in Sharpe ratios between two configs.

    Resamples both lists independently and computes
    ``mean(a) - mean(b)`` for each bootstrap iteration.  The difference
    is significant at the 5% level if 0 is not in the 95% CI.

    Args:
        sharpe_a: Sharpe ratios from config A (e.g. across seeds).
        sharpe_b: Sharpe ratios from config B.
        n_bootstrap: Number of bootstrap resamples.
        confidence: Confidence level.
        seed: Random seed for reproducibility.

    Returns:
        A dict with keys:
            - ``mean_diff``: observed mean difference (mean(a) - mean(b)).
            - ``std_diff``: bootstrap standard error of the difference.
            - ``lower``: lower bound of the CI.
            - ``upper``: upper bound of the CI.
            - ``significant_at_5pct``: whether 0 is outside the CI.

    Raises:
        ValueError: If either list is empty.
    """
    if not sharpe_a:
        raise ValueError("sharpe_a must not be empty")
    if not sharpe_b:
        raise ValueError("sharpe_b must not be empty")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1); got {confidence}")

    rng = random.Random(seed)
    na, nb = len(sharpe_a), len(sharpe_b)

    diffs: list[float] = []
    for _ in range(n_bootstrap):
        sample_a = [sharpe_a[rng.randrange(na)] for _ in range(na)]
        sample_b = [sharpe_b[rng.randrange(nb)] for _ in range(nb)]
        diffs.append(_mean(sample_a) - _mean(sample_b))

    diffs.sort()

    alpha = 1.0 - confidence
    lower_idx = max(0, int(math.floor((alpha / 2.0) * n_bootstrap)))
    upper_idx = min(n_bootstrap - 1, int(math.ceil((1.0 - alpha / 2.0) * n_bootstrap)) - 1)

    lower = diffs[lower_idx]
    upper = diffs[upper_idx]
    observed_diff = _mean(sharpe_a) - _mean(sharpe_b)

    # Significant if 0 is NOT in the CI
    significant = (lower > 0.0) or (upper < 0.0)

    return {
        "mean_diff": float(observed_diff),
        "std_diff": float(_std(diffs, ddof=1)),
        "lower": float(lower),
        "upper": float(upper),
        "significant_at_5pct": bool(significant),
    }
