"""
scripts/build_synthetic_dataset.py — generate a synthetic OHLCV dataset +
point-in-time manifest for local testing of the RealLightGBMTrainer.

A smaller, self-contained companion to ``scripts/build_dataset_manifest.py``.
Instead of loading real bars from parquet or a database, it generates
synthetic OHLCV data via a numpy geometric-Brownian-motion random walk and
then reuses the *same* feature/label/manifest pipeline so the output is
structurally identical to a real dataset.

No external data dependencies — only numpy + polars (both lazy-imported).
Deterministic given the same ``--seed``.

Usage::

  uv run python scripts/build_synthetic_dataset.py \\
      --n-symbols 3 --n-days 500 --seed 42 \\
      --manifest-dir data/datasets/

The output parquet plugs directly into ``RealLightGBMTrainer`` via a
``file://`` URI, and the manifest JSON carries the PIT proof + purged-k-fold
boundaries.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence

# scripts/ are not packaged; make sibling imports work.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from build_dataset_manifest import (  # noqa: E402
    FEATURE_NAMES,
    NS_PER_DAY,
    build_dataset_manifest,
    write_dataset_parquet,
    write_manifest_json,
)
from quant_foundry.feature_lake import export_receipt  # noqa: E402

# Also ensure quant_foundry src is importable (build_dataset_manifest does
# this, but be explicit in case of partial imports).
_REPO_ROOT = _SCRIPTS_DIR.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if _QF_SRC.exists() and str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))


# ---------------------------------------------------------------------------
# Synthetic OHLCV generation
# ---------------------------------------------------------------------------


def generate_synthetic_bars(
    symbol: str,
    n_days: int,
    seed: int,
    start_price: float = 100.0,
    annualized_vol: float = 0.30,
    annualized_drift: float = 0.05,
) -> list[dict[str, float]]:
    """Generate *n_days* of synthetic daily OHLCV bars for *symbol*.

    The close path is a geometric Brownian motion.  Open/high/low are
    derived from the close with a small intraday range; volume is a noisy
    baseline.  All timestamps are daily increments starting from a fixed
    epoch (2022-01-01 UTC) so the data is deterministic and PIT-ordered.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    dt = 1.0 / 252.0  # one trading day
    sigma = annualized_vol
    mu = annualized_drift
    shocks = rng.normal(
        loc=(mu - 0.5 * sigma**2) * dt,
        scale=sigma * np.sqrt(dt),
        size=n_days,
    )
    log_path = np.cumsum(shocks)
    close = start_price * np.exp(log_path)

    # Intraday open/high/low around each close.
    intraday_range = np.abs(rng.normal(0, 0.01, size=n_days))
    open_ = close * (1.0 + rng.normal(0, 0.005, size=n_days))
    high = np.maximum(open_, close) * (1.0 + intraday_range)
    low = np.minimum(open_, close) * (1.0 - intraday_range)

    # Volume: baseline + noise + mild autocorrelation.
    vol_base = 1_000_000.0
    volume = vol_base * (1.0 + rng.normal(0, 0.3, size=n_days))
    volume = np.maximum(volume, 1.0)

    # Timestamps: daily increments from 2022-01-01 UTC.
    base_ns = 1_642_090_560 * 1_000_000_000  # 2022-01-01 00:00:00 UTC

    bars: list[dict[str, float]] = []
    for i in range(n_days):
        bars.append(
            {
                "ts_event": int(base_ns + i * NS_PER_DAY),
                "open": float(open_[i]),
                "high": float(high[i]),
                "low": float(low[i]),
                "close": float(close[i]),
                "volume": float(volume[i]),
            },
        )
    return bars


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _symbol_for_index(i: int) -> str:
    """Generate a deterministic synthetic symbol name."""
    # SYNA, SYNB, SYNC, ...
    return f"SYN{chr(ord('A') + i)}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_synthetic_dataset",
        description=(
            "Generate a synthetic OHLCV dataset + point-in-time manifest "
            "for local testing of the RealLightGBMTrainer."
        ),
    )
    parser.add_argument(
        "--output",
        "--manifest-dir",
        dest="manifest_dir",
        default="data/datasets/",
        help="Directory to write the dataset parquet + manifest JSON (default: data/datasets/).",
    )
    parser.add_argument(
        "--n-symbols",
        type=int,
        default=3,
        help="Number of synthetic symbols (default: 3).",
    )
    parser.add_argument(
        "--n-days",
        type=int,
        default=500,
        help="Number of daily bars per symbol (default: 500).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed (default: 42).",
    )
    parser.add_argument(
        "--label-horizon-days",
        type=int,
        default=5,
        help="Label horizon in days (default: 5).",
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
        help="Dataset ID (default: auto-generated).",
    )
    args = parser.parse_args(argv)

    if args.n_symbols < 1:
        raise SystemExit("--n-symbols must be >= 1")
    if args.n_days < 30:
        raise SystemExit("--n-days must be >= 30 (need warmup + label horizon)")

    # --- generate synthetic bars -----------------------------------------
    bars_by_symbol: dict[str, list[dict[str, float]]] = {}
    for i in range(args.n_symbols):
        sym = _symbol_for_index(i)
        # Per-symbol seed so each symbol has an independent path.
        bars_by_symbol[sym] = generate_synthetic_bars(
            sym,
            n_days=args.n_days,
            seed=args.seed + i * 1000,
        )

    # --- build manifest via the shared pipeline --------------------------
    dataset_id = args.dataset_id or (
        f"synthetic_s{args.n_symbols}_d{args.n_days}_h{args.label_horizon_days}d_seed{args.seed}"
    )

    source_refs = [
        "synthetic:geometric_brownian_motion",
        f"seed:{args.seed}",
        f"n_symbols:{args.n_symbols}",
        f"n_days:{args.n_days}",
    ]

    manifest, availability, feature_rows, data_rows = build_dataset_manifest(
        bars_by_symbol,
        label_horizon_days=args.label_horizon_days,
        n_folds=args.n_folds,
        dataset_id=dataset_id,
        source_vintage_refs=source_refs,
    )

    if not data_rows:
        raise SystemExit(
            "no usable rows after feature/label computation "
            "(increase --n-days).",
        )

    # --- export parquet + manifest ---------------------------------------
    manifest_dir = pathlib.Path(args.manifest_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = manifest_dir / f"{dataset_id}.parquet"
    manifest_path = manifest_dir / f"{dataset_id}.manifest.json"

    n_written = write_dataset_parquet(data_rows, parquet_path)
    write_manifest_json(manifest, availability, manifest_path)
    receipt = export_receipt(manifest, availability, manifest_dir)

    # --- report ----------------------------------------------------------
    m_hash = manifest.manifest_hash()
    print(f"[synthetic] dataset_id     : {manifest.dataset_id}")
    print(f"[synthetic] manifest_hash  : {m_hash}")
    print(f"[synthetic] row_count      : {manifest.row_count}")
    print(f"[synthetic] parquet rows   : {n_written}")
    print(f"[synthetic] feature schema : {list(FEATURE_NAMES)}")
    print(f"[synthetic] pit_proof_verified : {manifest.pit_proof_verified}")
    print(f"[synthetic] parquet path    : {parquet_path}")
    print(f"[synthetic] manifest path   : {manifest_path}")
    print(f"[synthetic] receipt path    : {receipt.receipt_path}")
    print(f"[synthetic] symbols         : {sorted(bars_by_symbol.keys())}")
    print(f"[synthetic] folds           : {len(manifest.folds.folds)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
