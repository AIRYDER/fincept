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

v1.1.0 improvements over v1.0.0:
- **Bayesian β shrinkage** (Vasicek): shrinks OLS β toward a prior
  (default 1.0) with configurable shrinkage weight. More robust for
  short windows and thin-trading symbols.
- **Trading-day horizons**: horizons count actual trading bars, not
  calendar days. A +5d horizon on Friday is 5 trading sessions, not
  5 calendar days spanning a weekend.
- **Return type option**: ``close_to_close`` (default) or
  ``open_to_close`` (intraday only — excludes overnight gaps, which
  is better for media sentiment that breaks during the trading day).
- **CAR (cumulative abnormal return) variant**: sums daily abnormal
  returns over the horizon instead of using a single endpoint return.
  More robust to outlier days within the window.
- **Thin-trading guard**: drops symbols with too few bars in the β
  window (configurable via ``min_beta_window``).

This module is registered as:
- ``label:abnormal-return:1.0.0`` (original, calendar-day, close-to-close)
- ``label:abnormal-return:1.1.0`` (improved, trading-day, configurable)
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

#: Default horizons in trading days: 1d, 1w, 1m, 1q.
DEFAULT_HORIZON_DAYS: tuple[int, ...] = (1, 5, 21, 63)

#: Default β estimation window in trading days (~1 year).
DEFAULT_BETA_WINDOW = 252

#: Default β prior for Vasicek shrinkage (market β).
DEFAULT_BETA_PRIOR = 1.0

#: Default Vasicek shrinkage weight in [0, 1]. 0 = pure OLS, 1 = pure prior.
#: 0.3 means 30% prior + 70% OLS — mild shrinkage.
DEFAULT_SHRINKAGE = 0.3


# --------------------------------------------------------------------------- #
# v1.1.0 — improved label with Bayesian β, trading-day horizons, CAR, etc.    #
# --------------------------------------------------------------------------- #


@register_module(
    "label",
    "abnormal-return",
    "1.1.0",
    default_config={
        "horizon_days": list(DEFAULT_HORIZON_DAYS),
        "primary_horizon": 5,
        "beta_window": DEFAULT_BETA_WINDOW,
        "min_beta_window": 60,
        "beta_prior": DEFAULT_BETA_PRIOR,
        "shrinkage": DEFAULT_SHRINKAGE,
        "return_type": "close_to_close",  # or "open_to_close"
        "ar_method": "endpoint",  # or "car" (cumulative abnormal return)
    },
)
class AbnormalReturnLabel:
    """Compute abnormal-return labels for feature rows (v1.1.0 — improved).

    For each feature row at ``(symbol, decision_time)``:
    1. Find the asset bar at or before ``decision_time`` (base bar).
    2. Find the benchmark bar at or before ``decision_time`` (base benchmark).
    3. Estimate β from the trailing ``beta_window`` trading bars ending
       *before* ``decision_time`` (no look-ahead), using Vasicek
       Bayesian shrinkage toward ``beta_prior``.
    4. For each horizon ``h`` (in trading days):
       - Find the asset bar ``h`` trading sessions after the base bar.
       - Find the benchmark bar at the same future time.
       - Compute returns based on ``return_type``:
         - ``close_to_close``: close[n] / close[0] - 1
         - ``open_to_close``: (close[n] / open[0] - 1) for the first
           session, then close-to-close for subsequent sessions
       - Compute abnormal return based on ``ar_method``:
         - ``endpoint``: asset_return − β · benchmark_return (single endpoint)
         - ``car``: sum of daily (asset_ret_t − β · bench_ret_t) over
           the horizon (cumulative abnormal return — more robust to
           outlier days)
    5. The primary label (used for training) is the abnormal return at
       ``primary_horizon``.  Other horizons are added as extra feature
       columns ``ar_<h>d`` so the model / attribution report can
       analyze multi-horizon response.

    Rows without enough price history (no base bar, no β window, or
    no future bar at the primary horizon) are dropped.
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
        self.beta_prior: float = self.config.get("beta_prior", DEFAULT_BETA_PRIOR)
        self.shrinkage: float = self.config.get("shrinkage", DEFAULT_SHRINKAGE)
        self.return_type: str = self.config.get("return_type", "close_to_close")
        self.ar_method: str = self.config.get("ar_method", "endpoint")

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

            # --- base bar (last at or before decision_time) ------------
            base_idx = _last_idx_at_or_before([b.ts_ns for b in bars], dt)
            if base_idx is None:
                continue
            base_asset = bars[base_idx]

            base_bench_idx = _last_idx_at_or_before(bench_ts, dt)
            if base_bench_idx is None:
                continue

            # --- β estimation (trailing window, no look-ahead) ----------
            beta = _estimate_beta_v2(
                bars,
                bench_sorted,
                dt,
                window=self.beta_window,
                min_window=self.min_beta_window,
                beta_prior=self.beta_prior,
                shrinkage=self.shrinkage,
            )
            if beta is None:
                continue

            # --- abnormal returns at each horizon (trading-day based) ---
            ar_values: dict[str, float] = {}
            primary_label: float | None = None
            for h in self.horizon_days:
                ar = self._compute_ar_for_horizon(
                    bars,
                    bench_sorted,
                    base_idx,
                    base_bench_idx,
                    h,
                    beta,
                )
                if ar is None:
                    continue
                ar_values[f"ar_{h}d"] = ar
                if h == self.primary_horizon:
                    primary_label = ar

            if primary_label is None:
                continue

            # Merge AR columns into features and set label.
            new_features = {**row.features, **ar_values}
            labeled.append(
                FeatureRowData(
                    symbol=row.symbol,
                    decision_time=row.decision_time,
                    features=new_features,
                    label=primary_label,
                )
            )

        return labeled

    def _compute_ar_for_horizon(
        self,
        bars: list[PriceBar],
        bench_sorted: list[PriceBar],
        base_idx: int,
        base_bench_idx: int,
        horizon: int,
        beta: float,
    ) -> float | None:
        """Compute abnormal return for a single horizon (trading-day based).

        Uses ``h`` trading sessions after the base bar, not ``h`` calendar
        days.  This ensures a +5d horizon is always 5 trading sessions
        regardless of weekends/holidays.
        """
        future_idx = base_idx + horizon
        if future_idx >= len(bars):
            return None

        # Find benchmark bar at or after the future asset bar's timestamp.
        future_asset_ts = bars[future_idx].ts_ns
        future_bench_idx = _first_idx_at_or_after(
            [b.ts_ns for b in bench_sorted],
            future_asset_ts,
        )
        if future_bench_idx is None:
            return None

        if self.ar_method == "car":
            return self._compute_car(
                bars,
                bench_sorted,
                base_idx,
                base_bench_idx,
                beta,
                horizon,
            )
        else:
            return self._compute_endpoint_ar(
                bars,
                bench_sorted,
                base_idx,
                future_idx,
                base_bench_idx,
                future_bench_idx,
                beta,
            )

    def _compute_endpoint_ar(
        self,
        bars: list[PriceBar],
        bench_sorted: list[PriceBar],
        base_idx: int,
        future_idx: int,
        base_bench_idx: int,
        future_bench_idx: int,
        beta: float,
    ) -> float | None:
        """Endpoint abnormal return: asset_return − β · benchmark_return."""
        asset_ret = self._compute_return(bars, base_idx, future_idx)
        bench_ret = self._compute_return(bench_sorted, base_bench_idx, future_bench_idx)

        if asset_ret is None or bench_ret is None:
            return None

        return round(asset_ret - beta * bench_ret, 12)

    def _compute_car(
        self,
        bars: list[PriceBar],
        bench_sorted: list[PriceBar],
        base_idx: int,
        base_bench_idx: int,
        beta: float,
        horizon: int,
    ) -> float | None:
        """Cumulative abnormal return: sum of daily AR over the horizon.

        CAR = Σ_{t=1}^{h} (asset_ret_t − β · bench_ret_t)

        More robust than endpoint AR because it doesn't let a single
        outlier day dominate the label.
        """
        car = 0.0
        for i in range(1, horizon + 1):
            curr_asset_idx = base_idx + i
            if curr_asset_idx >= len(bars):
                return None

            # Daily asset return
            if self.return_type == "open_to_close" and i == 1:
                # First session: open-to-close
                if bars[base_idx].open <= 0:
                    return None
                asset_ret = (bars[curr_asset_idx].close / bars[base_idx].open) - 1.0
            else:
                prev_asset = bars[curr_asset_idx - 1]
                curr_asset = bars[curr_asset_idx]
                if prev_asset.close <= 0:
                    return None
                asset_ret = math.log(curr_asset.close / prev_asset.close)

            # Match benchmark daily return
            curr_asset_ts = bars[curr_asset_idx].ts_ns
            curr_bench_idx = _first_idx_at_or_after(
                [b.ts_ns for b in bench_sorted],
                curr_asset_ts,
            )
            if curr_bench_idx is None or curr_bench_idx < 1:
                continue

            prev_bench = bench_sorted[curr_bench_idx - 1]
            curr_bench = bench_sorted[curr_bench_idx]
            if prev_bench.close <= 0:
                continue
            bench_ret = math.log(curr_bench.close / prev_bench.close)

            car += asset_ret - beta * bench_ret

        return round(car, 12)

    def _compute_return(
        self,
        bars: list[PriceBar],
        start_idx: int,
        end_idx: int,
    ) -> float | None:
        """Compute return between two bar indices based on return_type."""
        if start_idx >= len(bars) or end_idx >= len(bars):
            return None

        if self.return_type == "open_to_close":
            # First session: open-to-close, then close-to-close
            start_price = bars[start_idx].open
            if start_price <= 0:
                return None
            end_price = bars[end_idx].close
            return (end_price / start_price) - 1.0
        else:
            # close-to-close
            start_price = bars[start_idx].close
            if start_price <= 0:
                return None
            end_price = bars[end_idx].close
            return (end_price / start_price) - 1.0


# --------------------------------------------------------------------------- #
# v1.0.0 — original label (kept for backward compatibility)                   #
# --------------------------------------------------------------------------- #


@register_module(
    "label",
    "abnormal-return-v1",
    "1.0.0",
    default_config={
        "horizon_days": list(DEFAULT_HORIZON_DAYS),
        "primary_horizon": 5,
        "beta_window": DEFAULT_BETA_WINDOW,
        "min_beta_window": 60,
    },
)
class AbnormalReturnLabelV1:
    """v1.0.0 — original abnormal-return label (calendar-day, close-to-close, OLS β).

    Kept for backward compatibility and A/B comparison against v1.1.0.
    Use ``label:abnormal-return:1.1.0`` for improved labels.
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
        """Add abnormal-return labels to feature rows (v1.0.0 algorithm)."""
        bench_sorted = sorted(benchmark_bars, key=lambda b: b.ts_ns)
        bench_ts = [b.ts_ns for b in bench_sorted]
        bench_close = [b.close for b in bench_sorted]

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

            base_asset = _last_at_or_before(bars, dt)
            base_bench_idx = _last_idx_at_or_before(bench_ts, dt)
            if base_asset is None or base_bench_idx is None:
                continue
            base_bench_price = bench_close[base_bench_idx]

            beta = _estimate_beta_v1(
                bars,
                bench_ts,
                bench_close,
                dt,
                window=self.beta_window,
                min_window=self.min_beta_window,
            )
            if beta is None:
                continue

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

            new_features = {**row.features, **ar_values}
            labeled.append(
                FeatureRowData(
                    symbol=row.symbol,
                    decision_time=row.decision_time,
                    features=new_features,
                    label=primary_label,
                )
            )

        return labeled


# --------------------------------------------------------------------------- #
# β estimation functions                                                       #
# --------------------------------------------------------------------------- #


def _estimate_beta_v2(
    asset_bars: list[PriceBar],
    bench_sorted: list[PriceBar],
    decision_time: int,
    *,
    window: int,
    min_window: int,
    beta_prior: float,
    shrinkage: float,
) -> float | None:
    """Estimate β with Vasicek Bayesian shrinkage (v1.1.0).

    β_shrunk = (1 - λ) · β_OLS + λ · β_prior

    where λ is the shrinkage weight. This is more robust than pure OLS
    for short windows or thin-trading symbols.

    Also includes a thin-trading guard: if the asset has fewer than
    ``min_window`` bars in the trailing window, returns None.
    """
    # Collect asset log returns in the trailing window.
    asset_returns: list[float] = []
    asset_ts: list[int] = []

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
    bench_ts_list = [b.ts_ns for b in bench_sorted]
    bench_close_list = [b.close for b in bench_sorted]
    bench_returns: list[float] = []
    for a_ts in asset_ts:
        idx = _last_idx_at_or_before(bench_ts_list, a_ts)
        if idx is None or idx < 1:
            bench_returns.append(0.0)
            continue
        prev_bench = bench_close_list[idx - 1]
        curr_bench = bench_close_list[idx]
        if prev_bench <= 0:
            bench_returns.append(0.0)
        else:
            bench_returns.append(math.log(curr_bench / prev_bench))

    if len(bench_returns) < min_window:
        return None

    # OLS β = Cov / Var
    n = len(asset_returns)
    mean_a = sum(asset_returns) / n
    mean_b = sum(bench_returns) / n
    cov = (
        sum((a - mean_a) * (b - mean_b) for a, b in zip(asset_returns, bench_returns, strict=True))
        / n
    )
    var_b = sum((b - mean_b) ** 2 for b in bench_returns) / n
    if var_b <= 0:
        return None

    beta_ols = cov / var_b

    # Vasicek shrinkage: β_shrunk = (1 - λ) · β_OLS + λ · β_prior
    # Clamp shrinkage to [0, 1]
    lam = max(0.0, min(1.0, shrinkage))
    beta_shrunk = (1.0 - lam) * beta_ols + lam * beta_prior

    return round(beta_shrunk, 6)


def _estimate_beta_v1(
    asset_bars: list[PriceBar],
    bench_ts: list[int],
    bench_close: list[float],
    decision_time: int,
    *,
    window: int,
    min_window: int,
) -> float | None:
    """Estimate β from trailing daily returns (v1.0.0 — pure OLS, no shrinkage)."""
    asset_returns: list[float] = []
    asset_ts: list[int] = []

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

    asset_returns = asset_returns[-window:]
    asset_ts = asset_ts[-window:]

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

    n = len(asset_returns)
    mean_a = sum(asset_returns) / n
    mean_b = sum(bench_returns) / n
    cov = (
        sum((a - mean_a) * (b - mean_b) for a, b in zip(asset_returns, bench_returns, strict=True))
        / n
    )
    var_b = sum((b - mean_b) ** 2 for b in bench_returns) / n
    if var_b <= 0:
        return None
    return round(cov / var_b, 6)


# --------------------------------------------------------------------------- #
# Price lookup helpers                                                         #
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


__all__ = ["DEFAULT_HORIZON_DAYS", "AbnormalReturnLabel", "AbnormalReturnLabelV1"]
