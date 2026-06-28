"""
quant_foundry.data_ingestion.equities — ingest real equity OHLCV bars into a
leakage-safe point-in-time dataset.

This module wraps the shared feature/label/manifest pipeline from
``scripts/build_dataset_manifest.py`` (imported without modifying it) and adds
a quality report alongside the standard parquet + manifest + receipt export.

The output is a fully leakage-safe dataset:

- A parquet file with ``decision_time``, the 5 features, and a binary label.
- A ``FeatureLakeManifest`` JSON with purged-k-fold + embargo boundaries.
- An export receipt proving PIT correctness.
- A :class:`DatasetQualityReport` JSON with coverage, feature quality, label
  balance, fold quality, leakage checks, and drift indicators.

Heavy dependencies (numpy, polars) are imported lazily via the shared
``build_dataset_manifest`` helpers so this module is importable without them.
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from quant_foundry.data_ingestion.quality_report import compute_quality_report
from quant_foundry.feature_lake import export_receipt

# ---------------------------------------------------------------------------
# Import the shared pipeline from scripts/build_dataset_manifest.py without
# modifying it.  scripts/ is not a package, so prepend it to sys.path.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[5] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from build_dataset_manifest import (  # noqa: E402
    FEATURE_NAMES,
    build_dataset_manifest,
    load_bars_from_parquet,
    write_dataset_parquet,
    write_manifest_json,
)

if TYPE_CHECKING:
    from quant_foundry.data_ingestion.quality_report import DatasetQualityReport
    from quant_foundry.dataset_manifest import FeatureLakeManifest


@dataclass(frozen=True)
class IngestionResult:
    """Result of a dataset ingestion run.

    Holds the paths to every emitted artifact plus the in-memory manifest and
    quality report so callers can inspect them without re-reading from disk.
    """

    parquet_path: pathlib.Path
    manifest_path: pathlib.Path
    receipt_path: pathlib.Path
    quality_path: pathlib.Path
    manifest: FeatureLakeManifest
    quality_report: DatasetQualityReport


def ingest_equity_bars(
    bars_path: pathlib.Path,
    *,
    output_dir: pathlib.Path,
    dataset_id: str,
    symbols: list[str] | None = None,
    label_horizon_days: int = 5,
    n_folds: int = 3,
    source_vintage_refs: list[str] | None = None,
) -> IngestionResult:
    """Ingest equity OHLCV bars into a leakage-safe dataset.

    Parameters
    ----------
    bars_path
        Path to a parquet file (or directory of per-symbol parquet files) with
        OHLCV bars.  Expected schema: ``symbol, ts_event, open, high, low,
        close, volume`` (as produced by ``scripts/ingest_bars.py``).
    output_dir
        Directory to write the dataset parquet + manifest + receipt + quality
        report.  Created if it does not exist.
    dataset_id
        Unique dataset identifier (non-empty).
    symbols
        Optional list of symbols to filter to.  If ``None``, all symbols in
        the parquet are used.
    label_horizon_days
        Forward-return label horizon in days (default 5).
    n_folds
        Number of purged-k-fold validation windows (default 3).
    source_vintage_refs
        Optional provenance references recorded on the manifest.

    Returns
    -------
    IngestionResult
        Paths to all emitted artifacts plus the manifest and quality report.
    """
    bars_path = pathlib.Path(bars_path)
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- load bars -------------------------------------------------------
    # Use a wide time window so all bars in the file are included; the
    # caller can pre-filter by supplying a trimmed parquet.
    if symbols is None:
        # Peek at the parquet to discover symbols if not provided.
        import polars as pl

        if bars_path.is_dir():
            found: set[str] = set()
            for p in sorted(bars_path.glob("*.parquet")):
                df = pl.read_parquet(str(p))
                if "symbol" in df.columns:
                    found.update(df["symbol"].unique().to_list())
            symbols = sorted(found)
        else:
            df = pl.read_parquet(str(bars_path))
            if "symbol" in df.columns:
                symbols = sorted(df["symbol"].unique().to_list())
            else:
                symbols = ["UNKNOWN"]

    if not symbols:
        raise ValueError(
            f"no symbols found in {bars_path}; pass symbols= explicitly",
        )

    # Use the full available time range; load_bars_from_parquet filters to
    # [start_ns, end_ns).
    start_ns = 0
    end_ns = 9_999_999_999_999_999_999  # far future
    bars_by_symbol = load_bars_from_parquet(
        bars_path if bars_path.is_dir() else bars_path.parent,
        symbols,
        start_ns,
        end_ns,
    )

    # If bars_path is a single file (not a dir), load_bars_from_parquet may
    # not pick it up unless it matches <symbol>.parquet or is globbed.  Load
    # it directly as a fallback.
    if not bars_by_symbol and bars_path.is_file():
        import polars as pl

        df = pl.read_parquet(str(bars_path))
        keep = [
            c
            for c in ("symbol", "ts_event", "open", "high", "low", "close", "volume")
            if c in df.columns
        ]
        df = df.select(keep)
        for sym in symbols:
            sub = df.filter(pl.col("symbol") == sym).sort("ts_event")
            if sub.height == 0:
                continue
            bars_by_symbol[sym] = [
                {
                    "ts_event": int(row["ts_event"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
                for row in sub.iter_rows(named=True)
            ]

    if not bars_by_symbol:
        raise ValueError(
            f"no bars loaded from {bars_path} for symbols {symbols}",
        )

    # --- build manifest via the shared pipeline --------------------------
    refs = list(source_vintage_refs or [])
    refs.append(f"bars_path:{bars_path.resolve()}")

    manifest, availability, _feature_rows, data_rows = build_dataset_manifest(
        bars_by_symbol,
        label_horizon_days=label_horizon_days,
        n_folds=n_folds,
        dataset_id=dataset_id,
        source_vintage_refs=refs,
    )

    if not data_rows:
        raise ValueError(
            "no usable rows after feature/label computation "
            "(need >= 20 warmup + label horizon days of history per symbol).",
        )

    # --- export parquet --------------------------------------------------
    parquet_path = output_dir / f"{dataset_id}.parquet"
    manifest_path = output_dir / f"{dataset_id}.manifest.json"

    write_dataset_parquet(data_rows, parquet_path)

    # --- compute quality report then embed its hash in the manifest ------
    quality_report = compute_quality_report(
        parquet_path,
        manifest,
        feature_names=FEATURE_NAMES,
    )
    quality_path = output_dir / f"{dataset_id}.quality.json"
    quality_report.write(quality_path)

    # Re-emit the manifest with the quality report hash so consumers can
    # verify the manifest was produced alongside this specific quality report.
    manifest = manifest.model_copy(
        update={"quality_report_hash": quality_report.quality_hash()},
    )

    # --- write manifest + receipt ----------------------------------------
    write_manifest_json(manifest, availability, manifest_path)
    receipt = export_receipt(manifest, availability, output_dir)

    return IngestionResult(
        parquet_path=parquet_path,
        manifest_path=manifest_path,
        receipt_path=receipt.receipt_path,
        quality_path=quality_path,
        manifest=manifest,
        quality_report=quality_report,
    )


__all__ = [
    "FEATURE_NAMES",
    "IngestionResult",
    "ingest_equity_bars",
]
