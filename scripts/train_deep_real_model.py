"""Deep real model training — rich features, 10 years, 20+ symbols, full pipeline.

This script leverages the FULL training system we built:
- Fetches 10 years of real OHLCV data for 20+ symbols + SPY + VIX via yfinance
- Computes 18 rich features (technical, cross-sectional, volatility regime, cross-asset)
- Builds a proper FeatureLakeManifest with PIT proof + purged k-fold + embargo
- Runs the full RunPod training handler with RealLightGBMTrainer
- Hyperparameter search space (num_leaves, learning_rate, max_depth, n_estimators)
- 5-fold walk-forward validation with purge gap
- HMAC-signed callback processed through the gateway
- Dossier registered with full metrics (PBO, deflated Sharpe, Brier, etc.)

Usage:
    uv run python scripts/train_deep_real_model.py
    uv run python scripts/train_deep_real_model.py --years 10 --symbols "AAPL,MSFT,..."
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import time
from collections.abc import Sequence
from typing import Any

# Bootstrap paths
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))
_SHARED = _REPO_ROOT / "runpod" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from datetime import UTC

from quant_foundry.feature_availability import FeatureAvailabilityReport  # noqa: E402
from quant_foundry.feature_lake import (  # noqa: E402
    FeatureLakeBuilder,
    FeatureRow,
    FeatureValue,
    UniverseEntry,
    export_receipt,
)
from quant_foundry.gateway import QuantFoundryGateway  # noqa: E402
from quant_foundry.real_trainer import RealLightGBMTrainer  # noqa: E402
from quant_foundry.runpod_training import RunPodTrainingHandler  # noqa: E402
from quant_foundry.schemas import RunPodTrainingRequest  # noqa: E402

NS_PER_DAY = 86_400_000_000_000

# Rich feature set — 18 features across 4 categories
FEATURE_NAMES: tuple[str, ...] = (
    # --- Price-based (5) ---
    "ret_1d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "mom_10d",
    # --- Volatility (4) ---
    "vol_20d",
    "vol_60d",
    "vol_ratio",  # today's volume / 20-day mean volume
    "vol_regime",  # vol_20d / vol_60d (short-term vs long-term vol)
    # --- Technical indicators (5) ---
    "rsi_14",  # Relative Strength Index (14-day)
    "bb_position",  # Bollinger Band position (0 = lower band, 1 = upper band)
    "price_vs_sma50",  # close / SMA(50) - 1
    "price_vs_sma200",  # close / SMA(200) - 1
    "atr_ratio",  # ATR(14) / close (normalized average true range)
    # --- Cross-asset (2) ---
    "spy_corr_20d",  # 20-day rolling correlation with SPY returns
    "spy_beta_60d",  # 60-day rolling beta vs SPY
    # --- VIX / regime (2) ---
    "vix_level",  # VIX level (volatility regime proxy)
    "vix_change_5d",  # VIX 5-day change
)


def fetch_yfinance_bars(
    symbols: list[str],
    years: int,
) -> dict[str, list[dict[str, float]]]:
    """Fetch real daily OHLCV bars from Yahoo Finance."""
    from datetime import datetime

    import yfinance as yf

    end = datetime.now(UTC)
    start = datetime(end.year - years, end.month, end.day, tzinfo=UTC)

    bars_by_symbol: dict[str, list[dict[str, float]]] = {}
    for sym in symbols:
        print(f"  Fetching {sym:6s} ({start.date()} to {end.date()})...", end=" ", flush=True)
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
            if df.empty:
                print("NO DATA")
                continue
            bars: list[dict[str, float]] = []
            for idx, row in df.iterrows():
                ts_ns = int(idx.tz_convert("UTC").value)
                bars.append(
                    {
                        "ts_event": ts_ns,
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    }
                )
            bars_by_symbol[sym] = bars
            print(f"{len(bars)} bars")
        except Exception as exc:
            print(f"ERROR: {exc}")

    return bars_by_symbol


def compute_rsi(close: list[float], period: int = 14) -> list[float]:
    """Compute RSI (Relative Strength Index)."""
    import numpy as np

    n = len(close)
    rsi = np.full(n, 50.0)
    if n < period + 1:
        return rsi.tolist()

    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return rsi.tolist()


def compute_atr(
    high: list[float], low: list[float], close: list[float], period: int = 14
) -> list[float]:
    """Compute Average True Range."""
    import numpy as np

    n = len(close)
    atr = np.zeros(n)
    if n < period + 1:
        return atr.tolist()

    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    atr[period] = np.mean(tr[1 : period + 1])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    return atr.tolist()


def compute_sma(values: list[float], period: int) -> list[float]:
    """Simple Moving Average."""
    import numpy as np

    n = len(values)
    sma = np.full(n, np.nan)
    if n < period:
        return sma.tolist()
    cumsum = np.cumsum(values)
    sma[period - 1 :] = (cumsum[period - 1 :] - np.concatenate([[0], cumsum[:-period]])) / period
    return sma.tolist()


def compute_rolling_corr(ret_a: list[float], ret_b: list[float], window: int) -> list[float]:
    """Rolling Pearson correlation. Returns 0.0 where not enough data."""
    import numpy as np

    n = len(ret_a)
    corr = np.zeros(n)
    if n < window:
        return corr.tolist()
    a = np.array(ret_a)
    b = np.array(ret_b)
    for i in range(window - 1, n):
        a_w = a[i - window + 1 : i + 1]
        b_w = b[i - window + 1 : i + 1]
        if np.std(a_w) > 0 and np.std(b_w) > 0:
            corr[i] = float(np.corrcoef(a_w, b_w)[0, 1])
    return corr.tolist()


def compute_rolling_beta(ret_a: list[float], ret_b: list[float], window: int) -> list[float]:
    """Rolling beta (regression coefficient). Returns 0.0 where not enough data."""
    import numpy as np

    n = len(ret_a)
    beta = np.zeros(n)
    if n < window:
        return beta.tolist()
    a = np.array(ret_a)
    b = np.array(ret_b)
    for i in range(window - 1, n):
        a_w = a[i - window + 1 : i + 1]
        b_w = b[i - window + 1 : i + 1]
        var_b = np.var(b_w)
        if var_b > 0:
            beta[i] = float(np.cov(a_w, b_w)[0, 1] / var_b)
    return beta.tolist()


def compute_rich_features(
    bars: list[dict[str, float]],
    spy_bars: list[dict[str, float]] | None,
    vix_bars: list[dict[str, float]] | None,
    label_horizon_days: int,
) -> list[dict[str, float]]:
    """Compute 18 rich features + forward-return label from a single symbol's bars.

    All features use only data at or before the decision time (PIT-correct).
    The label uses future data (it's the target, not a feature).
    """
    import numpy as np

    n = len(bars)
    if n < 205 + label_horizon_days:  # need 200 for SMA200 + warmup
        return []

    close = np.array([b["close"] for b in bars], dtype=np.float64)
    high = np.array([b["high"] for b in bars], dtype=np.float64)
    low = np.array([b["low"] for b in bars], dtype=np.float64)
    volume = np.array([b["volume"] for b in bars], dtype=np.float64)
    ts = np.array([b["ts_event"] for b in bars], dtype=np.int64)

    log_close = np.log(close)
    log_ret = np.zeros(n, dtype=np.float64)
    log_ret[1:] = np.diff(log_close)

    # --- Price-based features ---
    ret_1d = log_ret.copy()
    ret_5d = np.zeros(n)
    ret_10d = np.zeros(n)
    ret_20d = np.zeros(n)
    mom_10d = np.zeros(n)
    for i in range(n):
        if i >= 5:
            ret_5d[i] = log_close[i] - log_close[i - 5]
        if i >= 10:
            ret_10d[i] = log_close[i] - log_close[i - 10]
            mom_10d[i] = close[i] / close[i - 10] - 1.0
        if i >= 20:
            ret_20d[i] = log_close[i] - log_close[i - 20]

    # --- Volatility features ---
    vol_20d = np.zeros(n)
    vol_60d = np.zeros(n)
    for i in range(n):
        if i >= 19:
            vol_20d[i] = float(np.std(log_ret[i - 19 : i + 1], ddof=0))
        if i >= 59:
            vol_60d[i] = float(np.std(log_ret[i - 59 : i + 1], ddof=0))

    vol_mean_20 = np.zeros(n)
    for i in range(n):
        if i >= 19:
            vol_mean_20[i] = float(np.mean(volume[i - 19 : i + 1]))
    vol_ratio = np.where(vol_mean_20 > 0, volume / np.where(vol_mean_20 > 0, vol_mean_20, 1.0), 1.0)
    vol_regime = np.where(vol_60d > 0, vol_20d / np.where(vol_60d > 0, vol_60d, 1.0), 1.0)

    # --- Technical indicators ---
    rsi = compute_rsi(close.tolist(), 14)

    # Bollinger Bands (20-day, 2 std)
    sma20 = np.array(compute_sma(close.tolist(), 20))
    std20 = np.zeros(n)
    for i in range(19, n):
        std20[i] = float(np.std(close[i - 19 : i + 1], ddof=0))
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    bb_width = upper - lower
    bb_position = np.where(
        bb_width > 0, (close - lower) / np.where(bb_width > 0, bb_width, 1.0), 0.5
    )

    sma50 = np.array(compute_sma(close.tolist(), 50))
    sma200 = np.array(compute_sma(close.tolist(), 200))
    price_vs_sma50 = np.where(np.isfinite(sma50) & (sma50 > 0), close / sma50 - 1.0, 0.0)
    price_vs_sma200 = np.where(np.isfinite(sma200) & (sma200 > 0), close / sma200 - 1.0, 0.0)

    atr = compute_atr(high.tolist(), low.tolist(), close.tolist(), 14)
    atr_ratio = np.array(atr) / np.where(close > 0, close, 1.0)

    # --- Cross-asset features (SPY correlation + beta) ---
    spy_corr = np.zeros(n)
    spy_beta = np.zeros(n)
    if spy_bars is not None and len(spy_bars) > 60:
        # Align SPY returns by timestamp
        spy_ts = {b["ts_event"]: b["close"] for b in spy_bars}
        spy_close_aligned = np.zeros(n)
        for i in range(n):
            spy_close_aligned[i] = spy_ts.get(ts[i], 0.0)
        spy_log = np.log(np.where(spy_close_aligned > 0, spy_close_aligned, 1.0))
        spy_ret = np.zeros(n)
        spy_ret[1:] = np.diff(spy_log)
        spy_corr = np.array(compute_rolling_corr(log_ret.tolist(), spy_ret.tolist(), 20))
        spy_beta = np.array(compute_rolling_beta(log_ret.tolist(), spy_ret.tolist(), 60))

    # --- VIX features ---
    vix_level = np.zeros(n)
    vix_change_5d = np.zeros(n)
    if vix_bars is not None and len(vix_bars) > 5:
        vix_ts = {b["ts_event"]: b["close"] for b in vix_bars}
        vix_aligned = np.zeros(n)
        for i in range(n):
            vix_aligned[i] = vix_ts.get(ts[i], 0.0)
        vix_level = vix_aligned
        for i in range(5, n):
            if vix_aligned[i] > 0 and vix_aligned[i - 5] > 0:
                vix_change_5d[i] = vix_aligned[i] - vix_aligned[i - 5]

    # --- Build rows ---
    rows: list[dict[str, float]] = []
    for i in range(n):
        # Warmup: need 200 days for SMA200
        if i < 200:
            continue
        # Label: need forward data
        if i + label_horizon_days >= n:
            break

        fwd_ret = float(log_close[i + label_horizon_days] - log_close[i])
        label = 1.0 if fwd_ret > 0.0 else 0.0

        row = {
            "decision_time": int(ts[i]),
            "ret_1d": float(ret_1d[i]),
            "ret_5d": float(ret_5d[i]),
            "ret_10d": float(ret_10d[i]),
            "ret_20d": float(ret_20d[i]),
            "mom_10d": float(mom_10d[i]),
            "vol_20d": float(vol_20d[i]),
            "vol_60d": float(vol_60d[i]),
            "vol_ratio": float(vol_ratio[i]),
            "vol_regime": float(vol_regime[i]),
            "rsi_14": float(rsi[i]),
            "bb_position": float(bb_position[i]),
            "price_vs_sma50": float(price_vs_sma50[i]),
            "price_vs_sma200": float(price_vs_sma200[i]),
            "atr_ratio": float(atr_ratio[i]),
            "spy_corr_20d": float(spy_corr[i]),
            "spy_beta_60d": float(spy_beta[i]),
            "vix_level": float(vix_level[i]),
            "vix_change_5d": float(vix_change_5d[i]),
            "label": label,
        }
        rows.append(row)

    return rows


def feature_schema_hash() -> str:
    import hashlib

    return hashlib.sha256(":".join(FEATURE_NAMES).encode()).hexdigest()


def label_schema_hash(horizon_days: int) -> str:
    import hashlib

    return hashlib.sha256(f"binary:forward_return:{horizon_days}d".encode()).hexdigest()


def build_manifest(
    bars_by_symbol: dict[str, list[dict[str, float]]],
    spy_bars: list[dict[str, float]] | None,
    vix_bars: list[dict[str, float]] | None,
    *,
    label_horizon_days: int,
    n_folds: int,
    dataset_id: str,
    source_refs: list[str],
) -> tuple[Any, FeatureAvailabilityReport, list[dict]]:
    """Build FeatureLakeManifest from rich features."""

    symbols = sorted(bars_by_symbol.keys())
    universe = tuple(UniverseEntry(symbol=s, listed_until=None, renamed_from=None) for s in symbols)

    all_data_rows: list[dict[str, float]] = []
    for sym in symbols:
        sym_rows = compute_rich_features(
            bars_by_symbol[sym], spy_bars, vix_bars, label_horizon_days
        )
        for r in sym_rows:
            r["__symbol"] = sym
        all_data_rows.extend(sym_rows)
        print(f"    {sym}: {len(sym_rows)} feature rows")

    # Build FeatureRow objects
    horizon_ns = label_horizon_days * NS_PER_DAY
    feature_rows: list[FeatureRow] = []
    for r in all_data_rows:
        dt = int(r["decision_time"])
        features = tuple(
            FeatureValue(name=name, value=float(r[name]), observed_at=dt) for name in FEATURE_NAMES
        )
        feature_rows.append(
            FeatureRow(
                symbol=r["__symbol"],
                event_ts=dt,
                decision_time=dt,
                features=features,
                label_horizon_ns=horizon_ns,
            )
        )

    f_hash = feature_schema_hash()
    l_hash = label_schema_hash(label_horizon_days)

    builder = FeatureLakeBuilder(
        dataset_id=dataset_id,
        universe=universe,
        rows=tuple(feature_rows),
        feature_schema_hash=f_hash,
        label_schema_hash=l_hash,
        max_label_horizon_ns=horizon_ns,
        n_folds=n_folds,
        source_vintage_refs=source_refs,
    )
    manifest = builder.build_manifest()
    availability = FeatureAvailabilityReport.from_rows(tuple(feature_rows), FEATURE_NAMES)

    return manifest, availability, all_data_rows


def write_parquet(data_rows: list[dict], out_path: pathlib.Path) -> int:
    import polars as pl

    if not data_rows:
        schema = {
            "decision_time": pl.Int64,
            **{name: pl.Float64 for name in FEATURE_NAMES},
            "label": pl.Float64,
        }
        pl.DataFrame(schema=schema).write_parquet(str(out_path))
        return 0

    columns: dict[str, list] = {"decision_time": [int(r["decision_time"]) for r in data_rows]}
    for name in FEATURE_NAMES:
        columns[name] = [float(r[name]) for r in data_rows]
    columns["label"] = [float(r["label"]) for r in data_rows]

    df = pl.DataFrame(columns).sort("decision_time")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(str(out_path))
    return df.height


def write_manifest_json(manifest, availability, out_path: pathlib.Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.loads(manifest.to_json())
    body["availability"] = json.loads(availability.to_json())
    body["feature_names"] = list(FEATURE_NAMES)
    out_path.write_text(json.dumps(body, sort_keys=True, indent=2))


# Default universe — 20 liquid large-caps across sectors
DEFAULT_SYMBOLS = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",  # tech
    "META",
    "TSLA",
    "JPM",
    "V",
    "JNJ",  # mixed
    "WMT",
    "PG",
    "UNH",
    "HD",
    "MA",  # consumer/health
    "DIS",
    "BAC",
    "XOM",
    "KO",
    "PEP",  # diversified
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="train_deep_real_model",
        description="Deep real model training with rich features on real market data.",
    )
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--label-horizon-days", type=int, default=5)
    parser.add_argument("--n-folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    # --- 1. Fetch data ---
    print("=" * 70)
    print("STEP 1: FETCH REAL MARKET DATA (10 years, 20+ symbols + SPY + VIX)")
    print("=" * 70)

    # Always fetch SPY and VIX for cross-asset features
    fetch_symbols = symbols + ["SPY", "^VIX"]
    bars_by_symbol = fetch_yfinance_bars(fetch_symbols, args.years)
    if not bars_by_symbol:
        print("ERROR: no data fetched")
        return 1

    spy_bars = bars_by_symbol.pop("SPY", None)
    vix_bars = bars_by_symbol.pop("^VIX", None)

    total_bars = sum(len(b) for b in bars_by_symbol.values())
    print(f"\n  Total bars: {total_bars}")
    print(f"  Symbols:    {len(bars_by_symbol)}")
    print(f"  SPY bars:   {len(spy_bars) if spy_bars else 0}")
    print(f"  VIX bars:   {len(vix_bars) if vix_bars else 0}")

    # --- 2. Build rich feature dataset ---
    print(f"\n{'=' * 70}")
    print(f"STEP 2: COMPUTE {len(FEATURE_NAMES)} RICH FEATURES + BUILD MANIFEST")
    print("=" * 70)
    print(f"  Features: {list(FEATURE_NAMES)}")
    print()

    dataset_id = f"deep_real_{'_'.join(sorted(bars_by_symbol.keys())[:5])}_y{args.years}_h{args.label_horizon_days}d"
    dataset_dir = _REPO_ROOT / "data" / "datasets" / "deep_real"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    source_refs = [
        "vendor:yfinance",
        f"symbols:{','.join(sorted(bars_by_symbol.keys()))}",
        f"years:{args.years}",
        f"features:{len(FEATURE_NAMES)}",
        f"label_horizon:{args.label_horizon_days}d",
        f"folds:{args.n_folds}",
        f"seed:{args.seed}",
    ]

    manifest, availability, data_rows = build_manifest(
        bars_by_symbol,
        spy_bars,
        vix_bars,
        label_horizon_days=args.label_horizon_days,
        n_folds=args.n_folds,
        dataset_id=dataset_id,
        source_refs=source_refs,
    )

    if not data_rows:
        print("ERROR: no usable rows. Increase --years or use different symbols.")
        return 1

    parquet_path = dataset_dir / f"{dataset_id}.parquet"
    manifest_path = dataset_dir / f"{dataset_id}.manifest.json"

    n_written = write_parquet(data_rows, parquet_path)
    write_manifest_json(manifest, availability, manifest_path)
    export_receipt(manifest, availability, dataset_dir)

    m_hash = manifest.manifest_hash()
    print(f"\n  dataset_id:          {manifest.dataset_id}")
    print(f"  manifest_hash:       {m_hash}")
    print(f"  row_count:           {manifest.row_count}")
    print(f"  parquet rows:        {n_written}")
    print(f"  features:            {len(FEATURE_NAMES)}")
    print(f"  pit_proof_verified:  {manifest.pit_proof_verified}")
    print(f"  folds:               {len(manifest.folds.folds)}")

    labels = [r["label"] for r in data_rows]
    n_up = sum(1 for l in labels if l == 1.0)
    print(
        f"  label balance:       {n_up} up / {len(labels) - n_up} down ({n_up / len(labels) * 100:.1f}% up)"
    )

    # --- 3. Set up gateway ---
    print(f"\n{'=' * 70}")
    print("STEP 3: SET UP GATEWAY + ENQUEUE TRAINING JOB")
    print("=" * 70)

    callback_secret = "deep-real-training-secret"
    base_dir = _REPO_ROOT / "data" / "deep_real_training"
    status_dir = base_dir / "worker_status"

    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    status_dir.mkdir(parents=True, exist_ok=True)

    os.environ["QUANT_FOUNDRY_ENABLED"] = "true"
    os.environ["QUANT_FOUNDRY_MODE"] = "local_mock"
    os.environ["QUANT_FOUNDRY_SHADOW_ONLY"] = "true"
    os.environ["QUANT_FOUNDRY_CALLBACK_SECRET"] = callback_secret
    os.environ["QUANT_FOUNDRY_BASE_DIR"] = str(base_dir)
    os.environ["QUANT_FOUNDRY_WORKER_STATUS_DIR"] = str(status_dir)
    os.environ["QUANT_FOUNDRY_USE_REAL_TRAINER"] = "true"

    gateway = QuantFoundryGateway.from_env(base_dir=base_dir)

    dataset_ref = f"file://{parquet_path.as_posix()}"
    job_id = f"deep-real-train-{int(time.time())}"
    idempotency_key = f"idemp-{job_id}"

    # Hyperparameter search space — let the trainer explore
    search_space = {
        "num_leaves": [31, 63, 127],
        "learning_rate": [0.01, 0.05, 0.1],
        "max_depth": [4, 6, 8],
        "n_estimators": [100, 200, 300],
        "min_data_in_leaf": [5, 10, 20],
    }

    request_payload = {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": dataset_ref,
        "model_family": "lightgbm",
        "random_seed": args.seed,
        "search_space": search_space,
        "extra_constraints": {
            "bar_seconds": "86400",
            "horizon_bars": str(args.label_horizon_days),
            "purge_bars": str(args.label_horizon_days),
        },
    }
    req = RunPodTrainingRequest.model_validate(request_payload)

    print(f"  job_id:       {req.job_id}")
    print("  model:        LightGBM")
    print(f"  search_space: {search_space}")
    print(f"  folds:        {args.n_folds}")
    print(f"  dataset:      {n_written} rows, {len(FEATURE_NAMES)} features")

    gateway.outbox.enqueue(
        job_id=job_id,
        job_type="training",
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        priority=0,
        budget_cents=0,
    )
    print(f"  [outbox] enqueued: {job_id}")

    # --- 4. Run training ---
    print(f"\n{'=' * 70}")
    print("STEP 4: RUN DEEP LIGHTGBM TRAINING (RunPodTrainingHandler)")
    print("=" * 70)

    try:
        from worker_status import write_status

        write_status(job_id, "started")
    except ImportError:
        pass

    trainer = RealLightGBMTrainer(n_folds=args.n_folds, annualization_factor=252)
    handler = RunPodTrainingHandler(
        callback_secret=callback_secret,
        trainer=trainer,
        deadline_seconds=600,
        worker_id="deep-real-worker-1",
    )

    print(f"  Training LightGBM with {args.n_folds}-fold walk-forward + purge gap...")
    print(f"  {n_written} rows x {len(FEATURE_NAMES)} features")
    print()

    start_ns = time.time_ns()
    result = handler.handle(req)
    elapsed_s = (time.time_ns() - start_ns) / 1_000_000_000

    try:
        from worker_status import write_status

        write_status(job_id, "completed", artifact_id=result.artifact_id)
    except ImportError:
        pass

    print(f"  Training completed in {elapsed_s:.1f}s")

    # --- 5. Parse + display results ---
    envelope = json.loads(result.callback_payload)
    dossier_data = envelope["payload"]["dossier"]
    artifact_data = envelope["payload"]["artifact_manifest"]
    metrics = dossier_data["training_metrics"]
    meta = dossier_data["metadata"]

    print(f"\n{'=' * 70}")
    print("STEP 5: DEEP TRAINING RESULTS")
    print("=" * 70)

    print("\n  Artifact:")
    print(f"    artifact_id:       {artifact_data['artifact_id']}")
    print(f"    sha256:            {artifact_data['sha256'][:16]}...")
    print(f"    size_bytes:        {artifact_data['size_bytes']:,}")
    print(f"    feature_schema:    {artifact_data['feature_schema_hash'][:16]}...")
    print(f"    label_schema:      {artifact_data['label_schema_hash'][:16]}...")

    print("\n  Dossier:")
    print(f"    model_id:          {dossier_data['model_id']}")
    print(f"    authority:         {dossier_data['authority']}")

    print(f"\n  Walk-Forward Metrics ({args.n_folds} folds, out-of-sample):")
    print(f"    accuracy:          {metrics.get('accuracy', 0):.6f}")
    print(f"    logloss:           {metrics.get('logloss', 0):.6f}")
    print(f"    brier_score:       {meta.get('brier_score', 'n/a')}")
    print(f"    win_rate:          {meta.get('win_rate', 'n/a')}")
    print(f"    sharpe_ratio:      {meta.get('sharpe_ratio', 'n/a')}")
    print(f"    max_drawdown:      {meta.get('max_drawdown', 'n/a')}")
    print(f"    pbo:               {dossier_data['pbo']}")
    print(f"    deflated_sharpe:   {dossier_data['deflated_sharpe']}")

    # Interpretation
    print("\n  Interpretation:")
    acc = metrics.get("accuracy", 0.5)
    pbo = dossier_data["pbo"]
    dsr = dossier_data["deflated_sharpe"]
    sharpe = float(meta.get("sharpe_ratio", 0))

    if acc > 0.56:
        print(f"    accuracy {acc:.3f} — meaningful predictive signal")
    elif acc > 0.53:
        print(f"    accuracy {acc:.3f} — moderate signal, worth investigating")
    elif acc > 0.51:
        print(f"    accuracy {acc:.3f} — weak signal")
    else:
        print(f"    accuracy {acc:.3f} — near chance")

    if pbo < 0.3:
        print(f"    PBO {pbo:.3f} — low overfitting risk, results are robust")
    elif pbo < 0.5:
        print(f"    PBO {pbo:.3f} — moderate overfitting risk")
    elif pbo < 0.8:
        print(f"    PBO {pbo:.3f} — high overfitting risk")
    else:
        print(f"    PBO {pbo:.3f} — very high overfitting risk")

    if dsr > 1.0:
        print(f"    deflated Sharpe {dsr:.3f} — strong residual edge after overfit penalty")
    elif dsr > 0.5:
        print(f"    deflated Sharpe {dsr:.3f} — meaningful residual edge")
    elif dsr > 0:
        print(f"    deflated Sharpe {dsr:.3f} — small residual edge")
    else:
        print(f"    deflated Sharpe {dsr:.3f} — no residual edge after overfit penalty")

    if sharpe > 1.0:
        print(f"    raw Sharpe {sharpe:.3f} — positive risk-adjusted return (in-sample)")
    elif sharpe > 0:
        print(f"    raw Sharpe {sharpe:.3f} — marginally positive (in-sample)")
    else:
        print(f"    raw Sharpe {sharpe:.3f} — negative (in-sample)")

    # --- 6. Process callback ---
    print(f"\n{'=' * 70}")
    print("STEP 6: PROCESS CALLBACK + VERIFY")
    print("=" * 70)

    receipt = gateway.receive_callback(
        job_id=job_id,
        payload=result.callback_payload,
        signature=result.callback_signature,
        ts=result.callback_ts,
        worker_id="deep-real-worker-1",
    )

    final_rec = gateway.outbox.get(job_id)
    print(f"  callback ok:        {receipt.get('ok')}")
    print(f"  outbox status:      {final_rec.status.value}")

    try:
        registry = gateway.dossier_registry()
        dossiers = registry.list()
        print(f"  dossier registry:   {len(dossiers)} dossier(s)")
        if dossiers:
            d = dossiers[-1]
            print(f"    model_id: {d.model_id}")
            print(f"    status:   {d.status.value}")
    except Exception as exc:
        print(f"  dossier registry:   {exc}")

    # --- 7. Save ---
    results_dir = base_dir / "results"
    results_dir.mkdir(exist_ok=True)
    for name, data in [
        ("callback_envelope.json", envelope),
        ("artifact_manifest.json", artifact_data),
        ("dossier.json", dossier_data),
    ]:
        (results_dir / name).write_text(json.dumps(data, indent=2), encoding="utf-8")

    print(f"\n  Results saved: {results_dir}")

    print(f"\n{'=' * 70}")
    print(f"DEEP REAL MODEL TRAINING COMPLETE ({elapsed_s:.1f}s)")
    print(f"{'=' * 70}")
    print(
        f"  Data:       {n_written} rows, {len(FEATURE_NAMES)} features, {len(bars_by_symbol)} symbols"
    )
    print(f"  Folds:      {args.n_folds} walk-forward + purge gap")
    print(f"  Artifact:   {result.artifact_id}")
    print(f"  Dossier:    {result.dossier_id}")
    print(f"  Outbox:     {final_rec.status.value}")
    print(f"  Authority:  {dossier_data['authority']}")
    print(f"  Accuracy:   {metrics.get('accuracy', 0):.4f}")
    print(f"  Sharpe:     {sharpe:.4f}")
    print(f"  PBO:        {dossier_data['pbo']}")
    print(f"  Deflated:   {dossier_data['deflated_sharpe']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
