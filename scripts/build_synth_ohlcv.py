"""
scripts/build_synth_ohlcv.py - synthetic OHLCV bars for backtester runs.

Generates a parquet in the schema the backtester runner expects:
``symbol, ts_event, open, high, low, close, volume`` (+ optional
``vwap, trades``).  Reuses :func:`scripts.build_synth_parquet.synth_close_path`
for the close path, then derives high/low/open by adding plausible
intra-bar noise so the broker's LIMIT-fill logic has a meaningful range
to evaluate against (instead of degenerate open=high=low=close bars).

Usage::

  uv run python scripts/build_synth_ohlcv.py \\
      --bars 5000 --symbols BTC-USD,ETH-USD --out data/synth_ohlcv.parquet

Default produces ~3.5 days of 1m bars per symbol with a realistic
intra-bar high/low spread of a few basis points so LIMIT orders can
trigger.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

import numpy as np
import polars as pl


# Local copy of synth_close_path - kept in sync with
# scripts/build_synth_parquet.py.  The two scripts use the same RNG so
# a parquet with seed=42 produces the same close path either way.
def synth_close_path(
    n_bars: int,
    *,
    start_price: float = 50_000.0,
    annualized_vol: float = 0.60,
    bar_seconds: int = 60,
    drift_per_year: float = 0.05,
    seed: int = 42,
) -> np.ndarray:
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


def _bar_seconds_for_freq(freq: str) -> int:
    return {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "1d": 86400,
    }.get(freq, 60)


def synth_ohlcv_for_symbol(
    symbol: str,
    *,
    n_bars: int,
    start_ts_ns: int,
    freq: str,
    seed: int,
    intra_bar_bps: float = 8.0,
    base_volume: float = 100.0,
) -> pl.DataFrame:
    """Generate one symbol's bars as a polars frame.

    ``intra_bar_bps`` controls the average peak-to-trough range within
    a single bar, expressed in basis points of the close.  8 bps is a
    typical 1m crypto intrabar range (rough rule of thumb; tunable).
    """
    bar_seconds = _bar_seconds_for_freq(freq)
    close = synth_close_path(
        n_bars,
        seed=seed,
        bar_seconds=bar_seconds,
    )
    rng = np.random.default_rng(seed + 1)
    # Open of bar t is approximately close_{t-1} with a tiny gap.  At
    # t=0 we just use close[0] - a synthetic warmup edge case.
    prev_close = np.empty_like(close)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    # Intrabar range: log-normal centered on intra_bar_bps fraction of
    # close; ensures high >= max(open, close) and low <= min(open, close).
    half_range = (intra_bar_bps / 10_000.0) * close * np.exp(rng.normal(0, 0.2, size=n_bars))
    open_ = prev_close + (close - prev_close) * rng.uniform(0, 1, size=n_bars)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, half_range))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, half_range))
    # Volume: log-normal around base_volume.  Real markets show power-
    # law tails; this is a coarse approximation but good enough for a
    # backtester smoke test.
    volume = rng.lognormal(mean=np.log(base_volume), sigma=0.6, size=n_bars)

    ts_step_ns = bar_seconds * 1_000_000_000
    ts_events = start_ts_ns + np.arange(n_bars, dtype=np.int64) * ts_step_ns

    return pl.DataFrame(
        {
            "symbol": [symbol] * n_bars,
            "ts_event": ts_events,
            "open": open_.astype(np.float64),
            "high": high.astype(np.float64),
            "low": low.astype(np.float64),
            "close": close.astype(np.float64),
            "volume": volume.astype(np.float64),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_synth_ohlcv")
    parser.add_argument(
        "--bars",
        type=int,
        default=5000,
        help="Number of bars per symbol (default 5000).",
    )
    parser.add_argument(
        "--symbols",
        default="BTC-USD,ETH-USD,SOL-USD",
        help="Comma-separated symbols to include.",
    )
    parser.add_argument(
        "--freq",
        default="1m",
        help="Bar frequency: 1m | 5m | 15m | 1h | 1d.",
    )
    parser.add_argument(
        "--out",
        default="data/synth_ohlcv.parquet",
        help="Output parquet path.",
    )
    parser.add_argument(
        "--start-ts-unix",
        type=int,
        default=None,
        help="Start ts (unix seconds). Default: now() - bars * bar_seconds.",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    bar_seconds = _bar_seconds_for_freq(args.freq)
    if args.start_ts_unix is None:
        start_ts_ns = (int(time.time()) - args.bars * bar_seconds) * 1_000_000_000
    else:
        start_ts_ns = args.start_ts_unix * 1_000_000_000

    frames: list[pl.DataFrame] = []
    for i, sym in enumerate(symbols):
        # Vary seed per symbol so they aren't the same path; same series of
        # ts_events so multi-symbol replay merges deterministically.
        df = synth_ohlcv_for_symbol(
            sym,
            n_bars=args.bars,
            start_ts_ns=start_ts_ns,
            freq=args.freq,
            seed=args.seed + i * 17,
        )
        frames.append(df)

    combined = pl.concat(frames).sort(["ts_event", "symbol"])
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    combined.write_parquet(out)

    print(f"Wrote {out}")
    print(f"  rows     : {combined.height}")
    print(f"  symbols  : {symbols}")
    print(f"  freq     : {args.freq}")
    ts_min = combined["ts_event"].min()
    ts_max = combined["ts_event"].max()
    print(f"  ts span  : {ts_min!r} to {ts_max!r}")
    print(f"  cols     : {combined.columns}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
