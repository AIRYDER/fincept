"""
quant_foundry.data_ingestion.macro — ingest macro economic indicators into a
leakage-safe point-in-time dataset.

This module implements a minimal ingestion path for macro economic data
(e.g. interest rates, GDP, CPI).  It reads a CSV with columns
``date, indicator, value`` and builds a simple dataset where each row is a
macro indicator observation at a given date.

Features are derived per indicator:

- ``value`` — the raw indicator value.
- ``value_diff_1`` — first difference of the value within the same indicator.
- ``value_pct_change_1`` — percentage change of the value within the same
  indicator.

Labels are binary: ``1.0`` if the next observation of the same indicator is
higher than the current one (up), ``0.0`` otherwise (down/flat).  This is a
PIT-correct target — it uses the future observation but is the label, not a
feature.

Heavy dependencies (polars) are imported lazily so this module is importable
without them.
"""

from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from datetime import UTC, datetime
from typing import Any

from quant_foundry.data_ingestion.equities import IngestionResult
from quant_foundry.data_ingestion.quality_report import compute_quality_report
from quant_foundry.feature_availability import FeatureAvailabilityReport
from quant_foundry.feature_lake import (
    FeatureLakeBuilder,
    FeatureRow,
    FeatureValue,
    UniverseEntry,
    export_receipt,
)

MACRO_FEATURE_NAMES: tuple[str, ...] = (
    "value",
    "value_diff_1",
    "value_pct_change_1",
)

NS_PER_DAY = 86_400_000_000_000


def macro_feature_schema_hash() -> str:
    """SHA-256 over the sorted, colon-joined macro feature names."""
    payload = ":".join(sorted(MACRO_FEATURE_NAMES))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def macro_label_schema_hash() -> str:
    """SHA-256 over the macro label description."""
    payload = "macro_next_observation_direction"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _date_to_ns(date_str: str) -> int:
    """Parse a YYYY-MM-DD date string to nanoseconds since epoch (UTC)."""
    dt = datetime.fromisoformat(date_str.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1_000_000_000


def _load_macro_csv(csv_path: pathlib.Path) -> list[dict[str, str]]:
    """Load a macro CSV with columns: date, indicator, value."""
    csv_path = pathlib.Path(csv_path)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "indicator", "value"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(
                f"macro CSV must have columns {sorted(required)}; "
                f"got {reader.fieldnames}",
            )
        return list(reader)


def _compute_macro_features_and_labels(
    raw_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Compute macro features + forward-direction labels from raw CSV rows.

    Groups by indicator, sorts by date, computes first-difference and
    percentage-change features, and a binary label (1 if next value is
    higher).
    """
    # Group by indicator.
    by_indicator: dict[str, list[tuple[int, float]]] = {}
    for row in raw_rows:
        indicator = row["indicator"].strip()
        ts = _date_to_ns(row["date"])
        value = float(row["value"])
        by_indicator.setdefault(indicator, []).append((ts, value))

    all_data_rows: list[dict[str, Any]] = []
    for indicator, obs in sorted(by_indicator.items()):
        obs.sort(key=lambda x: x[0])
        n = len(obs)
        for i in range(n):
            ts, value = obs[i]
            # Features use only data at index <= i.
            if i > 0:
                prev_value = obs[i - 1][1]
                value_diff_1 = float(value - prev_value)
                value_pct_change_1 = (
                    float((value - prev_value) / prev_value)
                    if prev_value != 0
                    else 0.0
                )
            else:
                value_diff_1 = 0.0
                value_pct_change_1 = 0.0

            # Label: next observation direction (uses future data — target).
            if i + 1 < n:
                next_value = obs[i + 1][1]
                label = 1.0 if next_value > value else 0.0
            else:
                # No future observation: drop the row (no label).
                continue

            all_data_rows.append(
                {
                    "decision_time": ts,
                    "__symbol": indicator,
                    "value": float(value),
                    "value_diff_1": value_diff_1,
                    "value_pct_change_1": value_pct_change_1,
                    "label": label,
                },
            )
    return all_data_rows


def _write_macro_parquet(
    data_rows: list[dict[str, Any]],
    out_path: pathlib.Path,
) -> int:
    """Write the macro dataset (features + label) to a parquet file."""
    import polars as pl

    if not data_rows:
        schema = {
            "decision_time": pl.Int64,
            "symbol": pl.Utf8,
            **{name: pl.Float64 for name in MACRO_FEATURE_NAMES},
            "label": pl.Float64,
        }
        pl.DataFrame(schema=schema).write_parquet(str(out_path))
        return 0

    columns: dict[str, list[Any]] = {
        "decision_time": [int(r["decision_time"]) for r in data_rows],
        "symbol": [str(r["__symbol"]) for r in data_rows],
    }
    for name in MACRO_FEATURE_NAMES:
        columns[name] = [float(r[name]) for r in data_rows]
    columns["label"] = [float(r["label"]) for r in data_rows]

    df = pl.DataFrame(columns).sort("decision_time")
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(str(out_path))
    return df.height


def ingest_macro_indicators(
    csv_path: pathlib.Path,
    *,
    output_dir: pathlib.Path,
    dataset_id: str,
    n_folds: int = 3,
) -> IngestionResult:
    """Ingest macro economic indicators into a leakage-safe dataset.

    Parameters
    ----------
    csv_path
        Path to a CSV with columns ``date, indicator, value``.
    output_dir
        Directory to write the dataset parquet + manifest + receipt + quality
        report.  Created if it does not exist.
    dataset_id
        Unique dataset identifier (non-empty).
    n_folds
        Number of purged-k-fold validation windows (default 3).

    Returns
    -------
    IngestionResult
        Paths to all emitted artifacts plus the manifest and quality report.
    """
    csv_path = pathlib.Path(csv_path)
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_rows = _load_macro_csv(csv_path)
    if not raw_rows:
        raise ValueError(f"no rows in macro CSV {csv_path}")

    data_rows = _compute_macro_features_and_labels(raw_rows)
    if not data_rows:
        raise ValueError(
            "no usable rows after macro feature/label computation "
            "(need >= 2 observations per indicator for a label).",
        )

    # --- build feature rows + universe ----------------------------------
    symbols = sorted({r["__symbol"] for r in data_rows})
    universe = tuple(
        UniverseEntry(symbol=s, listed_until=None, renamed_from=None)
        for s in symbols
    )

    # Macro labels are per-observation direction; use a 1-day horizon for
    # the embargo (the label window is to the next observation, which is
    # typically >= 1 day for macro data).
    horizon_ns = NS_PER_DAY
    feature_rows: list[FeatureRow] = []
    for r in data_rows:
        dt = int(r["decision_time"])
        features = tuple(
            FeatureValue(name=name, value=float(r[name]), observed_at=dt)
            for name in MACRO_FEATURE_NAMES
        )
        feature_rows.append(
            FeatureRow(
                symbol=r["__symbol"],
                event_ts=dt,
                decision_time=dt,
                features=features,
                label_horizon_ns=horizon_ns,
            ),
        )

    f_hash = macro_feature_schema_hash()
    l_hash = macro_label_schema_hash()

    builder = FeatureLakeBuilder(
        dataset_id=dataset_id,
        universe=universe,
        rows=tuple(feature_rows),
        feature_schema_hash=f_hash,
        label_schema_hash=l_hash,
        max_label_horizon_ns=horizon_ns,
        n_folds=n_folds,
        source_vintage_refs=[
            f"macro_csv_path:{csv_path.resolve()}",
        ],
    )
    manifest = builder.build_manifest()
    availability = FeatureAvailabilityReport.from_rows(
        tuple(feature_rows),
        MACRO_FEATURE_NAMES,
    )

    # --- export parquet --------------------------------------------------
    parquet_path = output_dir / f"{dataset_id}.parquet"
    manifest_path = output_dir / f"{dataset_id}.manifest.json"

    _write_macro_parquet(data_rows, parquet_path)

    # --- compute quality report then embed its hash in the manifest ------
    quality_report = compute_quality_report(
        parquet_path,
        manifest,
        feature_names=MACRO_FEATURE_NAMES,
    )
    quality_path = output_dir / f"{dataset_id}.quality.json"
    quality_report.write(quality_path)

    manifest = manifest.model_copy(
        update={"quality_report_hash": quality_report.quality_hash()},
    )

    # --- write manifest + receipt ----------------------------------------
    body = json.loads(manifest.to_json())
    body["availability"] = json.loads(availability.to_json())
    body["feature_names"] = list(MACRO_FEATURE_NAMES)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(body, sort_keys=True, indent=2),
        encoding="utf-8",
    )

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
    "MACRO_FEATURE_NAMES",
    "ingest_macro_indicators",
    "macro_feature_schema_hash",
    "macro_label_schema_hash",
]
