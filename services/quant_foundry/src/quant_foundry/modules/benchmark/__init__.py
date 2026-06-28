"""Benchmark harness + attribution + comparison + significance reports."""

from __future__ import annotations

from quant_foundry.modules.benchmark.attribution import AttributionReport
from quant_foundry.modules.benchmark.comparison import ComparisonReport
from quant_foundry.modules.benchmark.harness import (
    BenchmarkConfig,
    BenchmarkHarness,
    BenchmarkResult,
)
from quant_foundry.modules.benchmark.multi_seed import (
    MultiSeedResult,
    MultiSeedRunner,
)
from quant_foundry.modules.benchmark.placebo import PlaceboTest
from quant_foundry.modules.benchmark.significance import (
    bootstrap_sharpe_ci,
    bootstrap_sharpe_difference_ci,
    diebold_mariano_test,
)

__all__ = [
    "AttributionReport",
    "BenchmarkConfig",
    "BenchmarkHarness",
    "BenchmarkResult",
    "ComparisonReport",
    "MultiSeedResult",
    "MultiSeedRunner",
    "PlaceboTest",
    "bootstrap_sharpe_ci",
    "bootstrap_sharpe_difference_ci",
    "diebold_mariano_test",
]
