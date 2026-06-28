"""
quant_foundry.modules.registry — central module registry + Protocol interfaces.

This is the backbone of the modular A/B-testable dataset system. Every
component (sentiment engine, source adapter, label computer, feature
computer, universe selector, price joiner) is a swappable module that
implements a Protocol interface for its category. Modules register
themselves in the central :class:`ModuleRegistry`, and the
:class:`DatasetComposer` combines them to build dataset variants that
can be trained and benchmarked against each other.

Design principles:
- **Protocol-based interfaces.** Each category has a ``Protocol`` that
  modules implement. No inheritance hierarchy — duck typing.
- **Stable module identity.** Each module has a unique ``module_id``,
  ``version``, and ``config``. The ``module_id`` is used in dataset IDs
  and benchmark reports so you can trace which module produced which
  result.
- **Lazy heavy deps.** Modules import numpy/polars/torch inside methods,
  not at module level, matching the convention in ``data_ingestion/``.
- **No circular imports.** The registry only holds references to module
  *classes* (not instances), and modules import the registry only for
  the ``register_module`` decorator — never the other way around.
- **Plugin-style.** Modules self-register via the
  :func:`register_module` decorator at import time. The registry is
  populated by importing the module subpackages.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# Module metadata                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModuleInfo:
    """Identity + provenance for a registered module.

    ``module_id`` is the stable, unique identifier used in dataset IDs
    and benchmark reports (e.g. ``"sentiment:finbert-v1"``).  ``category``
    is one of the :data:`MODULE_CATEGORIES`.  ``version`` is a
    semver-ish string for tracking module evolution.  ``config`` is the
    module's default configuration (overridable at compose time).
    """

    module_id: str
    category: str
    version: str
    config: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Module categories                                                           #
# --------------------------------------------------------------------------- #

#: The six module categories.  Each has a Protocol interface below.
MODULE_CATEGORIES: tuple[str, ...] = (
    "sentiment",
    "source",
    "label",
    "feature",
    "universe",
    "price_join",
)


# --------------------------------------------------------------------------- #
# Shared data types passed between modules                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MediaItem:
    """A single media item (news article, social post, etc.).

    This is the atom that flows through the pipeline:
    ``source → sentiment → feature → label → dataset``.

    ``available_at_ns`` is the PIT decision time — when the system could
    have acted on this item.  ``source`` identifies the platform
    (``"newsapi"``, ``"stocktwits"``, ``"reddit"``, ``"x_twitter"``).
    ``event_type`` is one of the 11 types from the news-impact-model
    classifier (``"regulatory"``, ``"earnings"``, etc.) or ``"social"``
    for social posts that don't map to a news event type.
    """

    item_id: str
    source: str
    headline: str
    body: str
    available_at_ns: int
    symbols: tuple[str, ...] = ()
    event_type: str = "general"
    url: str | None = None
    language: str = "en"
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.headline}\n{self.body}".strip()


@dataclass(frozen=True)
class PriceBar:
    """A single OHLCV bar used for price joining and label computation."""

    symbol: str
    ts_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class SentimentResult:
    """Sentiment score for a single media item.

    ``score`` is in ``[-1, 1]`` where -1 = very negative, 0 = neutral,
    1 = very positive.  ``confidence`` is in ``[0, 1]``.  ``provider``
    identifies the sentiment engine (``"naive"``, ``"finbert"``,
    ``"openai"``, ``"anthropic"``, ``"xai"``, ``"minimax"``).
    """

    item_id: str
    provider: str
    score: float
    confidence: float


@dataclass(frozen=True)
class FeatureRowData:
    """A computed feature row ready for FeatureLakeBuilder.

    ``decision_time`` is the PIT cutoff.  ``features`` maps feature
    names to float values.  ``label`` is the target value (may be
    ``None`` if the label module hasn't run yet).  ``symbol`` is the
    ticker this row pertains to.
    """

    symbol: str
    decision_time: int
    features: dict[str, float]
    label: float | None = None


# --------------------------------------------------------------------------- #
# Protocol interfaces for each module category                                #
# --------------------------------------------------------------------------- #


@runtime_checkable
class SentimentEngine(Protocol):
    """Scores media items for sentiment.

    A sentiment module takes a list of :class:`MediaItem` and returns a
    list of :class:`SentimentResult` (one per item).  The engine may be
    a naive word-list, FinBERT, or an LLM API call.
    """

    info: ModuleInfo

    def score(self, items: list[MediaItem]) -> list[SentimentResult]: ...


@runtime_checkable
class SourceAdapter(Protocol):
    """Fetches media items from a data source.

    A source module fetches raw data (news articles, social posts) and
    normalizes them into :class:`MediaItem` objects.  Source adapters
    may be async (vendor APIs) or sync (local files).
    """

    info: ModuleInfo

    async def fetch(
        self,
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> list[MediaItem]: ...


@runtime_checkable
class LabelComputer(Protocol):
    """Computes labels (targets) for feature rows.

    A label module takes feature rows + price bars and produces labels.
    The label is the thing the model learns to predict — e.g. abnormal
    return at +5d.  Labels use future data (by design) but are the
    *target*, not a feature — PIT correctness is enforced only on
    features.
    """

    info: ModuleInfo

    def compute_labels(
        self,
        rows: list[FeatureRowData],
        *,
        price_bars: dict[str, list[PriceBar]],
        benchmark_bars: list[PriceBar],
    ) -> list[FeatureRowData]: ...


@runtime_checkable
class FeatureComputer(Protocol):
    """Computes features from media items + sentiment scores.

    A feature module takes media items (with sentiment scores attached)
    and produces feature values grouped by ``(symbol, decision_time)``.
    Multiple feature modules can be composed — their features are merged.
    """

    info: ModuleInfo

    def compute_features(
        self,
        items: list[MediaItem],
        sentiments: list[SentimentResult],
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> dict[str, dict[int, dict[str, float]]]: ...


@runtime_checkable
class UniverseSelector(Protocol):
    """Selects the ticker universe for the dataset.

    A universe module returns the list of symbols to include.  Different
    modules implement different selection strategies (S&P 500, curated
    high-volume, volume-driven, tiered).
    """

    info: ModuleInfo

    def select_symbols(
        self,
        *,
        start_ns: int,
        end_ns: int,
    ) -> list[str]: ...


@runtime_checkable
class PriceJoiner(Protocol):
    """Loads price bars and joins them to media events.

    A price-join module loads OHLCV bars for the universe and provides
    them to the label computer.  It also provides benchmark bars for
    abnormal-return computation.
    """

    info: ModuleInfo

    def load_bars(
        self,
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> tuple[dict[str, list[PriceBar]], list[PriceBar]]: ...


# --------------------------------------------------------------------------- #
# Central registry                                                            #
# --------------------------------------------------------------------------- #


class ModuleRegistry:
    """Central registry of all available modules.

    Modules self-register via the :func:`register_module` decorator.
    The registry holds module *classes* keyed by ``module_id``.  Callers
    instantiate modules with optional config overrides at compose time.

    Usage::

        @register_module("sentiment", "naive-wordlist", "1.0.0")
        class NaiveWordlistSentiment:
            ...

        registry = ModuleRegistry.instance()
        cls = registry.get("sentiment:naive-wordlist:1.0.0")
        module = cls(config={"custom_words": [...]})
    """

    _instance: ModuleRegistry | None = None

    def __init__(self) -> None:
        self._modules: dict[str, dict[str, Any]] = {}

    @classmethod
    def instance(cls) -> ModuleRegistry:
        """Return the singleton registry instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(
        self,
        category: str,
        module_id: str,
        version: str,
        module_cls: type,
        default_config: dict[str, Any] | None = None,
    ) -> None:
        """Register a module class.

        Args:
            category: One of :data:`MODULE_CATEGORIES`.
            module_id: Unique module identifier (e.g. ``"naive-wordlist"``).
            version: Semver-ish version string (e.g. ``"1.0.0"``).
            module_cls: The module class (implements the category Protocol).
            default_config: Default configuration dict.
        """
        if category not in MODULE_CATEGORIES:
            raise ValueError(
                f"unknown module category: {category!r}; "
                f"valid: {MODULE_CATEGORIES}",
            )
        full_id = f"{category}:{module_id}:{version}"
        if full_id in self._modules:
            raise ValueError(
                f"module already registered: {full_id}",
            )
        self._modules[full_id] = {
            "category": category,
            "module_id": module_id,
            "version": version,
            "cls": module_cls,
            "default_config": dict(default_config or {}),
        }

    def get(self, full_id: str) -> type:
        """Return the module class for a full ID (``category:id:version``)."""
        entry = self._modules.get(full_id)
        if entry is None:
            raise KeyError(
                f"module not registered: {full_id!r}; "
                f"available: {sorted(self._modules.keys())}",
            )
        return entry["cls"]

    def get_info(self, full_id: str) -> ModuleInfo:
        """Return :class:`ModuleInfo` for a registered module."""
        entry = self._modules[full_id]
        return ModuleInfo(
            module_id=entry["module_id"],
            category=entry["category"],
            version=entry["version"],
            config=dict(entry["default_config"]),
        )

    def list_by_category(self, category: str) -> list[str]:
        """List all registered module IDs in a category."""
        if category not in MODULE_CATEGORIES:
            raise ValueError(f"unknown category: {category!r}")
        return sorted(
            full_id
            for full_id, entry in self._modules.items()
            if entry["category"] == category
        )

    def list_all(self) -> dict[str, list[str]]:
        """List all registered modules grouped by category."""
        return {
            cat: self.list_by_category(cat) for cat in MODULE_CATEGORIES
        }

    def create(
        self,
        full_id: str,
        *,
        config: dict[str, Any] | None = None,
    ) -> Any:
        """Instantiate a module with optional config overrides.

        Merges the module's default config with the caller-provided
        config (caller wins on conflicts).
        """
        entry = self._modules[full_id]
        merged = {**entry["default_config"], **(config or {})}
        return entry["cls"](config=merged)

    def clear(self) -> None:
        """Clear all registered modules (for testing)."""
        self._modules.clear()


# --------------------------------------------------------------------------- #
# Registration decorator                                                      #
# --------------------------------------------------------------------------- #


def register_module(
    category: str,
    module_id: str,
    version: str,
    default_config: dict[str, Any] | None = None,
) -> Any:
    """Decorator that registers a module class in the central registry.

    Usage::

        @register_module("sentiment", "naive-wordlist", "1.0.0")
        class NaiveWordlistSentiment:
            def __init__(self, config: dict[str, Any] | None = None) -> None:
                self.config = config or {}
            def score(self, items): ...
    """

    def decorator(cls: type) -> type:
        ModuleRegistry.instance().register(
            category=category,
            module_id=module_id,
            version=version,
            module_cls=cls,
            default_config=default_config,
        )
        # Attach ModuleInfo as a class attribute so Protocol checks
        # (``hasattr(cls, 'info')``) pass before instantiation.
        cls.info = ModuleInfo(
            module_id=module_id,
            category=category,
            version=version,
            config=dict(default_config or {}),
        )
        return cls

    return decorator


__all__ = [
    "MODULE_CATEGORIES",
    "MediaItem",
    "ModuleInfo",
    "ModuleRegistry",
    "PriceBar",
    "SentimentResult",
    "FeatureRowData",
    "LabelComputer",
    "FeatureComputer",
    "PriceJoiner",
    "SentimentEngine",
    "SourceAdapter",
    "UniverseSelector",
    "register_module",
]
