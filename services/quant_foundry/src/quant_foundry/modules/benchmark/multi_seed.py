"""
quant_foundry.modules.benchmark.multi_seed — multi-seed run wrapper.

The :class:`MultiSeedRunner` runs the same :class:`BenchmarkConfig`
with different random seeds (0, 1, 2, ..., n_seeds-1) and aggregates
the results.  This is the standard way to assess whether a benchmark
config's performance is stable across random initialization or just a
lucky seed.

The runner produces a :class:`MultiSeedResult` with the mean ± std of
the deflated Sharpe and PBO across seeds, plus the per-seed Sharpe
values for downstream significance testing (e.g. feeding into
:func:`bootstrap_sharpe_ci`).

Usage::

    runner = MultiSeedRunner(config, n_seeds=5, output_dir=Path("data/bench"))
    result = runner.run()
    print(f"Sharpe: {result.sharpe_mean:.3f} ± {result.sharpe_std:.3f}")
    if result.all_succeeded:
        print("All seeds succeeded")
"""

from __future__ import annotations

import dataclasses
import pathlib
from dataclasses import dataclass
from typing import Any

from quant_foundry.modules.benchmark.harness import (
    BenchmarkConfig,
    BenchmarkHarness,
    BenchmarkResult,
)


__all__ = ["MultiSeedResult", "MultiSeedRunner"]


@dataclass
class MultiSeedResult:
    """Aggregated result of running a config across multiple seeds.

    Attributes:
        config: The original (un-seeded) benchmark config.
        results: One :class:`BenchmarkResult` per seed.
        sharpe_mean: Mean deflated Sharpe across successful seeds.
        sharpe_std: Std dev of deflated Sharpe across successful seeds.
        pbo_mean: Mean PBO across successful seeds.
        pbo_std: Std dev of PBO across successful seeds.
        sharpe_values: Per-seed deflated Sharpe (None for failed seeds).
        all_succeeded: True if every seed succeeded.
    """

    config: BenchmarkConfig
    results: list[BenchmarkResult]
    sharpe_mean: float
    sharpe_std: float
    pbo_mean: float
    pbo_std: float
    sharpe_values: list[float | None]
    all_succeeded: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "config_name": self.config.name,
            "n_seeds": len(self.results),
            "sharpe_mean": round(self.sharpe_mean, 6),
            "sharpe_std": round(self.sharpe_std, 6),
            "pbo_mean": round(self.pbo_mean, 6),
            "pbo_std": round(self.pbo_std, 6),
            "sharpe_values": [
                round(v, 6) if v is not None else None for v in self.sharpe_values
            ],
            "all_succeeded": self.all_succeeded,
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class MultiSeedRunner:
    """Runs a single benchmark config across multiple random seeds.

    Args:
        config: The base :class:`BenchmarkConfig` to run.
        n_seeds: Number of seeds to run (default 5).  Seeds used are
            ``0, 1, ..., n_seeds - 1``.
        output_dir: Base directory for dataset + model artifacts.
        deadline_seconds: Per-seed deadline for training.
    """

    config: BenchmarkConfig
    n_seeds: int = 5
    output_dir: pathlib.Path = pathlib.Path("data/benchmarks")
    deadline_seconds: int = 600

    def run(self) -> MultiSeedResult:
        """Run the config across ``n_seeds`` seeds and aggregate results.

        Each seed gets a modified config with a unique ``random_seed``
        and a ``name`` suffix like ``"_seed0"``.  The runs are executed
        via :class:`BenchmarkHarness`.
        """
        seeded_configs = [self._seeded_config(i) for i in range(self.n_seeds)]
        harness = BenchmarkHarness(
            configs=seeded_configs,
            output_dir=self.output_dir,
            deadline_seconds=self.deadline_seconds,
        )
        results = harness.run()

        sharpe_values: list[float | None] = [r.deflated_sharpe for r in results]
        pbo_values: list[float | None] = [r.pbo for r in results]

        successful_sharpes = [v for v in sharpe_values if v is not None]
        successful_pbos = [v for v in pbo_values if v is not None]

        sharpe_mean, sharpe_std = _mean_std(successful_sharpes)
        pbo_mean, pbo_std = _mean_std(successful_pbos)

        return MultiSeedResult(
            config=self.config,
            results=results,
            sharpe_mean=sharpe_mean,
            sharpe_std=sharpe_std,
            pbo_mean=pbo_mean,
            pbo_std=pbo_std,
            sharpe_values=sharpe_values,
            all_succeeded=all(r.succeeded for r in results),
        )

    def _seeded_config(self, seed: int) -> BenchmarkConfig:
        """Build a copy of the base config with a specific seed."""
        base = dataclasses.asdict(self.config)
        base["name"] = f"{self.config.name}_seed{seed}"
        base["random_seed"] = seed
        return BenchmarkConfig(**base)


def _mean_std(values: list[float]) -> tuple[float, float]:
    """Compute (mean, std) of a list.  Returns (0.0, 0.0) if empty."""
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return float(mean), 0.0
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return float(mean), float(variance ** 0.5)
