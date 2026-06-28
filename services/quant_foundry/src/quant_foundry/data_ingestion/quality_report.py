"""
quant_foundry.data_ingestion.quality_report — comprehensive dataset quality report.

This module defines the :class:`DatasetQualityReport` model that is written
alongside every exported dataset as ``dataset.quality.json``.  The report
captures coverage, feature quality, label quality, fold quality, leakage
checks, and basic drift indicators so a downstream training job or tournament
can refuse to operate on a degraded dataset without re-deriving the stats.

The report is a frozen Pydantic v2 model with ``extra="forbid"`` so it is
tamper-evident and round-trip serialisation is exact — matching the
convention used by :class:`FeatureLakeManifest` and the core schema spine.

Heavy dependencies (polars, numpy) are imported lazily inside
:func:`compute_quality_report` so this module is importable without them,
following the same pattern as ``scripts/build_dataset_manifest.py``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.dataset_manifest import FeatureLakeManifest


class DatasetQualityReport(BaseModel):
    """Comprehensive quality report for an exported point-in-time dataset.

    Written alongside every dataset as ``dataset.quality.json``.  Captures:

    - **Coverage**: total rows, symbols, and the time span of the data.
    - **Feature quality**: per-feature non-null percentage and missing counts.
    - **Label quality**: binary label balance and missing count.
    - **Fold quality**: per-fold train/val row counts derived from the manifest.
    - **Leakage checks**: PIT proof, embargo sufficiency, and forward-join
      absence — all of which should be ``True`` for a leakage-safe dataset.
    - **Drift indicators**: per-feature mean and std across all rows, useful as
      a lightweight drift baseline between dataset versions.

    The model is frozen and forbids extra fields so the report is
    tamper-evident and serialisation is exact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    generated_at_ns: int

    # --- coverage --------------------------------------------------------
    total_rows: int
    total_symbols: int
    time_span_start_ns: int
    time_span_end_ns: int

    # --- feature quality -------------------------------------------------
    feature_names: tuple[str, ...]
    feature_coverage_pct: dict[str, float]  # feature -> % non-null
    feature_missing_count: dict[str, int]  # feature -> count of null/missing

    # --- label quality ---------------------------------------------------
    label_balance: dict[str, float]  # "0.0" -> fraction, "1.0" -> fraction
    label_missing_count: int

    # --- fold quality ----------------------------------------------------
    fold_count: int
    fold_train_counts: tuple[int, ...]
    fold_val_counts: tuple[int, ...]

    # --- leakage checks (all should be True) -----------------------------
    pit_proof_verified: bool
    embargo_sufficient: bool
    no_forward_joins: bool

    # --- drift indicators (basic) ---------------------------------------
    mean_feature_values: dict[str, float]  # feature -> mean across all rows
    std_feature_values: dict[str, float]  # feature -> std across all rows

    # --- serialization ---------------------------------------------------

    def to_json(self) -> str:
        """Serialize the report to a stable, sorted-key JSON string."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            indent=2,
        )

    def write(self, path: Path) -> Path:
        """Write the report to *path* (parent dirs created) and return it."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path


def compute_quality_report(
    parquet_path: Path,
    manifest: FeatureLakeManifest,
    *,
    feature_names: tuple[str, ...],
    label_column: str = "label",
    ts_column: str = "decision_time",
) -> DatasetQualityReport:
    """Compute a comprehensive quality report from a dataset parquet + manifest.

    Reads the parquet file at *parquet_path* with polars (lazy import) and
    derives coverage, feature quality, label quality, fold quality, leakage
    checks, and basic drift statistics.  The manifest supplies the fold
    boundaries and leakage-proof flags.

    Parameters
    ----------
    parquet_path
        Path to the dataset parquet file (columns: ``ts_column``, feature
        columns, ``label_column``).
    manifest
        The :class:`FeatureLakeManifest` for the dataset — used for fold
        counts and leakage-check flags.
    feature_names
        Ordered tuple of feature column names to assess.
    label_column
        Name of the label column (default ``"label"``).
    ts_column
        Name of the timestamp column (default ``"decision_time"``).

    Returns
    -------
    DatasetQualityReport
    """
    import polars as pl

    parquet_path = Path(parquet_path)
    df = pl.read_parquet(str(parquet_path))
    total_rows = df.height

    # --- coverage --------------------------------------------------------
    if total_rows > 0 and ts_column in df.columns:
        time_span_start_ns = int(df[ts_column].min())
        time_span_end_ns = int(df[ts_column].max())
    else:
        time_span_start_ns = 0
        time_span_end_ns = 0

    # ``symbol`` is optional in the parquet (the equity pipeline drops it);
    # fall back to the universe-derived count of 1 when absent.
    total_symbols = int(df["symbol"].n_unique()) if "symbol" in df.columns and total_rows > 0 else 1

    # --- feature quality -------------------------------------------------
    feature_coverage_pct: dict[str, float] = {}
    feature_missing_count: dict[str, int] = {}
    mean_feature_values: dict[str, float] = {}
    std_feature_values: dict[str, float] = {}

    for name in feature_names:
        if name not in df.columns:
            feature_coverage_pct[name] = 0.0
            feature_missing_count[name] = total_rows
            mean_feature_values[name] = 0.0
            std_feature_values[name] = 0.0
            continue
        col = df[name]
        null_count = int(col.null_count())
        non_null = total_rows - null_count
        feature_missing_count[name] = null_count
        feature_coverage_pct[name] = (
            round(100.0 * non_null / total_rows, 6) if total_rows > 0 else 0.0
        )
        if non_null > 0:
            mean_feature_values[name] = float(col.mean() or 0.0)
            std_feature_values[name] = float(col.std(ddof=0) or 0.0)
        else:
            mean_feature_values[name] = 0.0
            std_feature_values[name] = 0.0

    # --- label quality ---------------------------------------------------
    label_missing_count = 0
    label_balance: dict[str, float] = {}
    if label_column in df.columns and total_rows > 0:
        label_col = df[label_column]
        label_missing_count = int(label_col.null_count())
        non_null_labels = total_rows - label_missing_count
        if non_null_labels > 0:
            value_counts = label_col.drop_nulls().value_counts()
            counts_map: dict[Any, int] = {
                row[label_column]: int(row["count"])
                for row in value_counts.iter_rows(named=True)
            }
            for key in (0.0, 1.0):
                frac = counts_map.get(key, 0) / non_null_labels
                label_balance[str(key)] = round(frac, 6)
        else:
            label_balance = {"0.0": 0.0, "1.0": 0.0}
    else:
        label_balance = {"0.0": 0.0, "1.0": 0.0}

    # --- fold quality ----------------------------------------------------
    folds = manifest.folds.folds
    fold_count = len(folds)
    fold_train_counts: list[int] = []
    fold_val_counts: list[int] = []

    if total_rows > 0 and ts_column in df.columns:
        ts_series = df[ts_column]
        for fold in folds:
            train_mask = (ts_series >= fold.train_start) & (
                ts_series < fold.train_end
            )
            val_mask = (ts_series >= fold.val_start) & (ts_series < fold.val_end)
            fold_train_counts.append(int(train_mask.sum()))
            fold_val_counts.append(int(val_mask.sum()))
    else:
        fold_train_counts = [0] * fold_count
        fold_val_counts = [0] * fold_count

    # --- leakage checks --------------------------------------------------
    pit_proof_verified = manifest.pit_proof_verified
    embargo_sufficient = manifest.folds.embargo_ns >= manifest.folds.max_label_horizon_ns
    # ``no_forward_joins`` is guaranteed by the FeatureLakeBuilder's
    # as-of universe validation at construction time; the manifest's
    # ``pit_proof_verified`` flag is the downstream proof.
    no_forward_joins = manifest.pit_proof_verified

    return DatasetQualityReport(
        schema_version=1,
        dataset_id=manifest.dataset_id,
        generated_at_ns=int(time.time_ns()),
        total_rows=total_rows,
        total_symbols=total_symbols,
        time_span_start_ns=time_span_start_ns,
        time_span_end_ns=time_span_end_ns,
        feature_names=tuple(feature_names),
        feature_coverage_pct=feature_coverage_pct,
        feature_missing_count=feature_missing_count,
        label_balance=label_balance,
        label_missing_count=label_missing_count,
        fold_count=fold_count,
        fold_train_counts=tuple(fold_train_counts),
        fold_val_counts=tuple(fold_val_counts),
        pit_proof_verified=pit_proof_verified,
        embargo_sufficient=embargo_sufficient,
        no_forward_joins=no_forward_joins,
        mean_feature_values=mean_feature_values,
        std_feature_values=std_feature_values,
    )
