"""Triple-barrier labeling and meta-labeling for financial ML.

This module implements the labeling methods from López de Prado's
"Advances in Financial Machine Learning" (2018):

  * **Triple-barrier labeling** (Ch. 3): for each bar at time *t*,
    set three barriers around the entry price:

      - **Upper barrier** (profit-take): ``price * (1 + pt_width)``
      - **Lower barrier** (stop-loss): ``price * (1 - sl_width)``
      - **Vertical barrier** (timeout): bar at ``t + horizon_bars``

    The label is determined by which barrier is hit first:

      - Upper hit first → ``+1`` (profit-take)
      - Lower hit first → ``-1`` (stop-loss)
      - Vertical hit first → sign of return at timeout (``+1``, ``-1``, or ``0``)

    Barrier widths are typically scaled by volatility (e.g. daily
    σ) so the barriers adapt to changing market regimes.

  * **Meta-labeling** (Ch. 3, §3.6): a secondary binary classifier
    trained on ``(primary_signal, features) → {0, 1}`` that decides
    *whether to act* on the primary model's directional signal. The
    meta-label is ``1`` if the primary signal was correct (would have
    been profitable) and ``0`` otherwise. This separates the
    "direction" problem from the "should I bet" problem, improving
    precision without sacrificing recall.

Design notes:

  * Pure-Python, no numpy/pandas dependency at module level (imported
    lazily inside functions) so ``fincept_core.datasets`` stays
    importable in lightweight environments.
  * Pydantic v2 models for configuration, consistent with the rest of
    ``fincept_core.datasets``.
  * No imports from ``services/`` — this is a dependency-free
    labeling library.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "BarrierConfig",
    "MetaLabelConfig",
    "TripleBarrierLabel",
    "meta_labels",
    "triple_barrier_labels",
    "volatility_scaled_widths",
]


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #


class BarrierConfig(BaseModel):
    """Configuration for triple-barrier labeling.

    Args:
        profit_take_width: fraction of entry price for the upper barrier.
            E.g. 0.02 means the upper barrier is 2% above entry.
        stop_loss_width: fraction of entry price for the lower barrier.
            E.g. 0.01 means the lower barrier is 1% below entry.
        horizon_bars: maximum number of bars to wait before the
            vertical (timeout) barrier is hit.
        min_volatility: floor for volatility scaling (avoids
            degenerate barriers when σ ≈ 0). Only used when
            ``volatility`` is passed to ``triple_barrier_labels``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profit_take_width: float
    stop_loss_width: float
    horizon_bars: int
    min_volatility: float = 1e-8


class MetaLabelConfig(BaseModel):
    """Configuration for meta-labeling.

    Args:
        side_column: name of the primary model's directional signal
            column (+1 / -1).
        label_column: name of the triple-barrier label column
            (+1 / -1 / 0).
        meta_label_column: name for the output meta-label column.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    side_column: str = "side"
    label_column: str = "label"
    meta_label_column: str = "meta_label"


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


class TripleBarrierLabel(BaseModel):
    """Result of triple-barrier labeling for a single bar.

    Args:
        index: bar index into the input price array.
        label: ``+1`` (profit-take hit), ``-1`` (stop-loss hit),
            or the sign of the return at the vertical barrier
            (``+1``, ``-1``, ``0``).
        barrier_hit: which barrier was hit first — ``"upper"``,
            ``"lower"``, or ``"vertical"``.
        hit_bar: index of the bar where the barrier was hit
            (``index + horizon_bars`` for the vertical barrier).
        entry_price: price at the entry bar.
        exit_price: price at the hit bar.
        return_pct: percentage return from entry to exit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    label: int
    barrier_hit: str
    hit_bar: int
    entry_price: float
    exit_price: float
    return_pct: float


# --------------------------------------------------------------------------- #
# Volatility-scaled barrier widths                                            #
# --------------------------------------------------------------------------- #


def volatility_scaled_widths(
    closes: list[float],
    window: int = 100,
    *,
    profit_take_sigma: float = 1.0,
    stop_loss_sigma: float = 1.0,
    min_volatility: float = 1e-8,
) -> list[tuple[float, float]]:
    """Compute per-bar volatility-scaled barrier widths.

    For each bar *t*, computes the rolling standard deviation of
    log-returns over the last ``window`` bars, then sets:

      * ``profit_take_width = profit_take_sigma * σ_t``
      * ``stop_loss_width = stop_loss_sigma * σ_t``

    This adapts the barriers to changing volatility regimes — wider
    barriers in high-vol periods, tighter in low-vol.

    Args:
        closes: list of close prices (chronological order).
        window: rolling window for volatility estimation.
        profit_take_sigma: multiplier for the upper barrier.
        stop_loss_sigma: multiplier for the lower barrier.
        min_volatility: floor for σ (avoids zero-width barriers).

    Returns:
        List of ``(profit_take_width, stop_loss_width)`` tuples,
        one per bar. Bars before ``window`` use the first available
        volatility estimate.
    """
    n = len(closes)
    if n < 2:
        return [(profit_take_sigma * min_volatility, stop_loss_sigma * min_volatility)] * n

    # Compute log-returns
    log_rets: list[float] = [0.0]
    for i in range(1, n):
        if closes[i - 1] > 0 and closes[i] > 0:
            import math

            log_rets.append(math.log(closes[i] / closes[i - 1]))
        else:
            log_rets.append(0.0)

    widths: list[tuple[float, float]] = []
    for i in range(n):
        start = max(0, i - window + 1)
        slice_rets = log_rets[start : i + 1]
        if len(slice_rets) < 2:
            sigma = min_volatility
        else:
            mean = sum(slice_rets) / len(slice_rets)
            var = sum((r - mean) ** 2 for r in slice_rets) / len(slice_rets)
            import math

            sigma = max(math.sqrt(var), min_volatility)
        pt = profit_take_sigma * sigma
        sl = stop_loss_sigma * sigma
        widths.append((pt, sl))

    return widths


# --------------------------------------------------------------------------- #
# Triple-barrier labeling                                                     #
# --------------------------------------------------------------------------- #


def triple_barrier_labels(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    config: BarrierConfig,
    *,
    per_bar_widths: list[tuple[float, float]] | None = None,
) -> list[TripleBarrierLabel]:
    """Compute triple-barrier labels for a price series.

    For each bar *t*, sets three barriers:

      * Upper: ``close[t] * (1 + pt_width)``
      * Lower: ``close[t] * (1 - sl_width)``
      * Vertical: bar ``t + horizon_bars``

    Scans forward from *t* to find which barrier is hit first. The
    intrabar high/low determines whether the upper or lower barrier
    is touched. If neither is touched before the vertical barrier,
    the label is the sign of the return at ``t + horizon_bars``.

    Args:
        highs: per-bar high prices (chronological).
        lows: per-bar low prices (chronological).
        closes: per-bar close prices (chronological).
        config: barrier configuration (widths + horizon).
        per_bar_widths: optional per-bar ``(pt_width, sl_width)``
            tuples (e.g. from ``volatility_scaled_widths``). When
            ``None``, uses ``config.profit_take_width`` and
            ``config.stop_loss_width`` for all bars.

    Returns:
        One :class:`TripleBarrierLabel` per bar. The last
        ``config.horizon_bars`` bars have no label (insufficient
        future data) and are excluded from the result.

    Raises:
        ValueError: if input lengths mismatch or config is invalid.
    """
    n = len(closes)
    if not (len(highs) == n and len(lows) == n):
        raise ValueError("highs, lows, closes must have equal length")
    if config.profit_take_width <= 0:
        raise ValueError("profit_take_width must be > 0")
    if config.stop_loss_width <= 0:
        raise ValueError("stop_loss_width must be > 0")
    if config.horizon_bars < 1:
        raise ValueError("horizon_bars must be >= 1")

    h = config.horizon_bars
    results: list[TripleBarrierLabel] = []

    for t in range(n - h):
        entry = closes[t]
        if per_bar_widths is not None:
            pt_w, sl_w = per_bar_widths[t]
        else:
            pt_w = config.profit_take_width
            sl_w = config.stop_loss_width

        upper = entry * (1.0 + pt_w)
        lower = entry * (1.0 - sl_w)
        vertical_bar = t + h

        barrier_hit = "vertical"
        hit_bar = vertical_bar
        exit_price = closes[vertical_bar]

        for j in range(t + 1, vertical_bar + 1):
            # Check if the intrabar range touches the upper or lower
            # barrier. If both are touched in the same bar, we assume
            # the upper barrier is hit first (conservative for a long
            # bias; the AFML convention is to use the bar's open or
            # to resolve ambiguity via tick data, which we don't have
            # here).
            if highs[j] >= upper:
                barrier_hit = "upper"
                hit_bar = j
                exit_price = upper
                break
            if lows[j] <= lower:
                barrier_hit = "lower"
                hit_bar = j
                exit_price = lower
                break

        if barrier_hit == "upper":
            label = 1
        elif barrier_hit == "lower":
            label = -1
        else:
            # Vertical barrier: label by sign of return at timeout
            ret = closes[vertical_bar] - entry
            if ret > 0:
                label = 1
            elif ret < 0:
                label = -1
            else:
                label = 0

        return_pct = ((exit_price - entry) / entry) * 100.0 if entry > 0 else 0.0

        results.append(
            TripleBarrierLabel(
                index=t,
                label=label,
                barrier_hit=barrier_hit,
                hit_bar=hit_bar,
                entry_price=entry,
                exit_price=exit_price,
                return_pct=return_pct,
            )
        )

    return results


# --------------------------------------------------------------------------- #
# Meta-labeling                                                               #
# --------------------------------------------------------------------------- #


def meta_labels(
    sides: list[int],
    barrier_labels: list[int],
    config: MetaLabelConfig | None = None,
) -> list[int]:
    """Compute meta-labels from primary signals and triple-barrier labels.

    The meta-label is a binary {0, 1} value that indicates whether the
    primary model's directional signal was correct:

      * ``1`` if ``side == barrier_label`` (the primary signal agreed
        with the realized outcome — the trade would have been profitable)
      * ``0`` if ``side != barrier_label`` (the primary signal was wrong
        — the trade would have lost money)

    This separates the "which direction?" problem (primary model) from
    the "should I bet?" problem (meta-model). The meta-model is trained
    on ``(side, features) → meta_label`` to learn when to trust the
    primary signal.

    Args:
        sides: primary model's directional signals (+1 or -1).
        barrier_labels: triple-barrier labels (+1, -1, or 0).
        config: optional configuration (unused for the core logic
            but kept for API consistency).

    Returns:
        List of meta-labels (0 or 1), one per input pair.

    Raises:
        ValueError: if input lengths mismatch.
    """
    if len(sides) != len(barrier_labels):
        raise ValueError(
            f"sides and barrier_labels must have equal length; "
            f"got {len(sides)} and {len(barrier_labels)}"
        )

    results: list[int] = []
    for side, bl in zip(sides, barrier_labels, strict=True):
        # A vertical-barrier label of 0 (no movement) is treated as
        # "primary was wrong" (meta-label = 0) — no edge, no bet.
        if side == bl and side != 0:
            results.append(1)
        else:
            results.append(0)
    return results
