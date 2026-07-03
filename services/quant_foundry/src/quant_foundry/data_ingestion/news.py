"""
quant_foundry.data_ingestion.news — ingest news events into a leakage-safe
point-in-time dataset.

This module loads vendor news events (in the format produced by the
``news-impact-model`` experiment's :func:`load_vendor_news_events`), derives
simple text-based features, and builds a :class:`FeatureLakeManifest` with a
purged-k-fold + embargo structure and a :class:`DatasetQualityReport`.

News features are text-derived and deliberately simple but functional:

- ``headline_len`` — length of the headline in characters.
- ``body_len`` — length of the body in characters.
- ``sentiment_proxy`` — a naive sentiment proxy in ``[-1, 1]`` computed from
  positive/negative word counts in the headline + body.
- ``event_type_count`` — number of distinct event types seen at or before the
  event's availability time (a coarse news-flow indicator).
- ``symbol_count`` — number of symbols linked to the event.

Labels are binary: ``1.0`` if a subsequent event for the same symbol arrives
within ``label_horizon_days`` (more news → attention), ``0.0`` otherwise.
This is a simple, PIT-correct label: it uses future event availability times
but is the *target*, not a feature.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
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

# ---------------------------------------------------------------------------
# Import the news event loader from the news-impact-model experiment without
# modifying it.  The experiment is a workspace member but not on the
# quant_foundry import path, so add its src dir to sys.path.
#
# The repo root is at parents[5] in the dev checkout but only parents[3] in
# the RunPod worker image.  Guard the index so importing this module never
# raises IndexError in the container, and skip sys.path insertion when the
# experiments directory is not present.
# ---------------------------------------------------------------------------
_parents = pathlib.Path(__file__).resolve().parents
_REPO_ROOT = _parents[5] if len(_parents) > 5 else None
_NEWS_SRC = _REPO_ROOT / "experiments" / "news-impact-model" / "src" if _REPO_ROOT else None
if _NEWS_SRC and _NEWS_SRC.is_dir() and str(_NEWS_SRC) not in sys.path:
    sys.path.insert(0, str(_NEWS_SRC))

try:
    from news_impact_model.events import (
        NormalizedNewsEvent,
        load_vendor_news_events,
    )
except ModuleNotFoundError:
    # news-impact-model is not available in the worker image.  The names are
    # only used inside ingest_news_events(); callers that do not invoke news
    # ingestion will never touch them.
    NormalizedNewsEvent = None  # type: ignore[assignment,misc]
    load_vendor_news_events = None  # type: ignore[assignment]

NEWS_FEATURE_NAMES: tuple[str, ...] = (
    "headline_len",
    "body_len",
    "sentiment_proxy",
    "event_type_count",
    "symbol_count",
)

NS_PER_DAY = 86_400_000_000_000

# Naive sentiment word lists for a lightweight, dependency-free sentiment proxy.
_POSITIVE_WORDS = frozenset(
    {
        "beat", "beats", "surge", "surges", "jump", "jumps", "rise", "rises",
        "gain", "gains", "profit", "profits", "raise", "raises", "upgrade",
        "outperform", "strong", "growth", "grow", "win", "wins", "approve",
        "approved", "launch", "unveil", "partner", "partnership", "record",
        "high", "boost", "boosts", "rally", "soar", "soars", "breakthrough",
    },
)
_NEGATIVE_WORDS = frozenset(
    {
        "miss", "misses", "fall", "falls", "drop", "drops", "cut", "cuts",
        "lower", "lowers", "loss", "losses", "downgrade", "weak", "decline",
        "declines", "sue", "sued", "sues", "lawsuit", "settlement", "probe",
        "investigation", "hack", "breach", "ban", "sanction", "recall",
        "halt", "delay", "fire", "fraud", "default", "bankrupt", "warning",
    },
)


def _sentiment_proxy(text: str) -> float:
    """Naive sentiment in ``[-1, 1]`` from positive/negative word counts."""
    words = text.lower().split()
    if not words:
        return 0.0
    pos = sum(1 for w in words if w.strip(".,!?;:\"'()[]") in _POSITIVE_WORDS)
    neg = sum(1 for w in words if w.strip(".,!?;:\"'()[]") in _NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 6)


def news_feature_schema_hash() -> str:
    """SHA-256 over the sorted, colon-joined news feature names."""
    payload = ":".join(sorted(NEWS_FEATURE_NAMES))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def news_label_schema_hash(horizon_days: int) -> str:
    """SHA-256 over the news label description."""
    payload = f"news_subsequent_event_within_{horizon_days}d"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compute_news_features_and_labels(
    events: list[NormalizedNewsEvent],
    *,
    label_horizon_days: int,
) -> list[dict[str, Any]]:
    """Compute text-derived features + forward-event labels from news events.

    Each event becomes a row keyed by its ``available_at_ns`` (the
    vendor-availability time, which is the PIT decision time).  Features use
    only the event's own text (no look-ahead).  The label is ``1.0`` if a
    subsequent event for any of the same symbols arrives within
    ``label_horizon_days`` of this event's availability time.
    """
    if not events:
        return []

    horizon_ns = label_horizon_days * NS_PER_DAY
    sorted_events = sorted(events, key=lambda e: e.available_at_ns)

    # Pre-compute per-symbol event times for label lookup.
    symbol_times: dict[str, list[int]] = {}
    for ev in sorted_events:
        for sym in ev.symbols:
            symbol_times.setdefault(sym, []).append(ev.available_at_ns)

    # Running set of event types seen so far (for event_type_count).
    seen_types: set[str] = set()
    rows: list[dict[str, Any]] = []
    for ev in sorted_events:
        text = ev.text
        headline_len = float(len(ev.headline))
        body_len = float(len(ev.body))
        sentiment = _sentiment_proxy(text)
        seen_types.add(ev.event_type)
        event_type_count = float(len(seen_types))
        symbol_count = float(len(ev.symbols))

        # Label: is there a subsequent event for any of the same symbols
        # within the horizon window?
        label = 0.0
        window_end = ev.available_at_ns + horizon_ns
        for sym in ev.symbols:
            times = symbol_times.get(sym, [])
            for t in times:
                if ev.available_at_ns < t <= window_end:
                    label = 1.0
                    break
            if label == 1.0:
                break

        rows.append(
            {
                "decision_time": ev.available_at_ns,
                "__symbol": ev.symbols[0] if ev.symbols else "NEWS",
                "headline_len": headline_len,
                "body_len": body_len,
                "sentiment_proxy": sentiment,
                "event_type_count": event_type_count,
                "symbol_count": symbol_count,
                "label": label,
            },
        )
    return rows


def _write_news_parquet(
    data_rows: list[dict[str, Any]],
    out_path: pathlib.Path,
) -> int:
    """Write the news dataset (features + label) to a parquet file."""
    import polars as pl

    if not data_rows:
        schema = {
            "decision_time": pl.Int64,
            "symbol": pl.Utf8,
            **{name: pl.Float64 for name in NEWS_FEATURE_NAMES},
            "label": pl.Float64,
        }
        pl.DataFrame(schema=schema).write_parquet(str(out_path))
        return 0

    columns: dict[str, list[Any]] = {
        "decision_time": [int(r["decision_time"]) for r in data_rows],
        "symbol": [str(r["__symbol"]) for r in data_rows],
    }
    for name in NEWS_FEATURE_NAMES:
        columns[name] = [float(r[name]) for r in data_rows]
    columns["label"] = [float(r["label"]) for r in data_rows]

    df = pl.DataFrame(columns).sort("decision_time")
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(str(out_path))
    return df.height


def ingest_news_events(
    events_path: pathlib.Path,
    *,
    output_dir: pathlib.Path,
    dataset_id: str,
    source_type: str = "vendor",
    label_horizon_days: int = 1,
    n_folds: int = 3,
) -> IngestionResult:
    """Ingest news events into a leakage-safe dataset.

    Parameters
    ----------
    events_path
        Path to a vendor news export (JSONL, JSON, or CSV) in the format
        consumed by :func:`load_vendor_news_events`.
    output_dir
        Directory to write the dataset parquet + manifest + receipt + quality
        report.  Created if it does not exist.
    dataset_id
        Unique dataset identifier (non-empty).
    source_type
        Vendor source type passed to the news normalizer (default ``"vendor"``).
    label_horizon_days
        Forward-event label horizon in days (default 1).
    n_folds
        Number of purged-k-fold validation windows (default 3).

    Returns
    -------
    IngestionResult
        Paths to all emitted artifacts plus the manifest and quality report.
    """
    events_path = pathlib.Path(events_path)
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    events = load_vendor_news_events(events_path, source_type=source_type)
    if not events:
        raise ValueError(f"no news events loaded from {events_path}")

    data_rows = _compute_news_features_and_labels(
        events,
        label_horizon_days=label_horizon_days,
    )
    if not data_rows:
        raise ValueError(
            "no usable rows after news feature/label computation",
        )

    # --- build feature rows + universe ----------------------------------
    symbols = sorted({r["__symbol"] for r in data_rows})
    universe = tuple(
        UniverseEntry(symbol=s, listed_until=None, renamed_from=None)
        for s in symbols
    )

    horizon_ns = label_horizon_days * NS_PER_DAY
    feature_rows: list[FeatureRow] = []
    for r in data_rows:
        dt = int(r["decision_time"])
        features = tuple(
            FeatureValue(name=name, value=float(r[name]), observed_at=dt)
            for name in NEWS_FEATURE_NAMES
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

    f_hash = news_feature_schema_hash()
    l_hash = news_label_schema_hash(label_horizon_days)

    builder = FeatureLakeBuilder(
        dataset_id=dataset_id,
        universe=universe,
        rows=tuple(feature_rows),
        feature_schema_hash=f_hash,
        label_schema_hash=l_hash,
        max_label_horizon_ns=horizon_ns,
        n_folds=n_folds,
        source_vintage_refs=[
            f"news_events_path:{events_path.resolve()}",
            f"source_type:{source_type}",
        ],
    )
    manifest = builder.build_manifest()
    availability = FeatureAvailabilityReport.from_rows(
        tuple(feature_rows),
        NEWS_FEATURE_NAMES,
    )

    # --- export parquet + manifest + receipt -----------------------------
    parquet_path = output_dir / f"{dataset_id}.parquet"
    manifest_path = output_dir / f"{dataset_id}.manifest.json"

    _write_news_parquet(data_rows, parquet_path)

    # --- compute quality report then embed its hash in the manifest ------
    quality_report = compute_quality_report(
        parquet_path,
        manifest,
        feature_names=NEWS_FEATURE_NAMES,
    )
    quality_path = output_dir / f"{dataset_id}.quality.json"
    quality_report.write(quality_path)

    manifest = manifest.model_copy(
        update={"quality_report_hash": quality_report.quality_hash()},
    )

    # --- write manifest + receipt ----------------------------------------
    import json

    body = json.loads(manifest.to_json())
    body["availability"] = json.loads(availability.to_json())
    body["feature_names"] = list(NEWS_FEATURE_NAMES)
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
    "NEWS_FEATURE_NAMES",
    "ingest_news_events",
    "news_feature_schema_hash",
    "news_label_schema_hash",
]
