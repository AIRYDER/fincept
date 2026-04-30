"""
scripts/build_synth_parquet.py - bootstrap dataset for gbm_predictor.train.

Generates a synthetic OHLCV+features parquet with the exact 11 columns
the trainer expects (close + the 10 names from
``agents.gbm_predictor.features.FEATURES``).  The close path is a
geometric Brownian motion with mild momentum; features are computed
from that path so the structural relationships (e.g. ``ret_1m`` vs
``rv_5m``) are internally consistent.

The trained model that comes out of this dataset is **not** predictive
of real markets - random walks have no edge.  The point is:

  1. Produce a well-formed model.txt + meta.json so gbm_predictor goes
     UP and emits Predictions.
  2. Exercise the entire wire path: features -> agent -> Prediction ->
     orchestrator -> OMS -> portfolio.
  3. Provide a baseline AUC (~0.50) you can compare against once you
     retrain on real captured data.

Usage::

  uv run python scripts/build_synth_parquet.py \\
      --bars 43200 --out data/synth_bars.parquet

Defaults produce 30 days of 1-minute bars (60 * 24 * 30 = 43200).
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np
import polars as pl

# Match the trainer's feature schema exactly.  If FEATURES changes
# upstream, this script will fail loudly when the trainer runs.
from agents.gbm_predictor.features import FEATURES


def synth_close_path(
    n_bars: int,
    *,
    start_price: float = 50_000.0,
    annualized_vol: float = 0.60,
    bar_seconds: int = 60,
    drift_per_year: float = 0.05,
    seed: int = 42,
) -> np.ndarray:
    """Geometric Brownian motion with drift.

    Volatility of 0.60 is a reasonable BTC-like number.  Drift is
    intentionally low so labels are roughly balanced.
    """
    rng = np.random.default_rng(seed)
    dt = bar_seconds / (365 * 24 * 3600)
    sigma = annualized_vol
    mu = drift_per_year
    shocks = rng.normal(
        loc=(mu - 0.5 * sigma**2) * dt,
        scale=sigma * np.sqrt(dt),
        size=n_bars,
    )
    log_path = np.cumsum(shocks)
    return start_price * np.exp(log_path)


def compute_synthetic_features(close: np.ndarray) -> pl.DataFrame:
    """Compute the 10 FEATURES + close column as a polars frame.

    Returns/momentum features come straight from the price path.
    Realized-vol features are rolling stdev of log-returns.  Book
    features (book_imbalance_1, spread_bps) cannot be inferred from
    OHLC alone, so we synthesize plausible series:

      - book_imbalance_1: bounded in [-1, 1], lightly autocorrelated.
      - spread_bps: lognormal around a regime-dependent mean (wider
        when realized vol spikes).

    These won't be predictive but they exercise the same dtype and
    range that production features will hit.
    """
    n = len(close)
    log_close = np.log(close)

    def shifted_return(window: int) -> np.ndarray:
        """log(close_t / close_{t-window})."""
        if window >= n:
            return np.full(n, np.nan)
        out = np.full(n, np.nan)
        out[window:] = log_close[window:] - log_close[:-window]
        return out

    def rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
        """Trailing rolling std with NaN warmup."""
        s = pl.Series(arr)
        return s.rolling_std(window_size=window, min_samples=window).to_numpy()

    def zscore(arr: np.ndarray, window: int) -> np.ndarray:
        s = pl.Series(arr)
        mean = s.rolling_mean(window_size=window, min_samples=window)
        std = s.rolling_std(window_size=window, min_samples=window)
        return ((s - mean) / std).to_numpy()

    log_ret_1 = shifted_return(1)
    ret_1m = log_ret_1
    ret_5m = shifted_return(5)
    ret_15m = shifted_return(15)
    ret_60m = shifted_return(60)

    rv_5m = rolling_std(log_ret_1, 5) * np.sqrt(5)
    rv_30m = rolling_std(log_ret_1, 30) * np.sqrt(30)

    mom_z_30m = zscore(log_ret_1, 30)
    mom_z_240m = zscore(log_ret_1, 240)

    # --- book features (synthesized) ----------------------------------------
    rng = np.random.default_rng(20240101)
    # AR(1) imbalance with phi = 0.9, bounded by tanh.
    raw = rng.normal(0, 1, size=n)
    imb_state = np.zeros(n)
    phi = 0.9
    for i in range(1, n):
        imb_state[i] = phi * imb_state[i - 1] + raw[i] * np.sqrt(1 - phi**2)
    book_imbalance_1 = np.tanh(imb_state * 0.6)

    # Spread widens with realized vol; baseline 1-3 bps.
    rv_for_spread = np.where(np.isnan(rv_5m), 0.0, rv_5m) * 10_000  # bps-ish
    spread_bps = np.exp(0.6 + 0.4 * rng.normal(0, 1, size=n)) + 0.4 * rv_for_spread
    spread_bps = np.clip(spread_bps, 0.5, 200.0)

    columns = {
        "close": close,
        "ret_1m": ret_1m,
        "ret_5m": ret_5m,
        "ret_15m": ret_15m,
        "ret_60m": ret_60m,
        "rv_5m": rv_5m,
        "rv_30m": rv_30m,
        "mom_z_30m": mom_z_30m,
        "mom_z_240m": mom_z_240m,
        "book_imbalance_1": book_imbalance_1,
        "spread_bps": spread_bps,
    }

    # Sanity check: every required FEATURE name is present.
    missing = [f for f in FEATURES if f not in columns]
    if missing:
        raise RuntimeError(
            f"BUG: synthetic generator missing features {missing}; "
            f"update build_synth_parquet to match agents.gbm_predictor.features.FEATURES."
        )

    return pl.DataFrame(columns)


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_synth_parquet")
    parser.add_argument(
        "--bars",
        type=int,
        default=60 * 24 * 30,
        help="Number of 1-minute bars (default 43200 = 30 days).",
    )
    parser.add_argument(
        "--out",
        default="data/synth_bars.parquet",
        help="Output parquet path (created with parent dirs).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for the GBM close path.",
    )
    args = parser.parse_args()

    close = synth_close_path(args.bars, seed=args.seed)
    df = compute_synthetic_features(close)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)

    null_counts = {col: int(df[col].null_count()) for col in df.columns}
    print(f"Wrote {out}")
    print(f"  rows           : {df.height}")
    print(f"  columns        : {df.columns}")
    print(f"  null counts    : {null_counts}")
    print(f"  close range    : {close.min():.2f} - {close.max():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
