"""
Tests for quant_foundry.data_ingestion — real dataset expansion + quality reports.

Tests verify:
- :func:`compute_quality_report` works on the existing synthetic dataset.
- :func:`ingest_equity_bars` produces a valid dataset + manifest + quality report.
- :class:`DatasetQualityReport` has the correct field types and structure.
- :func:`get_ingester` returns the right function and rejects unknown vendors.

Tests requiring numpy/polars use ``pytest.importorskip`` so they are skipped
in environments without those deps, following the convention in
``test_dataset_manifest_builder.py``.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup — scripts/ is not a package, so add it to sys.path for the
# synthetic bar generator.
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Importability (no heavy deps required at import time)
# ---------------------------------------------------------------------------


def test_data_ingestion_importable() -> None:
    """The data_ingestion package must be importable without numpy/polars."""
    from quant_foundry.data_ingestion import (
        VENDOR_INGESTERS,
        DatasetQualityReport,
        IngestionResult,
        compute_quality_report,
        get_ingester,
        ingest_equity_bars,
        ingest_macro_indicators,
        ingest_news_events,
    )

    assert callable(compute_quality_report)
    assert callable(ingest_equity_bars)
    assert callable(ingest_news_events)
    assert callable(ingest_macro_indicators)
    assert callable(get_ingester)
    assert isinstance(VENDOR_INGESTERS, dict)
    assert IngestionResult is not None
    assert DatasetQualityReport is not None


def test_data_ingestion_no_module_level_heavy_deps() -> None:
    """numpy and polars must NOT be imported at module level (lazy imports)."""
    import quant_foundry.data_ingestion.equities as eq
    import quant_foundry.data_ingestion.macro as mc
    import quant_foundry.data_ingestion.news as ne
    import quant_foundry.data_ingestion.quality_report as qr

    for mod in (qr, eq, ne, mc):
        assert not hasattr(mod, "np"), f"{mod.__name__}: numpy at module level"
        assert not hasattr(mod, "pl"), f"{mod.__name__}: polars at module level"
        assert not hasattr(mod, "numpy"), f"{mod.__name__}: numpy at module level"
        assert not hasattr(mod, "polars"), f"{mod.__name__}: polars at module level"


# ---------------------------------------------------------------------------
# Vendor registry
# ---------------------------------------------------------------------------


def test_get_ingester_returns_correct_function() -> None:
    """get_ingester must return the ingestion function for known vendors."""
    from quant_foundry.data_ingestion import (
        VENDOR_INGESTERS,
        get_ingester,
        ingest_equity_bars,
        ingest_macro_indicators,
        ingest_news_events,
    )

    assert get_ingester("equity_bars") is ingest_equity_bars
    assert get_ingester("news_events") is ingest_news_events
    assert get_ingester("macro_indicators") is ingest_macro_indicators

    # Registry must contain all three.
    assert set(VENDOR_INGESTERS.keys()) == {
        "equity_bars",
        "news_events",
        "macro_indicators",
    }


def test_get_ingester_rejects_unknown_vendor() -> None:
    """get_ingester must raise ValueError for unknown vendors."""
    from quant_foundry.data_ingestion import get_ingester

    with pytest.raises(ValueError, match="unknown vendor"):
        get_ingester("does_not_exist")


# ---------------------------------------------------------------------------
# DatasetQualityReport model structure
# ---------------------------------------------------------------------------


def test_quality_report_model_config() -> None:
    """DatasetQualityReport must be frozen and forbid extra fields."""
    from quant_foundry.data_ingestion import DatasetQualityReport

    assert DatasetQualityReport.model_config.get("frozen") is True
    assert DatasetQualityReport.model_config.get("extra") == "forbid"


def test_quality_report_field_types() -> None:
    """DatasetQualityReport must have the correct field types/defaults."""
    from quant_foundry.data_ingestion import DatasetQualityReport

    fields = DatasetQualityReport.model_fields

    # Required fields that must be present.
    required = {
        "schema_version",
        "dataset_id",
        "generated_at_ns",
        "total_rows",
        "total_symbols",
        "time_span_start_ns",
        "time_span_end_ns",
        "feature_names",
        "feature_coverage_pct",
        "feature_missing_count",
        "label_balance",
        "label_missing_count",
        "fold_count",
        "fold_train_counts",
        "fold_val_counts",
        "pit_proof_verified",
        "embargo_sufficient",
        "no_forward_joins",
        "mean_feature_values",
        "std_feature_values",
    }
    assert required.issubset(set(fields.keys())), (
        f"missing fields: {required - set(fields.keys())}"
    )

    # schema_version default is 1.
    assert fields["schema_version"].default == 1


def test_quality_report_is_frozen() -> None:
    """A frozen model must reject attribute mutation."""
    from quant_foundry.data_ingestion import DatasetQualityReport

    report = DatasetQualityReport(
        schema_version=1,
        dataset_id="test",
        generated_at_ns=0,
        total_rows=0,
        total_symbols=0,
        time_span_start_ns=0,
        time_span_end_ns=0,
        feature_names=(),
        feature_coverage_pct={},
        feature_missing_count={},
        label_balance={},
        label_missing_count=0,
        fold_count=0,
        fold_train_counts=(),
        fold_val_counts=(),
        pit_proof_verified=True,
        embargo_sufficient=True,
        no_forward_joins=True,
        mean_feature_values={},
        std_feature_values={},
    )
    with pytest.raises((AttributeError, TypeError, ValueError)):
        report.total_rows = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests requiring numpy + polars
# ---------------------------------------------------------------------------

_NUMPY = pytest.importorskip("numpy")
_POLARS = pytest.importorskip("polars")

_SYNTHETIC_PARQUET = _REPO_ROOT / "data" / "datasets" / "backtest_synthetic" / (
    "synthetic_s5_d500_h5d_seed42.parquet"
)
_SYNTHETIC_MANIFEST = _REPO_ROOT / "data" / "datasets" / "backtest_synthetic" / (
    "synthetic_s5_d500_h5d_seed42.manifest.json"
)


def _load_synth_manifest():
    """Load the existing synthetic dataset's FeatureLakeManifest."""
    from quant_foundry.dataset_manifest import FeatureLakeManifest

    body = json.loads(_SYNTHETIC_MANIFEST.read_text(encoding="utf-8"))
    # The manifest JSON includes extra keys (availability, feature_names,
    # manifest_hash) that are not part of FeatureLakeManifest; pop them.
    for key in ("availability", "feature_names", "manifest_hash"):
        body.pop(key, None)
    return FeatureLakeManifest.model_validate(body)


def test_compute_quality_report_on_synthetic_dataset() -> None:
    """compute_quality_report must work on the existing synthetic dataset."""
    from quant_foundry.data_ingestion import compute_quality_report

    if not _SYNTHETIC_PARQUET.exists() or not _SYNTHETIC_MANIFEST.exists():
        pytest.skip("synthetic dataset not found")

    manifest = _load_synth_manifest()
    report = compute_quality_report(
        _SYNTHETIC_PARQUET,
        manifest,
        feature_names=(
            "ret_1d",
            "ret_5d",
            "vol_20d",
            "mom_10d",
            "vol_ratio",
        ),
    )

    assert report.dataset_id == manifest.dataset_id
    assert report.total_rows == manifest.row_count
    assert report.total_rows > 0
    assert report.time_span_start_ns <= report.time_span_end_ns
    assert report.fold_count == len(manifest.folds.folds)
    assert len(report.fold_train_counts) == report.fold_count
    assert len(report.fold_val_counts) == report.fold_count

    # Feature coverage should be 100% for the synthetic dataset.
    for name in ("ret_1d", "ret_5d", "vol_20d", "mom_10d", "vol_ratio"):
        assert report.feature_coverage_pct[name] == 100.0, (
            f"{name} coverage should be 100%"
        )
        assert report.feature_missing_count[name] == 0
        assert name in report.mean_feature_values
        assert name in report.std_feature_values

    # Leakage checks must all be True for a leakage-safe dataset.
    assert report.pit_proof_verified is True
    assert report.embargo_sufficient is True
    assert report.no_forward_joins is True

    # Label balance must sum to ~1.0 and have the expected keys.
    assert set(report.label_balance.keys()) == {"0.0", "1.0"}
    total = sum(report.label_balance.values())
    assert abs(total - 1.0) < 1e-6, f"label balance sums to {total}, expected 1.0"


def test_quality_report_serialization_roundtrip() -> None:
    """A quality report must round-trip through JSON."""
    from quant_foundry.data_ingestion import DatasetQualityReport

    if not _SYNTHETIC_PARQUET.exists() or not _SYNTHETIC_MANIFEST.exists():
        pytest.skip("synthetic dataset not found")

    from quant_foundry.data_ingestion import compute_quality_report

    manifest = _load_synth_manifest()
    report = compute_quality_report(
        _SYNTHETIC_PARQUET,
        manifest,
        feature_names=("ret_1d", "ret_5d", "vol_20d", "mom_10d", "vol_ratio"),
    )

    js = report.to_json()
    body = json.loads(js)
    restored = DatasetQualityReport.model_validate(body)
    assert restored == report


# ---------------------------------------------------------------------------
# Equity ingestion
# ---------------------------------------------------------------------------


def _write_synthetic_ohlcv_parquet(
    tmp_path: pathlib.Path,
    *,
    n_symbols: int = 3,
    n_days: int = 300,
    seed: int = 42,
) -> pathlib.Path:
    """Generate synthetic OHLCV bars and write them to a single parquet file."""
    import build_synthetic_dataset as bsd
    import polars as pl

    frames: list[pl.DataFrame] = []
    for i in range(n_symbols):
        sym = bsd._symbol_for_index(i)
        bars = bsd.generate_synthetic_bars(sym, n_days=n_days, seed=seed + i * 1000)
        df = pl.DataFrame(
            {
                "symbol": [sym] * len(bars),
                "ts_event": [b["ts_event"] for b in bars],
                "open": [b["open"] for b in bars],
                "high": [b["high"] for b in bars],
                "low": [b["low"] for b in bars],
                "close": [b["close"] for b in bars],
                "volume": [b["volume"] for b in bars],
            }
        )
        frames.append(df)

    combined = pl.concat(frames, how="vertical_relaxed")
    out_path = tmp_path / "synth_ohlcv.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.write_parquet(str(out_path))
    return out_path


def test_ingest_equity_bars_produces_valid_dataset(tmp_path: pathlib.Path) -> None:
    """ingest_equity_bars must produce a parquet + manifest + receipt + quality."""
    import polars as pl
    from quant_foundry.data_ingestion import ingest_equity_bars

    bars_path = _write_synthetic_ohlcv_parquet(tmp_path / "bars", n_symbols=3, n_days=300)
    output_dir = tmp_path / "out"

    result = ingest_equity_bars(
        bars_path,
        output_dir=output_dir,
        dataset_id="test_equity_ingest",
        label_horizon_days=5,
        n_folds=3,
    )

    # All paths must exist.
    assert result.parquet_path.exists()
    assert result.manifest_path.exists()
    assert result.receipt_path.exists()
    assert result.quality_path.exists()

    # Parquet must have the expected columns.
    df = pl.read_parquet(str(result.parquet_path))
    assert df.height > 0
    assert "decision_time" in df.columns
    assert "label" in df.columns
    for feat in ("ret_1d", "ret_5d", "vol_20d", "mom_10d", "vol_ratio"):
        assert feat in df.columns

    # Manifest must be PIT-proof.
    assert result.manifest.pit_proof_verified is True
    assert result.manifest.row_count == df.height

    # Quality report must be consistent.
    qr = result.quality_report
    assert qr.dataset_id == "test_equity_ingest"
    assert qr.total_rows == df.height
    assert qr.pit_proof_verified is True
    assert qr.embargo_sufficient is True
    assert qr.no_forward_joins is True
    assert qr.fold_count == len(result.manifest.folds.folds)

    # Quality JSON on disk must round-trip.
    from quant_foundry.data_ingestion import DatasetQualityReport

    body = json.loads(result.quality_path.read_text(encoding="utf-8"))
    restored = DatasetQualityReport.model_validate(body)
    assert restored == qr


def test_ingest_equity_bars_rejects_empty_bars(tmp_path: pathlib.Path) -> None:
    """ingest_equity_bars must raise on a parquet with no matching symbols."""
    import polars as pl
    from quant_foundry.data_ingestion import ingest_equity_bars

    # Write a parquet with a symbol column but no rows.
    df = pl.DataFrame(
        schema={
            "symbol": pl.Utf8,
            "ts_event": pl.Int64,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
        },
    )
    bars_path = tmp_path / "empty.parquet"
    df.write_parquet(str(bars_path))

    with pytest.raises((ValueError, Exception)):
        ingest_equity_bars(
            bars_path,
            output_dir=tmp_path / "out",
            dataset_id="test_empty",
            symbols=["NOPE"],
        )


# ---------------------------------------------------------------------------
# Macro ingestion
# ---------------------------------------------------------------------------


def test_ingest_macro_indicators_produces_valid_dataset(tmp_path: pathlib.Path) -> None:
    """ingest_macro_indicators must produce a dataset from a CSV."""
    import polars as pl
    from quant_foundry.data_ingestion import ingest_macro_indicators

    # Write a small macro CSV.
    csv_path = tmp_path / "macro.csv"
    lines = ["date,indicator,value"]
    # 40 monthly observations of two indicators.
    for i in range(40):
        date = f"2020-{(i % 12) + 1:02d}-01"
        lines.append(f"{date},fed_rate,{0.25 + i * 0.05}")
    for i in range(40):
        date = f"2020-{(i % 12) + 1:02d}-15"
        lines.append(f"{date},cpi,{100.0 + i * 0.3}")
    csv_path.write_text("\n".join(lines), encoding="utf-8")

    result = ingest_macro_indicators(
        csv_path,
        output_dir=tmp_path / "out",
        dataset_id="test_macro",
        n_folds=3,
    )

    assert result.parquet_path.exists()
    assert result.manifest_path.exists()
    assert result.receipt_path.exists()
    assert result.quality_path.exists()

    df = pl.read_parquet(str(result.parquet_path))
    assert df.height > 0
    assert "decision_time" in df.columns
    assert "label" in df.columns
    for feat in ("value", "value_diff_1", "value_pct_change_1"):
        assert feat in df.columns

    assert result.manifest.pit_proof_verified is True
    assert result.quality_report.total_rows == df.height
    assert result.quality_report.pit_proof_verified is True


# ---------------------------------------------------------------------------
# News ingestion
# ---------------------------------------------------------------------------


def test_ingest_news_events_produces_valid_dataset(tmp_path: pathlib.Path) -> None:
    """ingest_news_events must produce a dataset from a JSONL news export."""
    import polars as pl
    from quant_foundry.data_ingestion import ingest_news_events

    # Write a small JSONL news export.
    events_path = tmp_path / "news.jsonl"
    lines: list[str] = []
    base_ts = 1_642_090_560 * 1_000_000_000  # 2022-01-01 UTC
    day_ns = 86_400_000_000_000
    headlines = [
        ("AAPL beats earnings expectations, profit surges", "earnings"),
        ("MSFT faces lawsuit over antitrust probe", "litigation"),
        ("GOOG unveils new product launch", "product"),
        ("AAPL raises guidance, strong growth outlook", "guidance"),
        ("MSFT hack breach security incident reported", "security"),
        ("GOOG partnership agreement announced", "partnership"),
        ("AAPL cuts outlook, weak decline warning", "guidance"),
        ("MSFT upgrade outperform rating", "earnings"),
    ]
    for i, (headline, _etype) in enumerate(headlines):
        row = {
            "headline": headline,
            "body": f"Details about {headline.lower()}.",
            "source": "test",
            "published_at": base_ts + i * day_ns,
            "symbols": ["AAPL", "MSFT", "GOOG"][i % 3],
        }
        lines.append(json.dumps(row))
    events_path.write_text("\n".join(lines), encoding="utf-8")

    result = ingest_news_events(
        events_path,
        output_dir=tmp_path / "out",
        dataset_id="test_news",
        label_horizon_days=1,
        n_folds=3,
    )

    assert result.parquet_path.exists()
    assert result.manifest_path.exists()
    assert result.receipt_path.exists()
    assert result.quality_path.exists()

    df = pl.read_parquet(str(result.parquet_path))
    assert df.height > 0
    assert "decision_time" in df.columns
    assert "label" in df.columns
    for feat in ("headline_len", "body_len", "sentiment_proxy", "event_type_count", "symbol_count"):
        assert feat in df.columns

    assert result.manifest.pit_proof_verified is True
    assert result.quality_report.total_rows == df.height
    assert result.quality_report.pit_proof_verified is True
