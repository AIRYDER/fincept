"""
quant_foundry.modules.composer — combines modules to build a dataset.

The :class:`DatasetComposer` is the orchestration layer that wires
modules together into a complete dataset-building pipeline:

    1. universe.select_symbols() → list of tickers
    2. source.fetch(symbols, start, end) → list of MediaItem
    3. sentiment.score(items) → list of SentimentResult
    4. feature.compute_features(items, sentiments) → {symbol: {dt: features}}
    5. price_join.load_bars(symbols) → asset + benchmark bars
    6. label.compute_labels(rows, price_bars, benchmark) → labeled rows
    7. Build FeatureLakeBuilder → manifest + parquet + receipt + quality

The output is the same :class:`IngestionResult` shape as the existing
``data_ingestion`` functions, so it drops straight into the RunPod
training pipeline via ``dataset_manifest_ref``.

Multiple feature modules can be composed — their features are merged
per ``(symbol, decision_time)``.  The per-year feature module is
applied as a post-processing annotation step.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

from quant_foundry.modules.registry import (
    FeatureRowData,
    MediaItem,
    ModuleRegistry,
    PriceBar,
    SentimentResult,
)


# Reuse the existing feature-lake + quality report infrastructure.
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


@dataclass
class DatasetComposer:
    """Combines modules to build a leakage-safe dataset.

    Each argument is a full module ID (``category:id:version``) that
    must be registered in the :class:`ModuleRegistry`.  Call
    :func:`load_all_modules` first to populate the registry.

    Args:
        universe: Universe selector module ID.
        source: Source adapter module ID (may be async).
        sentiment: Sentiment engine module ID.
        features: List of feature computer module IDs (merged).
        label: Label computer module ID.
        price_join: Price joiner module ID.
        config: Optional config overrides per module ID.
    """

    universe: str
    source: str
    sentiment: str
    features: list[str]
    label: str
    price_join: str
    config: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _create(self, full_id: str) -> Any:
        """Instantiate a module from the registry with optional config."""
        registry = ModuleRegistry.instance()
        return registry.create(
            full_id,
            config=self.config.get(full_id),
        )

    def build(
        self,
        *,
        output_dir: pathlib.Path,
        dataset_id: str,
        start_ns: int,
        end_ns: int,
        n_folds: int = 3,
        label_horizon_ns: int = 5 * 86_400_000_000_000,
    ) -> IngestionResult:
        """Build the dataset end-to-end and write artifacts.

        Returns an :class:`IngestionResult` with paths to the parquet,
        manifest, receipt, and quality report.
        """
        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # --- 1. Universe ------------------------------------------------
        universe_mod = self._create(self.universe)
        symbols = universe_mod.select_symbols(start_ns=start_ns, end_ns=end_ns)
        if not symbols:
            raise ValueError("universe module returned no symbols")

        # --- 2. Source (may be async) -----------------------------------
        source_mod = self._create(self.source)
        fetch_result = source_mod.fetch(
            symbols=symbols,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        if asyncio.iscoroutine(fetch_result):
            items = asyncio.run(fetch_result)
        else:
            items = fetch_result
        if not items:
            raise ValueError("source module returned no media items")

        # --- 3. Sentiment ------------------------------------------------
        sentiment_mod = self._create(self.sentiment)
        sentiments = sentiment_mod.score(items)

        # --- 4. Features (merge multiple modules) -----------------------
        feature_mods = [self._create(fid) for fid in self.features]
        # Collect features from each module
        all_features: dict[str, dict[int, dict[str, float]]] = {}
        feature_names_ordered: list[str] = []
        per_year_mod = None

        for fmod in feature_mods:
            # Check if this is a per-year module (passthrough)
            if hasattr(fmod, "annotate_row") and not hasattr(fmod, "compute_features"):
                per_year_mod = fmod
                continue

            fresult = fmod.compute_features(
                items,
                sentiments,
                symbols=symbols,
                start_ns=start_ns,
                end_ns=end_ns,
            )
            # Merge into all_features
            for sym, dt_map in fresult.items():
                if sym not in all_features:
                    all_features[sym] = {}
                for dt, feats in dt_map.items():
                    if dt not in all_features[sym]:
                        all_features[sym][dt] = {}
                    all_features[sym][dt].update(feats)
                    # Track feature names
                    for fname in feats:
                        if fname not in feature_names_ordered:
                            feature_names_ordered.append(fname)

        # --- 5. Price join ----------------------------------------------
        price_mod = self._create(self.price_join)
        price_bars, benchmark_bars = price_mod.load_bars(
            symbols=symbols,
            start_ns=start_ns,
            end_ns=end_ns,
        )

        # --- 6. Build FeatureRowData list -------------------------------
        rows: list[FeatureRowData] = []
        for sym, dt_map in all_features.items():
            for dt, feats in dt_map.items():
                # Apply per-year annotation if available
                if per_year_mod is not None:
                    feats = {**feats, **per_year_mod.annotate_row(dt)}
                rows.append(FeatureRowData(
                    symbol=sym,
                    decision_time=dt,
                    features=feats,
                ))

        # Update feature names with per-year features
        if per_year_mod is not None and rows:
            sample_annot = per_year_mod.annotate_row(rows[0].decision_time)
            for fname in sample_annot:
                if fname not in feature_names_ordered:
                    feature_names_ordered.append(fname)

        if not rows:
            raise ValueError("no feature rows generated from media items")

        # --- 7. Labels --------------------------------------------------
        label_mod = self._create(self.label)
        labeled_rows = label_mod.compute_labels(
            rows,
            price_bars=price_bars,
            benchmark_bars=benchmark_bars,
        )
        if not labeled_rows:
            raise ValueError("label module produced no labeled rows")

        # --- 8. Build FeatureLake dataset -------------------------------
        return self._build_dataset(
            labeled_rows=labeled_rows,
            feature_names=feature_names_ordered,
            symbols=symbols,
            output_dir=output_dir,
            dataset_id=dataset_id,
            n_folds=n_folds,
            label_horizon_ns=label_horizon_ns,
            source_vintage_refs=self._vintage_refs(),
        )

    def _vintage_refs(self) -> list[str]:
        """Record the module IDs used, for provenance."""
        return [
            f"universe:{self.universe}",
            f"source:{self.source}",
            f"sentiment:{self.sentiment}",
            f"features:{','.join(self.features)}",
            f"label:{self.label}",
            f"price_join:{self.price_join}",
        ]

    def _build_dataset(
        self,
        *,
        labeled_rows: list[FeatureRowData],
        feature_names: list[str],
        symbols: list[str],
        output_dir: pathlib.Path,
        dataset_id: str,
        n_folds: int,
        label_horizon_ns: int,
        source_vintage_refs: list[str],
    ) -> IngestionResult:
        """Build the parquet + manifest + receipt + quality report."""
        # Build universe entries
        universe = tuple(
            UniverseEntry(symbol=s, listed_until=None, renamed_from=None)
            for s in sorted(symbols)
        )

        # Build FeatureRow objects
        feature_rows: list[FeatureRow] = []
        for row in labeled_rows:
            dt = row.decision_time
            features = tuple(
                FeatureValue(name=name, value=float(row.features.get(name, 0.0)), observed_at=dt)
                for name in feature_names
            )
            feature_rows.append(FeatureRow(
                symbol=row.symbol,
                event_ts=dt,
                decision_time=dt,
                features=features,
                label_horizon_ns=label_horizon_ns,
            ))

        # Schema hashes
        f_hash = hashlib.sha256(
            ":".join(sorted(feature_names)).encode("utf-8"),
        ).hexdigest()
        l_hash = hashlib.sha256(
            f"abnormal_return_multi_horizon".encode("utf-8"),
        ).hexdigest()

        builder = FeatureLakeBuilder(
            dataset_id=dataset_id,
            universe=universe,
            rows=tuple(feature_rows),
            feature_schema_hash=f_hash,
            label_schema_hash=l_hash,
            max_label_horizon_ns=label_horizon_ns,
            n_folds=n_folds,
            source_vintage_refs=source_vintage_refs,
        )
        manifest = builder.build_manifest()
        availability = FeatureAvailabilityReport.from_rows(
            tuple(feature_rows), tuple(feature_names),
        )

        # Write parquet
        parquet_path = output_dir / f"{dataset_id}.parquet"
        manifest_path = output_dir / f"{dataset_id}.manifest.json"
        self._write_parquet(labeled_rows, feature_names, parquet_path)

        # Quality report
        quality_report = compute_quality_report(
            parquet_path, manifest, feature_names=tuple(feature_names),
        )
        quality_path = output_dir / f"{dataset_id}.quality.json"
        quality_report.write(quality_path)

        # Embed quality hash in manifest
        manifest = manifest.model_copy(
            update={"quality_report_hash": quality_report.quality_hash()},
        )

        # Write manifest + receipt
        body = json.loads(manifest.to_json())
        body["availability"] = json.loads(availability.to_json())
        body["feature_names"] = list(feature_names)
        manifest_path.write_text(
            json.dumps(body, sort_keys=True, indent=2), encoding="utf-8",
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

    def _write_parquet(
        self,
        rows: list[FeatureRowData],
        feature_names: list[str],
        out_path: pathlib.Path,
    ) -> None:
        """Write the dataset to parquet (decision_time, features, label)."""
        import polars as pl

        out_path = pathlib.Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if not rows:
            schema = {
                "decision_time": pl.Int64,
                "symbol": pl.Utf8,
                **{name: pl.Float64 for name in feature_names},
                "label": pl.Float64,
            }
            pl.DataFrame(schema=schema).write_parquet(str(out_path))
            return

        columns: dict[str, list[Any]] = {
            "decision_time": [int(r.decision_time) for r in rows],
            "symbol": [r.symbol for r in rows],
        }
        for name in feature_names:
            columns[name] = [float(r.features.get(name, 0.0)) for r in rows]
        columns["label"] = [float(r.label) if r.label is not None else 0.0 for r in rows]

        df = pl.DataFrame(columns).sort("decision_time")
        df.write_parquet(str(out_path))


__all__ = ["DatasetComposer"]
