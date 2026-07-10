"""
Tests for Phase 4 — benchmark harness, attribution report, comparison report.

Tests verify:
- BenchmarkConfig is a frozen dataclass with the right fields.
- BenchmarkResult tracks success/failure correctly.
- BenchmarkHarness runs multiple configs and collects results.
- BenchmarkHarness gracefully handles failures (one config failing
  doesn't stop the others).
- BenchmarkHarness writes result summaries + combined report.
- AttributionReport groups feature importance by event type, source,
  sentiment provider, year, and horizon.
- AttributionReport handles models with mismatched feature names.
- AttributionReport writes JSON + produces human-readable summary.
- ComparisonReport ranks results by deflated Sharpe and PBO.
- ComparisonReport groups by source and sentiment engine.
- ComparisonReport writes JSON + produces human-readable summary.
"""

from __future__ import annotations

import json
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# --------------------------------------------------------------------------- #
# BenchmarkConfig / BenchmarkResult tests                                     #
# --------------------------------------------------------------------------- #


def test_benchmark_config_is_frozen() -> None:
    """BenchmarkConfig is a frozen dataclass."""
    from quant_foundry.modules.benchmark.harness import BenchmarkConfig

    config = BenchmarkConfig(
        name="test",
        universe="universe:sp500:1.0.0",
        source="source:newsapi:1.0.0",
        sentiment="sentiment:naive-wordlist:1.0.0",
        features=["feature:per-event-type:1.0.0"],
        label="label:abnormal-return:1.0.0",
        price_join="price_join:alpaca-bars:1.0.0",
        start_ns=0,
        end_ns=1,
    )
    with pytest.raises(AttributeError):
        config.name = "other"  # type: ignore[misc]


def test_benchmark_result_success_property() -> None:
    """BenchmarkResult.succeeded is True when dossier is set and no error."""
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    config = BenchmarkConfig(
        name="test",
        universe="u",
        source="s",
        sentiment="se",
        features=["f"],
        label="l",
        price_join="p",
        start_ns=0,
        end_ns=1,
    )

    # Failed result
    r_fail = BenchmarkResult(config=config, dataset_id="test", error="something failed")
    assert not r_fail.succeeded
    assert r_fail.deflated_sharpe is None

    # Successful result (mock dossier)
    mock_dossier = MagicMock()
    mock_dossier.deflated_sharpe = 1.5
    mock_dossier.pbo = 0.1
    mock_dossier.to_json.return_value = '{"model_id": "test"}'
    r_ok = BenchmarkResult(
        config=config,
        dataset_id="test",
        dossier=mock_dossier,
    )
    assert r_ok.succeeded
    assert r_ok.deflated_sharpe == 1.5
    assert r_ok.pbo == 0.1


def test_benchmark_result_to_dict() -> None:
    """BenchmarkResult.to_dict produces a JSON-compatible dict."""
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    config = BenchmarkConfig(
        name="test",
        universe="u",
        source="s",
        sentiment="se",
        features=["f"],
        label="l",
        price_join="p",
        start_ns=0,
        end_ns=1,
    )
    result = BenchmarkResult(config=config, dataset_id="test", error="fail")
    d = result.to_dict()
    assert d["name"] == "test"
    assert d["succeeded"] is False
    assert d["error"] == "fail"
    # Must be JSON-serializable
    json.dumps(d)


# --------------------------------------------------------------------------- #
# BenchmarkHarness tests                                                      #
# --------------------------------------------------------------------------- #


def test_benchmark_harness_handles_failures(tmp_path: pathlib.Path) -> None:
    """BenchmarkHarness records errors and continues to the next config."""
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkHarness,
    )

    # Create configs that will fail (nonexistent modules)
    config1 = BenchmarkConfig(
        name="fail-1",
        universe="universe:nonexistent:1.0.0",
        source="source:nonexistent:1.0.0",
        sentiment="sentiment:nonexistent:1.0.0",
        features=["feature:nonexistent:1.0.0"],
        label="label:nonexistent:1.0.0",
        price_join="price_join:nonexistent:1.0.0",
        start_ns=0,
        end_ns=1,
    )
    config2 = BenchmarkConfig(
        name="fail-2",
        universe="universe:also-nonexistent:1.0.0",
        source="source:also-nonexistent:1.0.0",
        sentiment="sentiment:also-nonexistent:1.0.0",
        features=["feature:also-nonexistent:1.0.0"],
        label="label:also-nonexistent:1.0.0",
        price_join="price_join:also-nonexistent:1.0.0",
        start_ns=0,
        end_ns=1,
    )

    harness = BenchmarkHarness(
        configs=[config1, config2],
        output_dir=tmp_path,
        deadline_seconds=10,
    )

    results = harness.run()
    assert len(results) == 2
    assert not results[0].succeeded
    assert results[0].error is not None
    assert not results[1].succeeded
    assert results[1].error is not None

    # Result summaries should be written
    assert (tmp_path / "fail-1" / "benchmark_result.json").exists()
    assert (tmp_path / "fail-2" / "benchmark_result.json").exists()


def test_benchmark_harness_writes_combined_report(tmp_path: pathlib.Path) -> None:
    """BenchmarkHarness.write_report produces a combined JSON report."""
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkHarness,
    )

    config = BenchmarkConfig(
        name="test-config",
        universe="universe:nonexistent:1.0.0",
        source="source:nonexistent:1.0.0",
        sentiment="sentiment:nonexistent:1.0.0",
        features=["feature:nonexistent:1.0.0"],
        label="label:nonexistent:1.0.0",
        price_join="price_join:nonexistent:1.0.0",
        start_ns=0,
        end_ns=1,
    )

    harness = BenchmarkHarness(
        configs=[config],
        output_dir=tmp_path,
        deadline_seconds=10,
    )
    results = harness.run()
    report_path = harness.write_report(results)

    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert "configs" in report
    assert "summary" in report
    assert len(report["configs"]) == 1
    assert report["configs"][0]["name"] == "test-config"


# --------------------------------------------------------------------------- #
# AttributionReport tests                                                     #
# --------------------------------------------------------------------------- #


def test_attribution_report_basic() -> None:
    """AttributionReport groups feature importance correctly."""
    from quant_foundry.modules.benchmark.attribution import AttributionReport

    feature_names = [
        "sent_earnings",
        "sent_regulatory",
        "sent_macro",
        "sent_social",
        "sent_mean",
        "sent_count",
        "year_2023",
        "year_2024",
        "ar_1d",
        "ar_5d",
        "ar_21d",
        "ar_63d",
    ]
    importances = [10.0, 5.0, 3.0, 8.0, 7.0, 2.0, 4.0, 6.0, 1.0, 9.0, 5.0, 3.0]

    report = AttributionReport.from_model(
        feature_importances=importances,
        feature_names=feature_names,
    )

    # Event type attribution
    et_attr = report.event_type_attribution()
    assert et_attr["earnings"] == 10.0
    assert et_attr["regulatory"] == 5.0
    assert et_attr["macro"] == 3.0

    # Source attribution (news = earnings + regulatory + macro, social = social)
    src_attr = report.source_attribution()
    assert src_attr["news"] == 10.0 + 5.0 + 3.0
    assert src_attr["social"] == 8.0

    # Year attribution
    year_attr = report.year_attribution()
    assert year_attr["2023"] == 4.0
    assert year_attr["2024"] == 6.0

    # Horizon attribution
    h_attr = report.horizon_attribution()
    assert h_attr["ar_1d"] == 1.0
    assert h_attr["ar_5d"] == 9.0

    # Total importance
    assert report.total_importance == sum(importances)

    # Top features
    assert len(report.top_features) > 0
    assert report.top_features[0]["feature"] == "sent_earnings"
    assert report.top_features[0]["importance"] == 10.0


def test_attribution_report_length_mismatch() -> None:
    """AttributionReport raises ValueError on length mismatch."""
    from quant_foundry.modules.benchmark.attribution import AttributionReport

    with pytest.raises(ValueError, match="length mismatch"):
        AttributionReport.from_model(
            feature_importances=[1.0, 2.0],
            feature_names=["a", "b", "c"],
        )


def test_attribution_report_write_json(tmp_path: pathlib.Path) -> None:
    """AttributionReport.write produces a valid JSON file."""
    from quant_foundry.modules.benchmark.attribution import AttributionReport

    feature_names = ["sent_earnings", "sent_regulatory", "year_2023", "ar_5d"]
    importances = [10.0, 5.0, 3.0, 8.0]

    report = AttributionReport.from_model(
        feature_importances=importances,
        feature_names=feature_names,
    )

    out_path = tmp_path / "attribution.json"
    result_path = report.write(out_path)
    assert result_path == out_path
    assert out_path.exists()

    body = json.loads(out_path.read_text())
    assert "event_type" in body
    assert "source" in body
    assert "year" in body
    assert "horizon" in body
    assert "top_features" in body
    assert body["event_type"]["earnings"] == 10.0


def test_attribution_report_summary_text() -> None:
    """AttributionReport.summary_text produces a human-readable string."""
    from quant_foundry.modules.benchmark.attribution import AttributionReport

    feature_names = ["sent_earnings", "sent_regulatory", "year_2023", "ar_5d"]
    importances = [10.0, 5.0, 3.0, 8.0]

    report = AttributionReport.from_model(
        feature_importances=importances,
        feature_names=feature_names,
    )

    text = report.summary_text()
    assert "ATTRIBUTION REPORT" in text
    assert "Event Type Attribution" in text
    assert "Source Attribution" in text
    assert "Year Attribution" in text
    assert "Horizon Attribution" in text
    assert "Top 10 Features" in text


def test_attribution_report_from_parquet(tmp_path: pathlib.Path) -> None:
    """AttributionReport.from_parquet reads features from a parquet file."""
    pytest.importorskip("polars")
    import polars as pl
    from quant_foundry.modules.benchmark.attribution import AttributionReport

    # Create a synthetic parquet
    df = pl.DataFrame(
        {
            "decision_time": [1, 2, 3],
            "symbol": ["AAPL", "AAPL", "MSFT"],
            "sent_earnings": [0.5, -0.3, 0.8],
            "sent_regulatory": [0.0, 0.2, -0.1],
            "year_2023": [1.0, 1.0, 1.0],
            "label": [0.01, -0.02, 0.03],
        }
    )
    parquet_path = tmp_path / "test_dataset.parquet"
    df.write_parquet(str(parquet_path))

    # Mock model with feature_importances_
    mock_model = MagicMock()
    mock_model.feature_importances_ = [10.0, 5.0, 3.0]
    mock_model.feature_name.return_value = ["sent_earnings", "sent_regulatory", "year_2023"]

    report = AttributionReport.from_parquet(
        parquet_path=parquet_path,
        model=mock_model,
    )

    assert "sent_earnings" in report.feature_names
    assert "label" not in report.feature_names
    assert "decision_time" not in report.feature_names
    assert report.event_type_attribution()["earnings"] == 10.0


# --------------------------------------------------------------------------- #
# ComparisonReport tests                                                      #
# --------------------------------------------------------------------------- #


def test_comparison_report_ranks_by_sharpe() -> None:
    """ComparisonReport ranks results by deflated Sharpe (descending)."""
    from quant_foundry.modules.benchmark.comparison import ComparisonReport
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    config = BenchmarkConfig(
        name="test",
        universe="u",
        source="source:newsapi:1.0.0",
        sentiment="sentiment:finbert:1.0.0",
        features=["f"],
        label="l",
        price_join="p",
        start_ns=0,
        end_ns=1,
    )

    def make_result(name: str, dsr: float, pbo: float) -> BenchmarkResult:
        c = BenchmarkConfig(**{**config.__dict__, "name": name})
        mock_dossier = MagicMock()
        mock_dossier.deflated_sharpe = dsr
        mock_dossier.pbo = pbo
        mock_dossier.metadata = {"n_rows": "100", "n_features": "10"}
        mock_dossier.to_json.return_value = '{"model_id": "test"}'
        return BenchmarkResult(config=c, dataset_id=name, dossier=mock_dossier)

    results = [
        make_result("low", 0.5, 0.3),
        make_result("high", 2.0, 0.1),
        make_result("mid", 1.0, 0.2),
    ]

    comparison = ComparisonReport.from_results(results)
    ranked = comparison.ranked_by_sharpe
    assert ranked[0]["name"] == "high"
    assert ranked[1]["name"] == "mid"
    assert ranked[2]["name"] == "low"


def test_comparison_report_ranks_by_pbo() -> None:
    """ComparisonReport ranks results by PBO (ascending — lower is better)."""
    from quant_foundry.modules.benchmark.comparison import ComparisonReport
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    config = BenchmarkConfig(
        name="test",
        universe="u",
        source="source:newsapi:1.0.0",
        sentiment="sentiment:finbert:1.0.0",
        features=["f"],
        label="l",
        price_join="p",
        start_ns=0,
        end_ns=1,
    )

    def make_result(name: str, dsr: float, pbo: float) -> BenchmarkResult:
        c = BenchmarkConfig(**{**config.__dict__, "name": name})
        mock_dossier = MagicMock()
        mock_dossier.deflated_sharpe = dsr
        mock_dossier.pbo = pbo
        mock_dossier.metadata = {}
        mock_dossier.to_json.return_value = "{}"
        return BenchmarkResult(config=c, dataset_id=name, dossier=mock_dossier)

    results = [
        make_result("a", 1.0, 0.5),
        make_result("b", 1.0, 0.1),
        make_result("c", 1.0, 0.3),
    ]

    comparison = ComparisonReport.from_results(results)
    ranked = comparison.ranked_by_pbo
    assert ranked[0]["name"] == "b"  # lowest PBO
    assert ranked[1]["name"] == "c"
    assert ranked[2]["name"] == "a"


def test_comparison_report_best_by_source() -> None:
    """ComparisonReport groups best results by source."""
    from quant_foundry.modules.benchmark.comparison import ComparisonReport
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    def make_result(name: str, source: str, dsr: float) -> BenchmarkResult:
        c = BenchmarkConfig(
            name=name,
            universe="u",
            source=source,
            sentiment="sentiment:finbert:1.0.0",
            features=["f"],
            label="l",
            price_join="p",
            start_ns=0,
            end_ns=1,
        )
        mock_dossier = MagicMock()
        mock_dossier.deflated_sharpe = dsr
        mock_dossier.pbo = 0.1
        mock_dossier.metadata = {}
        mock_dossier.to_json.return_value = "{}"
        return BenchmarkResult(config=c, dataset_id=name, dossier=mock_dossier)

    results = [
        make_result("news-1", "source:newsapi:1.0.0", 1.0),
        make_result("news-2", "source:newsapi:1.0.0", 2.0),
        make_result("social-1", "source:stocktwits:1.0.0", 1.5),
    ]

    comparison = ComparisonReport.from_results(results)
    assert "newsapi" in comparison.best_by_source
    assert comparison.best_by_source["newsapi"]["name"] == "news-2"
    assert "stocktwits" in comparison.best_by_source
    assert comparison.best_by_source["stocktwits"]["name"] == "social-1"


def test_comparison_report_best_by_sentiment() -> None:
    """ComparisonReport groups best results by sentiment engine."""
    from quant_foundry.modules.benchmark.comparison import ComparisonReport
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    def make_result(name: str, sentiment: str, dsr: float) -> BenchmarkResult:
        c = BenchmarkConfig(
            name=name,
            universe="u",
            source="source:newsapi:1.0.0",
            sentiment=sentiment,
            features=["f"],
            label="l",
            price_join="p",
            start_ns=0,
            end_ns=1,
        )
        mock_dossier = MagicMock()
        mock_dossier.deflated_sharpe = dsr
        mock_dossier.pbo = 0.1
        mock_dossier.metadata = {}
        mock_dossier.to_json.return_value = "{}"
        return BenchmarkResult(config=c, dataset_id=name, dossier=mock_dossier)

    results = [
        make_result("finbert-1", "sentiment:finbert:1.0.0", 1.5),
        make_result("naive-1", "sentiment:naive-wordlist:1.0.0", 0.8),
        make_result("finbert-2", "sentiment:finbert:1.0.0", 2.0),
    ]

    comparison = ComparisonReport.from_results(results)
    assert "finbert" in comparison.best_by_sentiment
    assert comparison.best_by_sentiment["finbert"]["name"] == "finbert-2"
    assert "naive-wordlist" in comparison.best_by_sentiment


def test_comparison_report_write_json(tmp_path: pathlib.Path) -> None:
    """ComparisonReport.write produces a valid JSON file."""
    from quant_foundry.modules.benchmark.comparison import ComparisonReport
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    config = BenchmarkConfig(
        name="test",
        universe="u",
        source="source:newsapi:1.0.0",
        sentiment="sentiment:finbert:1.0.0",
        features=["f"],
        label="l",
        price_join="p",
        start_ns=0,
        end_ns=1,
    )
    mock_dossier = MagicMock()
    mock_dossier.deflated_sharpe = 1.5
    mock_dossier.pbo = 0.1
    mock_dossier.metadata = {"n_rows": "100", "n_features": "10"}
    mock_dossier.to_json.return_value = '{"model_id": "test"}'
    result = BenchmarkResult(config=config, dataset_id="test", dossier=mock_dossier)

    comparison = ComparisonReport.from_results([result])
    out_path = tmp_path / "comparison.json"
    result_path = comparison.write(out_path)
    assert result_path == out_path
    assert out_path.exists()

    body = json.loads(out_path.read_text())
    assert "summary_table" in body
    assert "ranked_by_sharpe" in body
    assert "best_by_source" in body
    assert "best_by_sentiment" in body


def test_comparison_report_summary_text() -> None:
    """ComparisonReport.summary_text produces a human-readable string."""
    from quant_foundry.modules.benchmark.comparison import ComparisonReport
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    config = BenchmarkConfig(
        name="test",
        universe="u",
        source="source:newsapi:1.0.0",
        sentiment="sentiment:finbert:1.0.0",
        features=["f"],
        label="l",
        price_join="p",
        start_ns=0,
        end_ns=1,
    )
    mock_dossier = MagicMock()
    mock_dossier.deflated_sharpe = 1.5
    mock_dossier.pbo = 0.1
    mock_dossier.metadata = {}
    mock_dossier.to_json.return_value = "{}"
    result = BenchmarkResult(config=config, dataset_id="test", dossier=mock_dossier)

    comparison = ComparisonReport.from_results([result])
    text = comparison.summary_text()
    assert "BENCHMARK COMPARISON REPORT" in text
    assert "Ranked by Deflated Sharpe" in text
    assert "Best by Source" in text
    assert "Best by Sentiment Engine" in text


# --------------------------------------------------------------------------- #
# Module-level heavy deps check                                               #
# --------------------------------------------------------------------------- #


def test_benchmark_modules_no_heavy_deps() -> None:
    """Benchmark modules must not import heavy deps at module level."""
    import quant_foundry.modules.benchmark.attribution as attr
    import quant_foundry.modules.benchmark.comparison as comp
    import quant_foundry.modules.benchmark.harness as harness

    for mod in (attr, comp, harness):
        assert not hasattr(mod, "np"), f"{mod.__name__}: numpy at module level"
        assert not hasattr(mod, "pl"), f"{mod.__name__}: polars at module level"
        assert not hasattr(mod, "lightgbm"), f"{mod.__name__}: lightgbm at module level"
