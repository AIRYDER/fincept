"""
scripts/build_dataset_manifest.py — build a real, point-in-time dataset
manifest from OHLCV bar data for the RealLightGBMTrainer.

This script is the bridge between raw market data (parquet OHLCV produced by
``scripts/ingest_bars.py`` or the ``fincept_db.bars`` table) and the
leakage-safe ``FeatureLakeManifest`` that ``RealLightGBMTrainer`` references
via ``RunPodTrainingRequest.dataset_manifest_ref``.

What it does:
  1. Loads daily OHLCV bars (parquet or fincept_db).
  2. Computes simple, point-in-time-correct features from the bars:
       - ret_1d   — 1-day log return
       - ret_5d   — 5-day log return
       - vol_20d  — 20-day rolling realised volatility
       - mom_10d  — 10-day momentum (close / close[-10] - 1)
       - vol_ratio — today's volume / 20-day mean volume
  3. Creates binary labels from forward ``--label-horizon-days`` return
     direction (1 if forward return > 0 else 0).  The label uses future
     data but is the *target*, not a feature — PIT correctness is enforced
     only on features (``observed_at <= decision_time``).
  4. Builds ``FeatureRow`` objects with ``observed_at == decision_time``
     (the bar close is the vendor-availability time).
  5. Uses ``FeatureLakeBuilder`` to produce a ``FeatureLakeManifest`` with
     purged-k-fold + embargo boundaries.
  6. Exports the dataset as a parquet file (columns: ``decision_time``,
     feature columns, ``label``) that ``RealLightGBMTrainer._load_parquet``
     can read directly.
  7. Writes the manifest JSON alongside the parquet.
  8. Prints the manifest hash, row count, and feature schema.

The parquet column layout matches what ``RealLightGBMTrainer`` expects:
  - ``decision_time``  — nanoseconds since epoch (ts column)
  - feature columns    — float64
  - ``label``          — float64 (0.0 / 1.0)

Usage::

  uv run python scripts/build_dataset_manifest.py \\
      --bars-dir data/bars/ --manifest-dir data/datasets/ \\
      --symbols AAPL,MSFT,GOOG,AMZN,NVDA \\
      --start-date 2022-01-01 --end-date 2024-12-31 \\
      --label-horizon-days 5 --n-folds 3

Heavy dependencies (numpy, polars) are imported lazily so this module is
importable without them — the test suite imports it to verify the builder
logic without requiring ML/data deps at import time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

# scripts/ are not packaged; prepend the quant_foundry src dir so we can
# import the feature-lake builder without installing the package.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if _QF_SRC.exists() and str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

from quant_foundry.feature_availability import FeatureAvailabilityReport
from quant_foundry.feature_lake import (
    FeatureLakeBuilder,
    FeatureRow,
    FeatureValue,
    UniverseEntry,
    export_receipt,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NS_PER_DAY = 86_400_000_000_000

#: Ordered tuple of feature names produced by this builder.  The order is
#: stable so the parquet column layout is deterministic.
FEATURE_NAMES: tuple[str, ...] = (
    "ret_1d",
    "ret_5d",
    "vol_20d",
    "mom_10d",
    "vol_ratio",
)


# ---------------------------------------------------------------------------
# Schema hashes (deterministic over feature/label names)
# ---------------------------------------------------------------------------


def feature_schema_hash() -> str:
    """SHA-256 over the sorted, colon-joined feature names."""
    payload = ":".join(sorted(FEATURE_NAMES))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def label_schema_hash(horizon_days: int, label_method: str = "binary_forward_return") -> str:
    """SHA-256 over the label description.

    The hash includes the label method so that datasets with different
    labeling schemes (simple forward return vs triple-barrier) get
    different hashes — preventing accidental mixing.
    """
    if label_method == "triple_barrier":
        payload = f"triple_barrier_{horizon_days}d"
    else:
        payload = f"binary_forward_return_direction_{horizon_days}d"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Date / timestamp helpers
# ---------------------------------------------------------------------------


def _date_to_ns(date_str: str) -> int:
    """Parse a YYYY-MM-DD date string to nanoseconds since epoch (UTC midnight)."""
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1_000_000_000


def _ns_to_date_str(ns: int) -> str:
    """Format nanoseconds since epoch as a YYYY-MM-DD string (UTC)."""
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC).strftime(
        "%Y-%m-%d",
    )


# ---------------------------------------------------------------------------
# Bar loading
# ---------------------------------------------------------------------------


def load_bars_from_parquet(
    bars_dir: pathlib.Path,
    symbols: Sequence[str],
    start_ns: int,
    end_ns: int,
) -> dict[str, list[dict[str, float]]]:
    """Load OHLCV bars from parquet files in *bars_dir*.

    Expects files produced by ``scripts/ingest_bars.py`` (schema: symbol,
    ts_event, open, high, low, close, volume, trades, vwap).  Files may be
    either a single multi-symbol parquet or one parquet per symbol named
    ``<symbol>.parquet``.

    Returns ``{symbol: [{ts_event, open, high, low, close, volume}, ...]}``
    sorted by ``ts_event``, filtered to ``[start_ns, end_ns)``.
    """
    import polars as pl

    bars_dir = pathlib.Path(bars_dir)
    frames: list[pl.DataFrame] = []

    # Collect candidate parquet files.
    candidates: list[pathlib.Path] = []
    if bars_dir.is_dir():
        # Per-symbol files first.
        for sym in symbols:
            p = bars_dir / f"{sym}.parquet"
            if p.exists():
                candidates.append(p)
        # Then any other .parquet in the dir (multi-symbol).
        for p in sorted(bars_dir.glob("*.parquet")):
            if p not in candidates:
                candidates.append(p)

    for path in candidates:
        df = pl.read_parquet(str(path))
        # Normalise: only keep the columns we need.
        keep = [
            c
            for c in ("symbol", "ts_event", "open", "high", "low", "close", "volume")
            if c in df.columns
        ]
        df = df.select(keep)
        frames.append(df)

    if not frames:
        return {}

    combined = pl.concat(frames, how="vertical_relaxed")
    # Filter by time window.
    combined = combined.filter(
        (pl.col("ts_event") >= start_ns) & (pl.col("ts_event") < end_ns),
    )
    # Filter by requested symbols.
    combined = combined.filter(pl.col("symbol").is_in(list(symbols)))

    out: dict[str, list[dict[str, float]]] = {}
    for sym in symbols:
        sub = combined.filter(pl.col("symbol") == sym).sort("ts_event")
        if sub.height == 0:
            continue
        rows = []
        for row in sub.iter_rows(named=True):
            rows.append(
                {
                    "ts_event": int(row["ts_event"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                },
            )
        out[sym] = rows
    return out


async def load_bars_from_db(
    symbols: Sequence[str],
    start_ns: int,
    end_ns: int,
    freq: str = "1d",
) -> dict[str, list[dict[str, float]]]:
    """Load bars from the fincept_db database (async).

    Falls back to this when parquet files are not available.  Requires
    ``fincept_db`` to be installed and a database configured.
    """
    from fincept_db.bars import read_bars

    out: dict[str, list[dict[str, float]]] = {}
    for sym in symbols:
        events = await read_bars(sym, freq, start_ns, end_ns)
        if not events:
            continue
        out[sym] = [
            {
                "ts_event": int(e.ts_event),
                "open": float(e.open),
                "high": float(e.high),
                "low": float(e.low),
                "close": float(e.close),
                "volume": float(e.volume),
            }
            for e in events
        ]
    return out


# ---------------------------------------------------------------------------
# Feature + label computation
# ---------------------------------------------------------------------------


def compute_features_and_labels(
    bars: list[dict[str, float]],
    label_horizon_days: int,
    *,
    label_method: str = "binary_forward_return",
    profit_take_width: float = 0.02,
    stop_loss_width: float = 0.02,
    vol_scale_widths: bool = False,
) -> list[dict[str, Any]]:
    """Compute PIT features + labels from a single symbol's bars.

    Args:
        bars: OHLCV bar dicts (must have ``high``, ``low``, ``close``,
            ``volume``, ``ts_event``).
        label_horizon_days: forward horizon for the label (also the
            vertical barrier timeout for triple-barrier).
        label_method: ``"binary_forward_return"`` (default, legacy) or
            ``"triple_barrier"`` (Tier 2.3, AFML Ch. 3).
        profit_take_width: upper barrier width as fraction of entry
            price (triple-barrier only).
        stop_loss_width: lower barrier width as fraction of entry
            price (triple-barrier only).
        vol_scale_widths: if True, scale barrier widths by rolling
            volatility (triple-barrier only).

    Returns a list of row dicts, each with:
      - ``decision_time`` (ns)  — the bar close (== observed_at for all feats)
      - ``ret_1d``, ``ret_5d``, ``vol_20d``, ``mom_10d``, ``vol_ratio``
      - ``label`` — 0.0/1.0 (binary_forward_return) or -1.0/+1.0
        (triple_barrier, where +1 = profit-take, -1 = stop-loss,
        sign of return at timeout for vertical barrier)

    Rows without enough history (warmup) or forward data (label) are dropped.
    No look-ahead: every feature uses only data at or before ``decision_time``.
    """
    import numpy as np

    n = len(bars)
    if n < 25 + label_horizon_days:
        return []

    close = np.array([b["close"] for b in bars], dtype=np.float64)
    volume = np.array([b["volume"] for b in bars], dtype=np.float64)
    ts = np.array([b["ts_event"] for b in bars], dtype=np.int64)

    log_close = np.log(close)
    # log_ret[i] = log(close[i] / close[i-1]); log_ret[0] = 0 (placeholder).
    log_ret = np.zeros(n, dtype=np.float64)
    log_ret[1:] = np.diff(log_close)

    # --- triple-barrier labels (Tier 2.3) --------------------------------
    tb_label_map: dict[int, float] = {}  # bar index → label
    if label_method == "triple_barrier":
        from fincept_core.datasets import BarrierConfig, triple_barrier_labels, volatility_scaled_widths

        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]

        per_bar_widths = None
        if vol_scale_widths:
            per_bar_widths = volatility_scaled_widths(
                closes,
                window=20,
                profit_take_sigma=profit_take_width / 0.02,  # normalize
                stop_loss_sigma=stop_loss_width / 0.02,
            )

        cfg = BarrierConfig(
            profit_take_width=profit_take_width,
            stop_loss_width=stop_loss_width,
            horizon_bars=label_horizon_days,
        )
        tb_labels = triple_barrier_labels(
            highs, lows, closes, cfg, per_bar_widths=per_bar_widths,
        )
        for tbl in tb_labels:
            # Map triple-barrier label to float: +1 → 1.0, -1 → 0.0
            # (binary classification compatible: 1 = profit-take, 0 = stop-loss/timeout-negative)
            # Keep the raw -1/+1 in a separate column for meta-labeling.
            tb_label_map[tbl.index] = float(tbl.label)

    rows: list[dict[str, Any]] = []
    for i in range(n):
        # Warmup: need 20 days of history for vol_20d.
        if i < 20:
            continue
        # Label: need forward label_horizon_days of data.
        if i + label_horizon_days >= n:
            break

        # --- features (all use data at index <= i) -------------------------
        ret_1d = float(log_close[i] - log_close[i - 1])
        ret_5d = float(log_close[i] - log_close[i - 5]) if i >= 5 else 0.0
        vol_20d = float(np.std(log_ret[i - 19 : i + 1], ddof=0))
        mom_10d = float(close[i] / close[i - 10] - 1.0) if i >= 10 else 0.0
        vol_mean_20 = float(np.mean(volume[i - 19 : i + 1]))
        vol_ratio = float(volume[i] / vol_mean_20) if vol_mean_20 > 0 else 1.0

        # --- label (uses future data — this is the target, not a feature) --
        if label_method == "triple_barrier":
            raw_label = tb_label_map.get(i)
            if raw_label is None:
                continue  # bar was excluded by triple-barrier (insufficient data)
            # Convert to binary: +1 (profit-take) → 1.0, else 0.0
            label = 1.0 if raw_label > 0 else 0.0
        else:
            fwd_ret = float(log_close[i + label_horizon_days] - log_close[i])
            label = 1.0 if fwd_ret > 0.0 else 0.0

        rows.append(
            {
                "decision_time": int(ts[i]),
                "ret_1d": ret_1d,
                "ret_5d": ret_5d,
                "vol_20d": vol_20d,
                "mom_10d": mom_10d,
                "vol_ratio": vol_ratio,
                "label": label,
            },
        )
    return rows


# ---------------------------------------------------------------------------
# FeatureRow / Universe construction
# ---------------------------------------------------------------------------


def rows_to_feature_rows(
    data_rows: list[dict[str, Any]],
    label_horizon_days: int,
) -> tuple[FeatureRow, ...]:
    """Convert computed row dicts into PIT ``FeatureRow`` objects.

    Every feature's ``observed_at`` is set to the row's ``decision_time``
    (the bar close is when the vendor makes the value available).  This
    guarantees ``observed_at <= decision_time`` (equal).
    """
    horizon_ns = label_horizon_days * NS_PER_DAY
    out: list[FeatureRow] = []
    for r in data_rows:
        dt = int(r["decision_time"])
        features = tuple(
            FeatureValue(name=name, value=float(r[name]), observed_at=dt) for name in FEATURE_NAMES
        )
        out.append(
            FeatureRow(
                symbol=r["__symbol"],
                event_ts=dt,
                decision_time=dt,
                features=features,
                label_horizon_ns=horizon_ns,
            ),
        )
    return tuple(out)


def build_universe(symbols: Sequence[str]) -> tuple[UniverseEntry, ...]:
    """Build an as-of universe with all symbols still listed (no delistings)."""
    return tuple(
        UniverseEntry(symbol=s, listed_until=None, renamed_from=None) for s in sorted(symbols)
    )


# ---------------------------------------------------------------------------
# Parquet + manifest export
# ---------------------------------------------------------------------------


def write_dataset_parquet(
    data_rows: list[dict[str, Any]],
    out_path: pathlib.Path,
) -> int:
    """Write the training dataset (features + label) to a parquet file.

    Columns: ``decision_time``, feature columns, ``label``.
    Returns the number of rows written.
    """
    import polars as pl

    if not data_rows:
        # Write an empty parquet with the correct schema.
        schema = {
            "decision_time": pl.Int64,
            **{name: pl.Float64 for name in FEATURE_NAMES},
            "label": pl.Float64,
        }
        pl.DataFrame(schema=schema).write_parquet(str(out_path))
        return 0

    columns: dict[str, list[Any]] = {
        "decision_time": [int(r["decision_time"]) for r in data_rows],
    }
    for name in FEATURE_NAMES:
        columns[name] = [float(r[name]) for r in data_rows]
    columns["label"] = [float(r["label"]) for r in data_rows]

    df = pl.DataFrame(columns).sort("decision_time")
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(str(out_path))
    return df.height


def write_manifest_json(
    manifest: Any,
    availability: FeatureAvailabilityReport,
    out_path: pathlib.Path,
) -> None:
    """Write the manifest + availability report as a stable JSON file."""
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.loads(manifest.to_json())
    body["availability"] = json.loads(availability.to_json())
    body["feature_names"] = list(FEATURE_NAMES)
    out_path.write_text(json.dumps(body, sort_keys=True, indent=2))


# ---------------------------------------------------------------------------
# Top-level build
# ---------------------------------------------------------------------------


def build_dataset_manifest(
    bars_by_symbol: dict[str, list[dict[str, float]]],
    *,
    label_horizon_days: int,
    n_folds: int,
    dataset_id: str,
    source_vintage_refs: list[str] | None = None,
    label_method: str = "binary_forward_return",
    profit_take_width: float = 0.02,
    stop_loss_width: float = 0.02,
    vol_scale_widths: bool = False,
) -> tuple[Any, FeatureAvailabilityReport, tuple[FeatureRow, ...], list[dict[str, Any]]]:
    """Build a FeatureLakeManifest from bars grouped by symbol.

    Returns ``(manifest, availability, feature_rows, data_rows)`` where
    *data_rows* is the flat list of row dicts (with ``__symbol``) ready for
    parquet export.

    Tier 2.3: ``label_method`` selects between simple binary forward-return
    labels (default, legacy) and triple-barrier labels (AFML Ch. 3).
    """
    symbols = sorted(bars_by_symbol.keys())
    universe = build_universe(symbols)

    all_data_rows: list[dict[str, Any]] = []
    for sym in symbols:
        sym_rows = compute_features_and_labels(
            bars_by_symbol[sym],
            label_horizon_days,
            label_method=label_method,
            profit_take_width=profit_take_width,
            stop_loss_width=stop_loss_width,
            vol_scale_widths=vol_scale_widths,
        )
        for r in sym_rows:
            r["__symbol"] = sym
        all_data_rows.extend(sym_rows)

    feature_rows = rows_to_feature_rows(all_data_rows, label_horizon_days)

    f_hash = feature_schema_hash()
    l_hash = label_schema_hash(label_horizon_days, label_method)
    horizon_ns = label_horizon_days * NS_PER_DAY

    builder = FeatureLakeBuilder(
        dataset_id=dataset_id,
        universe=universe,
        rows=feature_rows,
        feature_schema_hash=f_hash,
        label_schema_hash=l_hash,
        max_label_horizon_ns=horizon_ns,
        n_folds=n_folds,
        source_vintage_refs=list(source_vintage_refs or []),
    )
    manifest = builder.build_manifest()
    availability = FeatureAvailabilityReport.from_rows(
        feature_rows,
        FEATURE_NAMES,
    )
    return manifest, availability, feature_rows, all_data_rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_symbols(s: str) -> list[str]:
    return [x.strip().upper() for x in s.split(",") if x.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_dataset_manifest",
        description=(
            "Build a point-in-time dataset manifest + parquet from OHLCV bars "
            "for the RealLightGBMTrainer."
        ),
    )
    parser.add_argument(
        "--input",
        "--bars-dir",
        dest="bars_dir",
        default="data/bars/",
        help="Directory containing parquet files with OHLCV bars (default: data/bars/).",
    )
    parser.add_argument(
        "--output",
        "--manifest-dir",
        dest="manifest_dir",
        default="data/datasets/",
        help="Directory to write the dataset parquet + manifest JSON (default: data/datasets/).",
    )
    parser.add_argument(
        "--symbols",
        default="AAPL,MSFT,GOOG,AMZN,NVDA",
        help="Comma-separated symbols (default: AAPL,MSFT,GOOG,AMZN,NVDA).",
    )
    parser.add_argument(
        "--start-date",
        default="2022-01-01",
        help="Start date YYYY-MM-DD (inclusive, default: 2022-01-01).",
    )
    parser.add_argument(
        "--end-date",
        default="2024-12-31",
        help="End date YYYY-MM-DD (inclusive, default: 2024-12-31).",
    )
    parser.add_argument(
        "--label-horizon-days",
        type=int,
        default=5,
        help="Label horizon in days (default: 5).",
    )
    parser.add_argument(
        "--train-window-days",
        type=int,
        default=252,
        help="Training window in days (default: 252).",
    )
    parser.add_argument(
        "--val-window-days",
        type=int,
        default=63,
        help="Validation window in days (default: 63).",
    )
    parser.add_argument(
        "--n-folds",
        type=int,
        default=3,
        help="Number of walk-forward folds (default: 3).",
    )
    parser.add_argument(
        "--dataset-id",
        default=None,
        help="Dataset ID (default: auto-generated from symbols + dates).",
    )
    parser.add_argument(
        "--use-db",
        action="store_true",
        help="Load bars from fincept_db instead of parquet files.",
    )
    # Tier 2.3: triple-barrier labeling options
    parser.add_argument(
        "--label-method",
        choices=["binary_forward_return", "triple_barrier"],
        default="binary_forward_return",
        help=(
            "Label method: 'binary_forward_return' (default, legacy) or "
            "'triple_barrier' (AFML Ch. 3)."
        ),
    )
    parser.add_argument(
        "--profit-take-width",
        type=float,
        default=0.02,
        help="Triple-barrier upper barrier width as fraction (default: 0.02 = 2%%).",
    )
    parser.add_argument(
        "--stop-loss-width",
        type=float,
        default=0.02,
        help="Triple-barrier lower barrier width as fraction (default: 0.02 = 2%%).",
    )
    parser.add_argument(
        "--vol-scale-widths",
        action="store_true",
        help="Scale triple-barrier widths by rolling volatility (AFML recommendation).",
    )
    args = parser.parse_args(argv)

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        raise SystemExit("--symbols must contain at least one ticker")

    start_ns = _date_to_ns(args.start_date)
    # end-date is inclusive → add one day for the half-open upper bound.
    end_ns = _date_to_ns(args.end_date) + NS_PER_DAY

    # --- load bars -------------------------------------------------------
    if args.use_db:
        import asyncio

        bars_by_symbol = asyncio.run(
            load_bars_from_db(symbols, start_ns, end_ns),
        )
    else:
        bars_by_symbol = load_bars_from_parquet(
            pathlib.Path(args.bars_dir),
            symbols,
            start_ns,
            end_ns,
        )

    if not bars_by_symbol:
        raise SystemExit(
            f"no bars found for {symbols} in {args.bars_dir} "
            f"({args.start_date}..{args.end_date}). "
            "Run scripts/ingest_bars.py first or use --use-db.",
        )

    # --- build manifest --------------------------------------------------
    dataset_id = args.dataset_id or (
        "ds:"
        + "_".join(symbols[:3])
        + f"_{args.start_date}_{args.end_date}_h{args.label_horizon_days}d"
    )
    if args.label_method == "triple_barrier":
        dataset_id += "_tb"

    source_refs = [
        f"bars_dir:{pathlib.Path(args.bars_dir).resolve()}",
        f"start:{args.start_date}",
        f"end:{args.end_date}",
        f"label_method:{args.label_method}",
    ]

    manifest, availability, _feature_rows, data_rows = build_dataset_manifest(
        bars_by_symbol,
        label_horizon_days=args.label_horizon_days,
        n_folds=args.n_folds,
        dataset_id=dataset_id,
        source_vintage_refs=source_refs,
        label_method=args.label_method,
        profit_take_width=args.profit_take_width,
        stop_loss_width=args.stop_loss_width,
        vol_scale_widths=args.vol_scale_widths,
    )

    if not data_rows:
        raise SystemExit(
            "no usable rows after feature/label computation "
            "(need >= 20 warmup + label horizon days of history per symbol).",
        )

    # --- export parquet + manifest ---------------------------------------
    manifest_dir = pathlib.Path(args.manifest_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = manifest_dir / f"{dataset_id}.parquet"
    manifest_path = manifest_dir / f"{dataset_id}.manifest.json"

    n_written = write_dataset_parquet(data_rows, parquet_path)
    write_manifest_json(manifest, availability, manifest_path)

    # Also write the export receipt (PIT proof).
    receipt = export_receipt(manifest, availability, manifest_dir)

    # --- report ----------------------------------------------------------
    m_hash = manifest.manifest_hash()
    print(f"[build_dataset] dataset_id     : {manifest.dataset_id}")
    print(f"[build_dataset] manifest_hash  : {m_hash}")
    print(f"[build_dataset] row_count      : {manifest.row_count}")
    print(f"[build_dataset] parquet rows   : {n_written}")
    print(f"[build_dataset] feature schema : {list(FEATURE_NAMES)}")
    print(f"[build_dataset] feature_schema_hash : {manifest.feature_schema_hash}")
    print(f"[build_dataset] label_schema_hash    : {manifest.label_schema_hash}")
    print(f"[build_dataset] pit_proof_verified   : {manifest.pit_proof_verified}")
    print(f"[build_dataset] parquet path    : {parquet_path}")
    print(f"[build_dataset] manifest path   : {manifest_path}")
    print(f"[build_dataset] receipt path    : {receipt.receipt_path}")
    print(f"[build_dataset] symbols         : {sorted(bars_by_symbol.keys())}")
    print(f"[build_dataset] folds           : {len(manifest.folds.folds)}")
    print(f"[build_dataset] embargo_ns      : {manifest.folds.embargo_ns}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
