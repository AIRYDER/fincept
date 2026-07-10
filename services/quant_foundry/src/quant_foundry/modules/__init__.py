"""
quant_foundry.modules — modular A/B-testable dataset system.

Every component (sentiment engine, source adapter, label computer,
feature computer, universe selector, price joiner) is a swappable
module that implements a Protocol interface for its category.  Modules
register themselves in the central :class:`ModuleRegistry`, and the
:class:`DatasetComposer` combines them to build dataset variants that
can be trained and benchmarked against each other.

Public surface:
    - :class:`ModuleRegistry`, :func:`register_module`
    - Protocol interfaces: :class:`SentimentEngine`, :class:`SourceAdapter`,
      :class:`LabelComputer`, :class:`FeatureComputer`,
      :class:`UniverseSelector`, :class:`PriceJoiner`
    - Shared types: :class:`MediaItem`, :class:`PriceBar`,
      :class:`SentimentResult`, :class:`FeatureRowData`
    - :class:`DatasetComposer` — combines modules → builds dataset
    - :func:`load_all_modules` — imports all module subpackages so
      they self-register

Usage::

    from quant_foundry.modules import (
        DatasetComposer,
        ModuleRegistry,
        load_all_modules,
    )

    load_all_modules()  # import all modules so they register
    registry = ModuleRegistry.instance()
    print(registry.list_all())

    composer = DatasetComposer(
        universe="universe:sp500:1.0.0",
        source="source:newsapi:1.0.0",
        sentiment="sentiment:naive-wordlist:1.0.0",
        features=["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
        label="label:abnormal-return:1.0.0",
        price_join="price_join:alpaca-bars:1.0.0",
    )
    result = composer.build(
        output_dir=Path("data/datasets"),
        dataset_id="media-sentiment-price-2023",
        start_ns=..., end_ns=...,
        n_folds=3,
    )
"""

from __future__ import annotations

from quant_foundry.modules.registry import (
    MODULE_CATEGORIES,
    FeatureComputer,
    FeatureRowData,
    LabelComputer,
    MediaItem,
    ModuleInfo,
    ModuleRegistry,
    PriceBar,
    PriceJoiner,
    SentimentEngine,
    SentimentResult,
    SourceAdapter,
    UniverseSelector,
    register_module,
)


def load_all_modules() -> None:
    """Import all module subpackages so they self-register.

    Call this once at startup to populate the registry with all
    available modules.  Each subpackage's ``__init__`` imports its
    modules, which triggers the ``@register_module`` decorator.
    """
    # Import each category package to trigger registration.
    import quant_foundry.modules.features
    import quant_foundry.modules.labels
    import quant_foundry.modules.price_join
    import quant_foundry.modules.sentiment
    import quant_foundry.modules.sources
    import quant_foundry.modules.universe  # noqa: F401 - side-effect import (module registration)


# Lazy import of DatasetComposer to avoid circular imports at module level.
def __getattr__(name: str) -> Any:
    if name == "DatasetComposer":
        from quant_foundry.modules.composer import DatasetComposer

        return DatasetComposer
    if name == "BenchmarkHarness":
        from quant_foundry.modules.benchmark.harness import BenchmarkHarness

        return BenchmarkHarness
    if name == "BenchmarkConfig":
        from quant_foundry.modules.benchmark.harness import BenchmarkConfig

        return BenchmarkConfig
    if name == "BenchmarkResult":
        from quant_foundry.modules.benchmark.harness import BenchmarkResult

        return BenchmarkResult
    if name == "AttributionReport":
        from quant_foundry.modules.benchmark.attribution import AttributionReport

        return AttributionReport
    if name == "ComparisonReport":
        from quant_foundry.modules.benchmark.comparison import ComparisonReport

        return ComparisonReport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from typing import Any  # noqa: E402

__all__ = [
    "MODULE_CATEGORIES",
    "AttributionReport",
    "BenchmarkConfig",
    "BenchmarkHarness",
    "BenchmarkResult",
    "ComparisonReport",
    "DatasetComposer",
    "FeatureComputer",
    "FeatureRowData",
    "LabelComputer",
    "MediaItem",
    "ModuleInfo",
    "ModuleRegistry",
    "PriceBar",
    "PriceJoiner",
    "SentimentEngine",
    "SentimentResult",
    "SourceAdapter",
    "UniverseSelector",
    "load_all_modules",
    "register_module",
]
