"""
quant_foundry.modules.benchmark.comparison — side-by-side comparison of benchmark runs.

The :class:`ComparisonReport` takes multiple :class:`BenchmarkResult`
entries (from :class:`BenchmarkHarness.run()`) and produces a
side-by-side comparison report that answers:

- Which benchmark config has the highest deflated Sharpe?
- Which has the lowest PBO (probability of backtest overfitting)?
- Which sentiment engine / source combination wins?
- How do training times compare?

The report is JSON-serializable and includes a ranked summary table
suitable for display in a dashboard or notebook.

Usage::

    harness = BenchmarkHarness(configs=..., output_dir=...)
    results = harness.run()
    comparison = ComparisonReport.from_results(results)
    comparison.write(output_dir / "comparison_report.json")
    print(comparison.summary_text())
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

from quant_foundry.modules.benchmark.harness import BenchmarkResult


@dataclass
class ComparisonReport:
    """Side-by-side comparison of multiple benchmark runs.

    Ranks results by deflated Sharpe (descending) and PBO (ascending),
    and groups by source / sentiment engine to identify the best
    module combinations.
    """

    results: list[BenchmarkResult]
    ranked_by_sharpe: list[dict[str, Any]] = field(default_factory=list)
    ranked_by_pbo: list[dict[str, Any]] = field(default_factory=list)
    best_by_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    best_by_sentiment: dict[str, dict[str, Any]] = field(default_factory=dict)
    summary_table: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_results(cls, results: list[BenchmarkResult]) -> ComparisonReport:
        """Build a comparison report from benchmark results."""
        report = cls(results=results)
        report._compute()
        return report

    def _compute(self) -> None:
        """Compute all rankings and groupings."""
        # --- Summary table ----------------------------------------------
        self.summary_table = []
        for r in self.results:
            self.summary_table.append({
                "name": r.config.name,
                "source": r.config.source,
                "sentiment": r.config.sentiment,
                "succeeded": r.succeeded,
                "deflated_sharpe": r.deflated_sharpe,
                "pbo": r.pbo,
                "row_count": (
                    r.dossier.metadata.get("n_rows", "?")
                    if r.dossier else "?"
                ),
                "n_features": (
                    r.dossier.metadata.get("n_features", "?")
                    if r.dossier else "?"
                ),
                "duration_seconds": round(r.duration_seconds, 3),
                "error": r.error,
            })

        # --- Ranked by deflated Sharpe (descending) ---------------------
        successful = [r for r in self.results if r.succeeded]
        self.ranked_by_sharpe = [
            {
                "name": r.config.name,
                "source": r.config.source,
                "sentiment": r.config.sentiment,
                "deflated_sharpe": r.deflated_sharpe,
                "pbo": r.pbo,
            }
            for r in sorted(
                successful,
                key=lambda r: r.deflated_sharpe or -999,
                reverse=True,
            )
        ]

        # --- Ranked by PBO (ascending — lower is better) ----------------
        self.ranked_by_pbo = [
            {
                "name": r.config.name,
                "source": r.config.source,
                "sentiment": r.config.sentiment,
                "pbo": r.pbo,
                "deflated_sharpe": r.deflated_sharpe,
            }
            for r in sorted(
                successful,
                key=lambda r: r.pbo if r.pbo is not None else 999,
            )
        ]

        # --- Best by source ---------------------------------------------
        source_groups: dict[str, list[BenchmarkResult]] = {}
        for r in successful:
            source_key = r.config.source.split(":")[1] if ":" in r.config.source else r.config.source
            source_groups.setdefault(source_key, []).append(r)

        for source_key, group in source_groups.items():
            best = max(group, key=lambda r: r.deflated_sharpe or -999)
            self.best_by_source[source_key] = {
                "name": best.config.name,
                "sentiment": best.config.sentiment,
                "deflated_sharpe": best.deflated_sharpe,
                "pbo": best.pbo,
            }

        # --- Best by sentiment engine -----------------------------------
        sentiment_groups: dict[str, list[BenchmarkResult]] = {}
        for r in successful:
            sentiment_key = r.config.sentiment.split(":")[1] if ":" in r.config.sentiment else r.config.sentiment
            sentiment_groups.setdefault(sentiment_key, []).append(r)

        for sentiment_key, group in sentiment_groups.items():
            best = max(group, key=lambda r: r.deflated_sharpe or -999)
            self.best_by_sentiment[sentiment_key] = {
                "name": best.config.name,
                "source": best.config.source,
                "deflated_sharpe": best.deflated_sharpe,
                "pbo": best.pbo,
            }

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "summary_table": self.summary_table,
            "ranked_by_sharpe": self.ranked_by_sharpe,
            "ranked_by_pbo": self.ranked_by_pbo,
            "best_by_source": self.best_by_source,
            "best_by_sentiment": self.best_by_sentiment,
        }

    def write(self, path: pathlib.Path) -> pathlib.Path:
        """Write the comparison report to a JSON file.

        Returns the path to the written file.
        """
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def summary_text(self) -> str:
        """Return a human-readable summary of the comparison."""
        lines = ["=" * 70, "BENCHMARK COMPARISON REPORT", "=" * 70, ""]

        # Summary table
        lines.append(f"{'Name':<30s} {'Source':<20s} {'Sentiment':<25s} {'DSR':>8s} {'PBO':>8s}")
        lines.append("-" * 95)
        for row in self.summary_table:
            dsr = f"{row['deflated_sharpe']:.4f}" if row['deflated_sharpe'] is not None else "N/A"
            pbo = f"{row['pbo']:.4f}" if row['pbo'] is not None else "N/A"
            lines.append(
                f"{row['name']:<30s} {row['source']:<20s} {row['sentiment']:<25s} {dsr:>8s} {pbo:>8s}"
            )
        lines.append("")

        # Ranked by Sharpe
        if self.ranked_by_sharpe:
            lines.append("Ranked by Deflated Sharpe (best first):")
            for i, entry in enumerate(self.ranked_by_sharpe, 1):
                dsr = f"{entry['deflated_sharpe']:.4f}" if entry['deflated_sharpe'] is not None else "N/A"
                lines.append(f"  {i}. {entry['name']:<30s} DSR={dsr}")
            lines.append("")

        # Best by source
        if self.best_by_source:
            lines.append("Best by Source:")
            for source, info in self.best_by_source.items():
                dsr = f"{info['deflated_sharpe']:.4f}" if info['deflated_sharpe'] is not None else "N/A"
                lines.append(f"  {source:<20s} → {info['name']:<30s} DSR={dsr}")
            lines.append("")

        # Best by sentiment
        if self.best_by_sentiment:
            lines.append("Best by Sentiment Engine:")
            for sentiment, info in self.best_by_sentiment.items():
                dsr = f"{info['deflated_sharpe']:.4f}" if info['deflated_sharpe'] is not None else "N/A"
                lines.append(f"  {sentiment:<20s} → {info['name']:<30s} DSR={dsr}")
            lines.append("")

        return "\n".join(lines)


__all__ = ["ComparisonReport"]
