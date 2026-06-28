"""
quant_foundry.modules.benchmark.harness — A/B benchmark harness for module combinations.

The :class:`BenchmarkHarness` takes multiple :class:`BenchmarkConfig`
entries (each defining a module combination + dataset parameters),
builds a dataset for each via :class:`DatasetComposer`, trains a model
on each using the existing :class:`RealLightGBMTrainer`, and collects
the :class:`ModelDossier` metrics (deflated Sharpe, PBO, training
metrics) for comparison.

This is the engine that answers the core research questions:
- Which sentiment engine (FinBERT vs LLM ensemble) produces the best
  abnormal-return prediction?
- Which source (news vs social) has higher signal-to-noise?
- How did media→price response change from 2018 to 2025?

Usage::

    harness = BenchmarkHarness(
        configs=[
            BenchmarkConfig(
                name="finbert-news-2023",
                universe="universe:sp500:1.0.0",
                source="source:newsapi:1.0.0",
                sentiment="sentiment:finbert:1.0.0",
                features=["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
                label="label:abnormal-return:1.0.0",
                price_join="price_join:alpaca-bars:1.0.0",
                start_ns=..., end_ns=...,
            ),
            BenchmarkConfig(
                name="llm-ensemble-social-2023",
                source="source:stocktwits:1.0.0",
                sentiment="sentiment:llm-ensemble-4:1.0.0",
                ...
            ),
        ],
        output_dir=Path("data/benchmarks"),
    )
    results = harness.run()
    # results[0].dossier.deflated_sharpe  → compare across configs
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

from quant_foundry.modules.composer import DatasetComposer


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for a single benchmark run.

    Each config defines a complete module combination + dataset
    parameters.  The harness builds a dataset and trains a model for
    each config, then collects the results for comparison.

    Args:
        name: Human-readable name for this benchmark (e.g.
            ``"finbert-news-2023"``).
        universe: Universe selector module ID.
        source: Source adapter module ID.
        sentiment: Sentiment engine module ID.
        features: List of feature computer module IDs.
        label: Label computer module ID.
        price_join: Price joiner module ID.
        start_ns: Dataset start time (nanoseconds).
        end_ns: Dataset end time (nanoseconds).
        n_folds: Number of walk-forward CV folds.
        config: Optional per-module config overrides.
        random_seed: Random seed for training (None = 0).
    """

    name: str
    universe: str
    source: str
    sentiment: str
    features: list[str]
    label: str
    price_join: str
    start_ns: int
    end_ns: int
    n_folds: int = 3
    config: dict[str, dict[str, Any]] = field(default_factory=dict)
    random_seed: int | None = None


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run.

    Contains the dataset artifacts, trained model dossier, and any
    errors encountered.  If the run failed, ``error`` is set and
    ``dossier`` is None.
    """

    config: BenchmarkConfig
    dataset_id: str
    parquet_path: pathlib.Path | None = None
    manifest_path: pathlib.Path | None = None
    dossier: Any = None  # ModelDossier
    artifact: Any = None  # ArtifactManifest
    error: str | None = None
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.dossier is not None and self.error is None

    @property
    def deflated_sharpe(self) -> float | None:
        if self.dossier is None:
            return None
        return self.dossier.deflated_sharpe

    @property
    def pbo(self) -> float | None:
        if self.dossier is None:
            return None
        return self.dossier.pbo

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict for reporting."""
        return {
            "name": self.config.name,
            "dataset_id": self.dataset_id,
            "succeeded": self.succeeded,
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 3),
            "deflated_sharpe": self.deflated_sharpe,
            "pbo": self.pbo,
            "parquet_path": str(self.parquet_path) if self.parquet_path else None,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "dossier": (
                json.loads(self.dossier.to_json()) if self.dossier else None
            ),
        }


@dataclass
class BenchmarkHarness:
    """Runs multiple benchmark configs and collects results.

    Args:
        configs: List of :class:`BenchmarkConfig` entries to run.
        output_dir: Base directory for dataset + model artifacts.
            Each config gets a subdirectory named after its ``name``.
        deadline_seconds: Per-config deadline for training (default 600s).
    """

    configs: list[BenchmarkConfig]
    output_dir: pathlib.Path
    deadline_seconds: int = 600

    def run(self) -> list[BenchmarkResult]:
        """Run all benchmark configs and return results.

        Each config is run sequentially (dataset build + train).
        Failures in one config don't stop the others — the error is
        recorded and the harness moves on.
        """
        results: list[BenchmarkResult] = []
        for config in self.configs:
            result = self._run_single(config)
            results.append(result)
        return results

    def _run_single(self, config: BenchmarkConfig) -> BenchmarkResult:
        """Run a single benchmark config."""
        start_time = time.time()
        dataset_id = f"bench-{config.name}"
        config_output_dir = pathlib.Path(self.output_dir) / config.name

        result = BenchmarkResult(
            config=config,
            dataset_id=dataset_id,
        )

        try:
            # --- 1. Build dataset ----------------------------------------
            composer = DatasetComposer(
                universe=config.universe,
                source=config.source,
                sentiment=config.sentiment,
                features=config.features,
                label=config.label,
                price_join=config.price_join,
                config=config.config,
            )

            ingestion_result = composer.build(
                output_dir=config_output_dir / "dataset",
                dataset_id=dataset_id,
                start_ns=config.start_ns,
                end_ns=config.end_ns,
                n_folds=config.n_folds,
            )

            result.parquet_path = ingestion_result.parquet_path
            result.manifest_path = ingestion_result.manifest_path

            # --- 2. Train model ------------------------------------------
            artifact, dossier = self._train(
                dataset_manifest_ref=str(ingestion_result.manifest_path),
                dataset_id=dataset_id,
                config=config,
            )

            result.artifact = artifact
            result.dossier = dossier

        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"

        result.duration_seconds = time.time() - start_time

        # Write result summary
        self._write_result_summary(result, config_output_dir)

        return result

    def _train(
        self,
        *,
        dataset_manifest_ref: str,
        dataset_id: str,
        config: BenchmarkConfig,
    ) -> tuple[Any, Any]:
        """Train a model on the built dataset using RealLightGBMTrainer."""
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodTrainingRequest

        trainer = RealLightGBMTrainer(n_folds=config.n_folds)

        req = RunPodTrainingRequest(
            job_id=f"bench-{config.name}",
            dataset_manifest_ref=dataset_manifest_ref,
            model_family="lightgbm",
            random_seed=config.random_seed,
        )

        deadline_ns = time.time_ns() + self.deadline_seconds * 1_000_000_000
        artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)
        return artifact, dossier

    def _write_result_summary(
        self,
        result: BenchmarkResult,
        output_dir: pathlib.Path,
    ) -> None:
        """Write a JSON summary of the benchmark result."""
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path = output_dir / "benchmark_result.json"
        summary_path.write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def write_report(self, results: list[BenchmarkResult]) -> pathlib.Path:
        """Write a combined JSON report of all benchmark results.

        Returns the path to the report file.
        """
        report_path = pathlib.Path(self.output_dir) / "benchmark_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "configs": [r.to_dict() for r in results],
            "summary": self._summary_table(results),
        }
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return report_path

    @staticmethod
    def _summary_table(results: list[BenchmarkResult]) -> list[dict[str, Any]]:
        """Build a summary table for the report."""
        rows: list[dict[str, Any]] = []
        for r in results:
            rows.append({
                "name": r.config.name,
                "source": r.config.source,
                "sentiment": r.config.sentiment,
                "succeeded": r.succeeded,
                "deflated_sharpe": r.deflated_sharpe,
                "pbo": r.pbo,
                "duration_seconds": round(r.duration_seconds, 3),
                "error": r.error,
            })
        return rows


__all__ = ["BenchmarkConfig", "BenchmarkHarness", "BenchmarkResult"]
