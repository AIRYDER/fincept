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

In addition to the one-pass :meth:`DatasetComposer.build`, this module
supports **incremental / streaming** updates via
:meth:`DatasetComposer.build_incremental` and the convenience
auto-detecting :meth:`DatasetComposer.build_or_update`.  Incremental
builds only fetch media items newer than the last build's
``decision_time``, score + feature + label them, and append the new
rows to the existing parquet — avoiding a full rebuild on every daily
re-training cycle.  Build state is persisted to
``{output_dir}/incremental_state.json`` via :func:`save_incremental_state`
/ :func:`load_incremental_state` so subsequent runs know where to resume
from and can detect module-config changes that require a full rebuild.
"""

from __future__ import annotations

import asyncio
import dataclasses
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

#: Nanoseconds per day — used for the β-estimation history buffer in
#: incremental builds and the default label horizon.
NS_PER_DAY: int = 86_400_000_000_000

#: Default forward label horizon (5 trading days) in nanoseconds.
DEFAULT_LABEL_HORIZON_NS: int = 5 * NS_PER_DAY

#: How many days of price history to load before ``since_ns`` in an
#: incremental build so β-estimation + labels still have enough lookback.
_INCREMENTAL_PRICE_HISTORY_DAYS: int = 400


# --------------------------------------------------------------------------- #
# Incremental build state                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IncrementalState:
    """Persistent state for an incremental dataset build.

    Written to ``{output_dir}/incremental_state.json`` after every build
    so the next run knows the last ``decision_time`` processed and can
    detect module-config changes that require a full rebuild.

    Attributes:
        dataset_id: The dataset identifier this state belongs to.
        last_build_ns: The max ``decision_time`` in the existing dataset
            (resume point for the next incremental fetch).
        row_count: Number of rows in the existing dataset at save time.
        parquet_path: Path to the existing parquet file.
        manifest_path: Path to the existing manifest JSON file.
        module_config_hash: Hash of the module IDs + config used to build
            the dataset.  If a subsequent run uses a different config,
            :meth:`DatasetComposer.build_or_update` does a full rebuild.
    """

    dataset_id: str
    last_build_ns: int
    row_count: int
    parquet_path: str
    manifest_path: str
    module_config_hash: str


def save_incremental_state(
    state: IncrementalState,
    output_dir: pathlib.Path | str,
) -> pathlib.Path:
    """Write ``state`` to ``{output_dir}/incremental_state.json``.

    Returns the path to the written file.
    """
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "incremental_state.json"
    path.write_text(
        json.dumps(dataclasses.asdict(state), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return path


def load_incremental_state(
    output_dir: pathlib.Path | str,
) -> IncrementalState:
    """Read incremental build state from ``{output_dir}/incremental_state.json``.

    Raises ``FileNotFoundError`` if the state file does not exist and
    ``KeyError``/``TypeError`` if it is malformed.
    """
    path = pathlib.Path(output_dir) / "incremental_state.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    return IncrementalState(
        dataset_id=body["dataset_id"],
        last_build_ns=int(body["last_build_ns"]),
        row_count=int(body["row_count"]),
        parquet_path=body["parquet_path"],
        manifest_path=body["manifest_path"],
        module_config_hash=body["module_config_hash"],
    )


def _incremental_state_path(output_dir: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(output_dir) / "incremental_state.json"


# --------------------------------------------------------------------------- #
# DatasetComposer                                                              #
# --------------------------------------------------------------------------- #


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

    # ------------------------------------------------------------------ #
    # Module-config hashing (for incremental rebuild detection)          #
    # ------------------------------------------------------------------ #

    def module_config_hash(self) -> str:
        """Stable hash over the module IDs + config used by this composer.

        If this hash changes between runs, :meth:`build_or_update` does a
        full rebuild instead of an incremental append (different features
        cannot be safely merged into an existing parquet).
        """
        payload = {
            "universe": self.universe,
            "source": self.source,
            "sentiment": self.sentiment,
            "features": list(self.features),
            "label": self.label,
            "price_join": self.price_join,
            "config": self.config,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()

    # ------------------------------------------------------------------ #
    # Pipeline (shared by build / build_incremental)                     #
    # ------------------------------------------------------------------ #

    def _collect_rows(
        self,
        *,
        start_ns: int,
        end_ns: int,
        fetch_start_ns: int | None = None,
        item_min_ns: int | None = None,
        price_start_ns: int | None = None,
        label_horizon_ns: int = DEFAULT_LABEL_HORIZON_NS,
    ) -> tuple[list[FeatureRowData], list[str], list[str]]:
        """Run universe → source → sentiment → features → price → label.

        Args:
            start_ns: Lower bound for feature-row ``decision_time``.
            end_ns: Upper bound (exclusive) for feature-row ``decision_time``.
            fetch_start_ns: Lower bound passed to the source adapter's
                ``fetch`` (defaults to ``start_ns``).  In incremental
                builds this is ``since_ns`` so only new items are fetched.
            item_min_ns: If set, drop fetched items with
                ``available_at_ns <= item_min_ns`` (strictly-new filter).
            price_start_ns: Lower bound passed to the price joiner's
                ``load_bars`` (defaults to ``start_ns``).  In incremental
                builds this is bumped back to retain β-estimation history.
            label_horizon_ns: Forward label horizon (ns).

        Returns ``(labeled_rows, feature_names_ordered, symbols)``.
        """
        fetch_start = fetch_start_ns if fetch_start_ns is not None else start_ns
        price_start = price_start_ns if price_start_ns is not None else start_ns

        # --- 1. Universe ------------------------------------------------
        universe_mod = self._create(self.universe)
        symbols = universe_mod.select_symbols(start_ns=start_ns, end_ns=end_ns)
        if not symbols:
            raise ValueError("universe module returned no symbols")

        # --- 2. Source (may be async) -----------------------------------
        source_mod = self._create(self.source)
        fetch_result = source_mod.fetch(
            symbols=symbols,
            start_ns=fetch_start,
            end_ns=end_ns,
        )
        if asyncio.iscoroutine(fetch_result):
            items = asyncio.run(fetch_result)
        else:
            items = fetch_result
        if item_min_ns is not None:
            items = [i for i in items if i.available_at_ns > item_min_ns]
        if not items:
            raise ValueError("source module returned no media items")

        # --- 3. Sentiment ------------------------------------------------
        sentiment_mod = self._create(self.sentiment)
        sentiments = sentiment_mod.score(items)

        # --- 4. Features (merge multiple modules) -----------------------
        feature_mods = [self._create(fid) for fid in self.features]
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
            for sym, dt_map in fresult.items():
                if sym not in all_features:
                    all_features[sym] = {}
                for dt, feats in dt_map.items():
                    if dt not in all_features[sym]:
                        all_features[sym][dt] = {}
                    all_features[sym][dt].update(feats)
                    for fname in feats:
                        if fname not in feature_names_ordered:
                            feature_names_ordered.append(fname)

        # --- 5. Price join ----------------------------------------------
        price_mod = self._create(self.price_join)
        price_bars, benchmark_bars = price_mod.load_bars(
            symbols=symbols,
            start_ns=price_start,
            end_ns=end_ns,
        )

        # --- 6. Build FeatureRowData list -------------------------------
        rows: list[FeatureRowData] = []
        for sym, dt_map in all_features.items():
            for dt, feats in dt_map.items():
                if per_year_mod is not None:
                    feats = {**feats, **per_year_mod.annotate_row(dt)}
                rows.append(FeatureRowData(
                    symbol=sym,
                    decision_time=dt,
                    features=feats,
                ))

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

        return labeled_rows, feature_names_ordered, symbols

    # ------------------------------------------------------------------ #
    # Full build                                                         #
    # ------------------------------------------------------------------ #

    def build(
        self,
        *,
        output_dir: pathlib.Path,
        dataset_id: str,
        start_ns: int,
        end_ns: int,
        n_folds: int = 3,
        label_horizon_ns: int = DEFAULT_LABEL_HORIZON_NS,
    ) -> IngestionResult:
        """Build the dataset end-to-end and write artifacts.

        Returns an :class:`IngestionResult` with paths to the parquet,
        manifest, receipt, and quality report.
        """
        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        labeled_rows, feature_names, symbols = self._collect_rows(
            start_ns=start_ns,
            end_ns=end_ns,
            label_horizon_ns=label_horizon_ns,
        )

        parquet_path = output_dir / f"{dataset_id}.parquet"
        self._write_parquet(labeled_rows, feature_names, parquet_path)

        return self._write_manifest_artifacts(
            rows=labeled_rows,
            feature_names=feature_names,
            symbols=symbols,
            output_dir=output_dir,
            dataset_id=dataset_id,
            n_folds=n_folds,
            label_horizon_ns=label_horizon_ns,
            parquet_path=parquet_path,
            source_vintage_refs=self._vintage_refs(),
        )

    # ------------------------------------------------------------------ #
    # Incremental build                                                  #
    # ------------------------------------------------------------------ #

    def build_incremental(
        self,
        *,
        output_dir: pathlib.Path,
        dataset_id: str,
        since_ns: int,
        end_ns: int,
        existing_parquet_path: pathlib.Path,
        n_folds: int = 3,
        label_horizon_ns: int = DEFAULT_LABEL_HORIZON_NS,
    ) -> IngestionResult:
        """Append new data to an existing dataset instead of rebuilding.

        This fetches only media items with ``available_at_ns > since_ns``,
        scores + features + labels them, deduplicates against the existing
        parquet by ``(symbol, decision_time)``, appends the new rows via
        ``polars.concat([existing_df, new_df])``, and rewrites the manifest
        + receipt + quality report with the updated row count.

        Args:
            output_dir: Where to write the updated artifacts.
            dataset_id: Dataset identifier (matches the existing build).
            since_ns: Only fetch media items after this time.
            end_ns: Upper bound (exclusive) for new feature-row decision times.
            existing_parquet_path: The existing parquet to append to.
            n_folds: Purged-k-fold count for the rewritten manifest.
            label_horizon_ns: Forward label horizon (ns).

        Returns an :class:`IngestionResult` pointing at the updated parquet.
        """
        import polars as pl

        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        existing_parquet_path = pathlib.Path(existing_parquet_path)
        if not existing_parquet_path.exists():
            raise FileNotFoundError(
                f"existing parquet not found: {existing_parquet_path}",
            )

        existing_df = pl.read_parquet(str(existing_parquet_path))
        if existing_df.height == 0:
            raise ValueError("existing parquet is empty — call build() first")

        # Feature names are every column except the non-feature ones.
        non_feature_cols = {"decision_time", "symbol", "label"}
        existing_feature_names = [
            c for c in existing_df.columns if c not in non_feature_cols
        ]

        # Existing (symbol, decision_time) pairs for deduplication.
        existing_pairs: set[tuple[str, int]] = set(zip(
            existing_df["symbol"].to_list(),
            [int(v) for v in existing_df["decision_time"].to_list()],
        ))
        # Max decision_time in the existing dataset (informational + state).
        max_existing_dt = int(existing_df["decision_time"].max())

        # --- collect NEW rows -------------------------------------------
        new_rows, new_feature_names, symbols = self._collect_rows(
            start_ns=since_ns,
            end_ns=end_ns,
            fetch_start_ns=since_ns,
            item_min_ns=since_ns,
            price_start_ns=since_ns - _INCREMENTAL_PRICE_HISTORY_DAYS * NS_PER_DAY,
            label_horizon_ns=label_horizon_ns,
        )

        # Deduplicate: drop new rows whose (symbol, decision_time) already
        # exists in the existing parquet.
        new_rows = [
            r for r in new_rows
            if (r.symbol, r.decision_time) not in existing_pairs
        ]

        # Merge feature-name lists (preserve existing order, append new).
        all_feature_names = list(existing_feature_names)
        for fname in new_feature_names:
            if fname not in all_feature_names:
                all_feature_names.append(fname)

        parquet_path = output_dir / f"{dataset_id}.parquet"

        if not new_rows:
            # Nothing new — just refresh artifacts from the existing parquet.
            all_rows = self._rows_from_df(existing_df, all_feature_names)
            # Ensure parquet is present at the output location.
            if pathlib.Path(parquet_path) != existing_parquet_path:
                existing_df.write_parquet(str(parquet_path))
        else:
            # --- append new rows via polars.concat ----------------------
            new_df = self._df_from_rows(new_rows, all_feature_names)
            # Align columns: ensure existing_df has every feature column.
            for col in all_feature_names:
                if col not in existing_df.columns:
                    existing_df = existing_df.with_columns(
                        pl.lit(0.0).cast(pl.Float64).alias(col),
                    )
            # Align new_df column order to the combined schema.
            new_df = new_df.select(
                ["decision_time", "symbol"] + all_feature_names + ["label"],
            )
            existing_df = existing_df.select(
                ["decision_time", "symbol"] + all_feature_names + ["label"],
            )
            combined_df = pl.concat([existing_df, new_df]).sort("decision_time")
            combined_df.write_parquet(str(parquet_path))

            # Reconstruct the full row set for manifest/quality generation.
            all_rows = self._rows_from_df(combined_df, all_feature_names)

        return self._write_manifest_artifacts(
            rows=all_rows,
            feature_names=all_feature_names,
            symbols=symbols,
            output_dir=output_dir,
            dataset_id=dataset_id,
            n_folds=n_folds,
            label_horizon_ns=label_horizon_ns,
            parquet_path=parquet_path,
            source_vintage_refs=self._vintage_refs(),
        )

    # ------------------------------------------------------------------ #
    # Auto-detecting build-or-update                                     #
    # ------------------------------------------------------------------ #

    def build_or_update(
        self,
        *,
        output_dir: pathlib.Path,
        dataset_id: str,
        start_ns: int,
        end_ns: int,
        n_folds: int = 3,
        label_horizon_ns: int = DEFAULT_LABEL_HORIZON_NS,
    ) -> IngestionResult:
        """Auto-detect whether to do a full build or an incremental update.

        1. If ``{output_dir}/incremental_state.json`` does not exist (or the
           referenced parquet is missing / config hash changed), do a full
           :meth:`build` and save fresh incremental state.
        2. Otherwise load the state and call :meth:`build_incremental` with
           ``since_ns = last_build_ns``.
        3. Update the incremental state after the build.
        """
        output_dir = pathlib.Path(output_dir)
        state_path = _incremental_state_path(output_dir)
        current_hash = self.module_config_hash()

        do_full = True
        state: IncrementalState | None = None
        if state_path.exists():
            try:
                state = load_incremental_state(output_dir)
            except (KeyError, TypeError, ValueError):
                state = None
            if state is not None:
                parquet_ok = pathlib.Path(state.parquet_path).exists()
                hash_ok = state.module_config_hash == current_hash
                dataset_ok = state.dataset_id == dataset_id
                do_full = not (parquet_ok and hash_ok and dataset_ok)

        if do_full:
            result = self.build(
                output_dir=output_dir,
                dataset_id=dataset_id,
                start_ns=start_ns,
                end_ns=end_ns,
                n_folds=n_folds,
                label_horizon_ns=label_horizon_ns,
            )
        else:
            assert state is not None  # for type checkers
            result = self.build_incremental(
                output_dir=output_dir,
                dataset_id=dataset_id,
                since_ns=state.last_build_ns,
                end_ns=end_ns,
                existing_parquet_path=pathlib.Path(state.parquet_path),
                n_folds=n_folds,
                label_horizon_ns=label_horizon_ns,
            )

        # --- persist updated incremental state --------------------------
        import polars as pl

        df = pl.read_parquet(str(result.parquet_path))
        last_build_ns = int(df["decision_time"].max()) if df.height > 0 else 0
        new_state = IncrementalState(
            dataset_id=dataset_id,
            last_build_ns=last_build_ns,
            row_count=df.height,
            parquet_path=str(result.parquet_path),
            manifest_path=str(result.manifest_path),
            module_config_hash=current_hash,
        )
        save_incremental_state(new_state, output_dir)
        return result

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

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

    def _write_manifest_artifacts(
        self,
        *,
        rows: list[FeatureRowData],
        feature_names: list[str],
        symbols: list[str],
        output_dir: pathlib.Path,
        dataset_id: str,
        n_folds: int,
        label_horizon_ns: int,
        parquet_path: pathlib.Path,
        source_vintage_refs: list[str],
    ) -> IngestionResult:
        """Build manifest + receipt + quality report for ``rows``.

        The parquet is assumed to already be written at ``parquet_path``.
        """
        # Build universe entries
        universe = tuple(
            UniverseEntry(symbol=s, listed_until=None, renamed_from=None)
            for s in sorted(symbols)
        )

        # Build FeatureRow objects
        feature_rows: list[FeatureRow] = []
        for row in rows:
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

        # Quality report (parquet already written)
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
        manifest_path = output_dir / f"{dataset_id}.manifest.json"
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
        """Build the parquet + manifest + receipt + quality report.

        .. deprecated:: kept for backward compatibility — new code uses
           :meth:`build` / :meth:`_write_manifest_artifacts` directly.
        """
        output_dir = pathlib.Path(output_dir)
        parquet_path = output_dir / f"{dataset_id}.parquet"
        self._write_parquet(labeled_rows, feature_names, parquet_path)
        return self._write_manifest_artifacts(
            rows=labeled_rows,
            feature_names=feature_names,
            symbols=symbols,
            output_dir=output_dir,
            dataset_id=dataset_id,
            n_folds=n_folds,
            label_horizon_ns=label_horizon_ns,
            parquet_path=parquet_path,
            source_vintage_refs=source_vintage_refs,
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

        df = self._df_from_rows(rows, feature_names)
        df.write_parquet(str(out_path))

    def _df_from_rows(
        self,
        rows: list[FeatureRowData],
        feature_names: list[str],
    ):
        """Build a polars DataFrame from FeatureRowData rows."""
        import polars as pl

        columns: dict[str, list[Any]] = {
            "decision_time": [int(r.decision_time) for r in rows],
            "symbol": [r.symbol for r in rows],
        }
        for name in feature_names:
            columns[name] = [float(r.features.get(name, 0.0)) for r in rows]
        columns["label"] = [
            float(r.label) if r.label is not None else 0.0 for r in rows
        ]
        return pl.DataFrame(columns).sort("decision_time")

    def _rows_from_df(
        self,
        df,
        feature_names: list[str],
    ) -> list[FeatureRowData]:
        """Reconstruct :class:`FeatureRowData` list from a polars DataFrame."""
        n = df.height
        sym_col = df["symbol"].to_list()
        dt_col = [int(v) for v in df["decision_time"].to_list()]
        label_col = df["label"].to_list()
        feat_cols = {name: df[name].to_list() for name in feature_names}
        rows: list[FeatureRowData] = []
        for i in range(n):
            feats = {
                name: float(feat_cols[name][i])
                for name in feature_names
                if name in feat_cols
            }
            label_val = label_col[i]
            rows.append(FeatureRowData(
                symbol=sym_col[i],
                decision_time=dt_col[i],
                features=feats,
                label=float(label_val) if label_val is not None else None,
            ))
        return rows


__all__ = [
    "DatasetComposer",
    "IncrementalState",
    "save_incremental_state",
    "load_incremental_state",
]
