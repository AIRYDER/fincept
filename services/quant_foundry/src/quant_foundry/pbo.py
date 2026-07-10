"""
quant_foundry.pbo — Probability of Backtest Overfitting (TASK-0406).

Implements the Combinatorially Symmetric Cross-Validation (CSCV) method from
Bailey, Borwein, López de Prado & Zhu (2017), "The Probability of Backtest
Overfitting" (Journal of Finance and Data Science).

The PBO is the probability that an optimal in-sample (IS) strategy
underperforms the median out-of-sample (OOS) strategy. A high PBO means the
family is likely overfit: the best-looking IS strategy is no better than
median OOS, so the IS ranking is not informative.

Method (simplified for the MVP skeleton):
1. Split the IS + OOS return series for N candidates into S partitions
   (default 16). For each partition split point, form a "combination" by
   assigning the first half of partitions to IS and the second half to OOS
   (combinatorially symmetric).
2. For each combination, rank the N candidates by IS Sharpe and by OOS
   Sharpe. Find the IS-optimal candidate and check whether its OOS rank is
   below the median (i.e. it underperforms the median OOS strategy).
3. PBO = fraction of combinations where the IS-optimal candidate
   underperforms the median OOS. The logit transform ``ln(pbo/(1-pbo))``
   is also reported (it is more interpretable for extreme values).

The implementation is stdlib-only (seeded ``random.Random`` for any
subsampling) and deterministic given a fixed seed. It operates on plain
``list[list[float]]`` return matrices (IS and OOS), NOT on settlement
records or dossiers — the caller is responsible for mapping evidence into
the return matrix.

File-disjoint from all active builders (see BUILDER3.md).
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class PBOResult:
    """Result of the PBO computation.

    Fields:
    - ``pbo``: the Probability of Backtest Overfitting in [0, 1]. High =>
      the family is likely overfit (the IS-optimal strategy underperforms
      the median OOS strategy in most combinations).
    - ``logit``: ``ln(pbo / (1 - pbo))``. More interpretable for extreme
      values (pbo near 0 or 1). Positive => overfit; negative => not overfit.
    - ``n_candidates``: the number of candidates in the family.
    - ``n_combinations``: the number of CSCV combinations evaluated.
    - ``threshold``: the PBO threshold above which the family is flagged.
    - ``flagged``: True if ``pbo > threshold``.
    """

    pbo: float
    logit: float
    n_candidates: int
    n_combinations: int
    threshold: float
    flagged: bool


def _sharpe(returns: list[float]) -> float:
    """Per-period Sharpe ratio (mean / std). 0 if std is 0."""
    if not returns:
        return 0.0
    mean = statistics.fmean(returns)
    n = len(returns)
    if n < 2:
        return 0.0
    var = sum((r - mean) ** 2 for r in returns) / n
    std = math.sqrt(var)
    if std == 0.0:
        return 0.0
    return mean / std


def probability_of_backtest_overfitting(
    is_returns: list[list[float]],
    oos_returns: list[list[float]],
    n_partitions: int = 16,
    seed: int = 0,
    threshold: float = 0.1,
) -> PBOResult:
    """Compute the Probability of Backtest Overfitting (CSCV method).

    Args:
    - ``is_returns``: N x T_in matrix of in-sample returns for N candidates.
    - ``oos_returns``: N x T_out matrix of out-of-sample returns for N candidates.
    - ``n_partitions``: the number of partitions to split the combined series
      into (default 16). More partitions => more combinations => more stable
      PBO but slower.
    - ``seed``: for any subsampling (deterministic).
    - ``threshold``: PBO above this flags the family as overfit.

    Returns a ``PBOResult`` with the PBO, logit, and flagged boolean.

    The method:
    1. For each candidate, concatenate IS + OOS returns into a single series
       of length T = T_in + T_out.
    2. Split T into ``n_partitions`` equal blocks.
    3. For each combination (choosing half the blocks as "IS" and half as
       "OOS"), compute the IS Sharpe and OOS Sharpe for each candidate.
    4. Find the IS-optimal candidate; check if its OOS Sharpe rank is below
       the median. If so, this combination counts as "overfit".
    5. PBO = fraction of overfit combinations.

    For the MVP skeleton we use a subset of combinations (sampled
    deterministically with the seed) to keep the computation tractable.
    The full CSCV enumerates all C(n_partitions, n_partitions/2) combinations,
    which is exponential; we sample at most 1000 combinations.
    """
    n_candidates = len(is_returns)
    if n_candidates == 0 or len(oos_returns) != n_candidates:
        raise ValueError("is_returns and oos_returns must be non-empty and equal length")
    if n_candidates == 1:
        # A single candidate cannot be overfit by definition.
        return PBOResult(
            pbo=0.0,
            logit=-float("inf"),
            n_candidates=1,
            n_combinations=0,
            threshold=threshold,
            flagged=False,
        )

    # Concatenate IS + OOS for each candidate.
    combined = [is_returns[i] + oos_returns[i] for i in range(n_candidates)]
    total_len = len(combined[0])
    if total_len < n_partitions:
        # Not enough data for the requested partitions; use fewer.
        n_partitions = max(2, total_len)

    block_size = total_len // n_partitions
    if block_size == 0:
        block_size = 1

    # Generate combinations: choose n_partitions//2 blocks as "IS".
    half = n_partitions // 2
    if half == 0:
        half = 1

    rng = random.Random(seed)

    # Sample combinations (full enumeration is exponential).
    max_combos = 1000
    n_combos = min(
        max_combos,
        _n_choose_k(n_partitions, half),
    )

    # Generate distinct combinations deterministically.
    combos = _sample_combinations(n_partitions, half, n_combos, rng)

    underperform_count = 0
    n_evaluated = 0
    for combo in combos:
        is_blocks = set(combo)
        # For each candidate, compute IS and OOS Sharpe from the blocks.
        is_sharpes: list[float] = []
        oos_sharpes: list[float] = []
        for c in range(n_candidates):
            is_ret = []
            oos_ret = []
            for b in range(n_partitions):
                start = b * block_size
                end = start + block_size
                block = combined[c][start:end]
                if b in is_blocks:
                    is_ret.extend(block)
                else:
                    oos_ret.extend(block)
            is_sharpes.append(_sharpe(is_ret))
            oos_sharpes.append(_sharpe(oos_ret))

        # Find the IS-optimal candidate.
        is_best = max(range(n_candidates), key=lambda i: is_sharpes[i])
        # Check if its OOS Sharpe is below the median.
        median_oos = statistics.median(oos_sharpes)
        if oos_sharpes[is_best] < median_oos:
            underperform_count += 1
        n_evaluated += 1

    pbo = 0.0 if n_evaluated == 0 else underperform_count / n_evaluated

    # Logit transform (clamp to avoid log(0)).
    pbo_clamped = min(max(pbo, 1e-10), 1.0 - 1e-10)
    logit = math.log(pbo_clamped / (1.0 - pbo_clamped))

    return PBOResult(
        pbo=pbo,
        logit=logit,
        n_candidates=n_candidates,
        n_combinations=n_evaluated,
        threshold=threshold,
        flagged=pbo > threshold,
    )


def _n_choose_k(n: int, k: int) -> int:
    """Compute C(n, k) without overflow for small n."""
    if k < 0 or k > n:
        return 0
    k = min(k, n - k)
    result = 1
    for i in range(k):
        result = result * (n - i) // (i + 1)
    return result


def _sample_combinations(n: int, k: int, count: int, rng: random.Random) -> list[tuple[int, ...]]:
    """Sample ``count`` distinct k-subsets of {0, ..., n-1} deterministically."""
    total = _n_choose_k(n, k)
    if count >= total:
        # Enumerate all.
        return _enumerate_combinations(n, k)
    # Sample distinct subsets.
    seen: set[tuple[int, ...]] = set()
    result: list[tuple[int, ...]] = []
    while len(result) < count:
        combo = tuple(sorted(rng.sample(range(n), k)))
        if combo not in seen:
            seen.add(combo)
            result.append(combo)
    return result


def _enumerate_combinations(n: int, k: int) -> list[tuple[int, ...]]:
    """Enumerate all k-subsets of {0, ..., n-1}."""
    result: list[tuple[int, ...]] = []

    def _recurse(start: int, chosen: list[int]) -> None:
        if len(chosen) == k:
            result.append(tuple(chosen))
            return
        for i in range(start, n):
            chosen.append(i)
            _recurse(i + 1, chosen)
            chosen.pop()

    _recurse(0, [])
    return result
