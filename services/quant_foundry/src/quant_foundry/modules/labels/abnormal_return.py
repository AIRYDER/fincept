"""
quant_foundry.modules.labels.abnormal_return — abnormal-return label computer.

This is the label module that replaces the existing "subsequent news
event" label with actual market response: **abnormal return** =
``asset_return − β · benchmark_return`` at multiple horizons.

It reuses the abnormal-return math from
``experiments/news-impact-model/src/news_impact_model/labels.py``
(``label_event_impact``) but operates on the module system's
:class:`FeatureRowData` + :class:`PriceBar` types.

β (beta) is estimated from a rolling window of *past* returns ending
**before** the decision time — no look-ahead.  The default window is
252 trading days (~1 year).

Horizons: ``+1d, +5d, +21d, +63d`` (1 day, 1 week, 1 month, 1 quarter).
The primary label is the +5d abnormal return; other horizons are
recorded as extra columns for multi-horizon analysis.

This module is registered as ``label:abnormal-return:1.0.0``.
"""

from __future__ import annotations

import math
from typing import Any

from quant_foundry.modules.registry import (
    FeatureRowData,
    ModuleInfo,
    PriceBar,
    register_module,
)

NS_PER_DAY = 86_400_000_000_000

#: Default horizons in days: 1d, 1w, 1m, 1q.
DEFAULT_HORIZON_DAYS: tuple[int, ...] = (1, 5, 21, 63)

#: Default β estimation window in trading days (~1 year).
DEFAULT_BETA_WINDOW = 252


@register_module(
    "label",
    "abnormal-return",
    "1.0.0",
    default_config={
        "horizon_days": list(DEFAULT_HORIZON_DAYS),
        "primary_horizon": 5,
        "beta_window": DEFAULT_BETA_WINDOW,
        "min_beta_window": 60,
    },
)
class AbnormalReturnLabel:
    """Compute abnormal-return labels for feature rows.

    For each feature row at ``(symbol, decision_time)``:
    1. Find the asset price at or before ``decision_time`` (base price).
    2. Find the benchmark price at or before ``decision_time`` (base benchmark).
    3. Estimate β from the trailing ``beta_window`` days of returns
       ending *before* ``decision_time`` (no look-ahead).
    4. For each horizon ``h``:
       - Find the asset price at ``decision_time + h·NS_PER_DAY``.
       - Find the benchmark price at the same future time.
       - ``abnormal_return[h] = asset_return − β · benchmark_return``
    5. The primary label (used for training) is the abnormal return at
       ``primary_horizon``.  Other horizons are added as extra feature
       columns ``ar_<h>d`` so the model / attribution report can
       analyze multi-horizon response.

    Rows without enough price history (no base price, no β window, or
    no future price at the primary horizon) are dropped.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.horizon_days: tuple[int, ...] = tuple(
            self.config.get("horizon_days", DEFAULT_HORIZON_DAYS),
        )
        self.primary_horizon: int = self.config.get("primary_horizon", 5)
        self.beta_window: int = self.config.get("beta_window", DEFAULT_BETA_WINDOW)
        self.min_beta_window: int = self.config.get("min_beta_window", 60)

    def compute_labels(
        self,
        rows: list[FeatureRowData],
        *,
        price_bars: dict[str, list[PriceBar]],
        benchmark_bars: list[PriceBar],
    ) -> list[FeatureRowData]:
        """Add abnormal-return labels to feature rows.

        Returns a new list of :class:`FeatureRowData` with ``label`` set
        and extra ``ar_<h>d`` columns added to ``features``.  Rows that
        can't be labeled (insufficient price history) are dropped.
        """
        # Pre-sort benchmark bars by timestamp for efficient lookup.
        bench_sorted = sorted(benchmark_bars, key=lambda b: b.ts_ns)
        bench_ts = [b.ts_ns for b in bench_sorted]
        bench_close = [b.close for b in bench_sorted]

        # Pre-sort each symbol's bars.
        asset_sorted: dict[str, list[PriceBar]] = {}
        for sym, bars in price_bars.items():
            asset_sorted[sym] = sorted(bars, key=lambda b: b.ts_ns)

        labeled: list[FeatureRowData] = []
        for row in rows:
            sym = row.symbol
            dt = row.decision_time
            bars = asset_sorted.get(sym)
            if not bars:
                continue

            # --- base prices (last at or before decision_time) ----------
            base_asset = _last_at_or_before(bars, dt)
            base_bench_idx = _last_idx_at_or_before(bench_ts, dt)
            if base_asset is None or base_bench_idx is None:
                continue
            base_bench_price = bench_close[base_bench_idx]

            # --- β estimation (trailing window, no look-ahead) ----------
            beta = _estimate_beta(
                bars,
                bench_ts,
                bench_close,
                dt,
                window=self.beta_window,
                min_window=self.min_beta_window,
            )
            if beta is None:
                continue

            # --- abnormal returns at each horizon -----------------------
            ar_values: dict[str, float] = {}
            primary_label: float | None = None
            for h in self.horizon_days:
                target_ts = dt + h * NS_PER_DAY
                asset_future = _first_at_or_after(bars, target_ts)
                bench_future_idx = _first_idx_at_or_after(bench_ts, target_ts)
                if asset_future is None or bench_future_idx is None:
                    continue
                asset_ret = _simple_return(base_asset.close, asset_future.close)
                bench_ret = _simple_return(base_bench_price, bench_close[bench_future_idx])
                ar = round(asset_ret - beta * bench_ret, 12)
                ar_values[f"ar_{h}d"] = ar
                if h == self.primary_horizon:
                    primary_label = ar

            if primary_label is None:
                continue

            # Merge AR columns into features and set label.
            new_features = {**row.features, **ar_values}
            labeled.append(FeatureRowData(
                symbol=row.symbol,
                decision_time=row.decision_time,
                features=new_features,
                label=primary_label,
            ))

        return labeled


# --------------------------------------------------------------------------- #
# Price lookup helpers (mirrors news_impact_model/labels.py)                  #
# --------------------------------------------------------------------------- #


def _last_at_or_before(bars: list[PriceBar], ts_ns: int) -> PriceBar | None:
    """Last bar at or before ``ts_ns``."""
    best: PriceBar | None = None
    for bar in bars:
        if bar.ts_ns <= ts_ns:
            if best is None or bar.ts_ns > best.ts_ns:
                best = bar
    return best


def _first_at_or_after(bars: list[PriceBar], ts_ns: int) -> PriceBar | None:
    """First bar at or after ``ts_ns``."""
    best: PriceBar | None = None
    for bar in bars:
        if bar.ts_ns >= ts_ns:
            if best is None or bar.ts_ns < best.ts_ns:
                best = bar
    return best


def _last_idx_at_or_before(ts_list: list[int], ts_ns: int) -> int | None:
    """Index of the last timestamp at or before ``ts_ns`` (binary search)."""
    import bisect

    idx = bisect.bisect_right(ts_list, ts_ns) - 1
    return idx if idx >= 0 else None


def _first_idx_at_or_after(ts_list: list[int], ts_ns: int) -> int | None:
    """Index of the first timestamp at or after ``ts_ns`` (binary search)."""
    import bisect

    idx = bisect.bisect_left(ts_list, ts_ns)
    return idx if idx < len(ts_list) else None


def _simple_return(start: float, end: float) -> float:
    if start <= 0:
        return 0.0
    return (end / start) - 1.0


def _estimate_beta(
    asset_bars: list[PriceBar],
    bench_ts: list[int],
    bench_close: list[float],
    decision_time: int,
    *,
    window: int,
    min_window: int,
) -> float | None:
    """Estimate β from trailing daily returns ending before ``decision_time``.

    β = Cov(asset_ret, bench_ret) / Var(bench_ret)

    Uses only bars with ``ts_ns < decision_time`` (strictly before —
    no look-ahead).  Requires at least ``min_window`` overlapping
    returns with the benchmark.
    """
    # Collect asset returns in the trailing window.
    asset_returns: list[float] = []
    asset_ts: list[int] = []

    # Build asset daily returns from sorted bars.
    for i in range(1, len(asset_bars)):
        if asset_bars[i].ts_ns >= decision_time:
            break
        prev = asset_bars[i - 1]
        curr = asset_bars[i]
        if prev.close <= 0:
            continue
        asset_returns.append(math.log(curr.close / prev.close))
        asset_ts.append(curr.ts_ns)

    if len(asset_returns) < min_window:
        return None

    # Take only the trailing `window` returns.
    asset_returns = asset_returns[-window:]
    asset_ts = asset_ts[-window:]

    # Match benchmark returns to asset return timestamps (as-of join).
    bench_returns: list[float] = []
    for a_ts in asset_ts:
        idx = _last_idx_at_or_before(bench_ts, a_ts)
        if idx is None or idx < 1:
            bench_returns.append(0.0)
            continue
        prev_bench = bench_close[idx - 1]
        curr_bench = bench_close[idx]
        if prev_bench <= 0:
            bench_returns.append(0.0)
        else:
            bench_returns.append(math.log(curr_bench / prev_bench))

    if len(bench_returns) < min_window:
        return None

    # β = Cov / Var
    n = len(asset_returns)
    mean_a = sum(asset_returns) / n
    mean_b = sum(bench_returns) / n
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(asset_returns, bench_returns, strict=True)) / n
    var_b = sum((b - mean_b) ** 2 for b in bench_returns) / n
    if var_b <= 0:
        return None
    return round(cov / var_b, 6)


__all__ = ["AbnormalReturnLabel", "DEFAULT_HORIZON_DAYS"]
