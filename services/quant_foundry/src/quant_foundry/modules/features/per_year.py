"""
quant_foundry.modules.features.per_year — per-year slicing features.

Adds a ``year`` column and per-year one-hot features so the model can
learn regime-dependent media response.  This is what enables the
"how did media→price response change from 2018 to 2025" analysis.

Features produced:
    ``year`` — the year as a float (2018.0, 2019.0, ...)
    ``year_2018`` through ``year_2025`` — one-hot indicators

This module is a *passthrough* feature computer — it doesn't consume
media items or sentiments; it just annotates each decision time with
its year.  It's designed to be composed alongside other feature
modules.

This module is registered as ``feature:per-year:1.0.0``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)

NS_PER_SECOND = 1_000_000_000


@register_module(
    "feature",
    "per-year",
    "1.0.0",
    default_config={
        "years": [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
    },
)
class PerYearFeatures:
    """Add per-year features to each decision time.

    This module is a passthrough — it doesn't generate new rows, it
    only adds year features to existing decision times.  When composed
    via :class:`DatasetComposer`, it runs after other feature modules
    and annotates their rows.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.years: list[int] = self.config.get("years", list(range(2018, 2026)))

    def compute_features(
        self,
        items: list[MediaItem],
        sentiments: list[SentimentResult],
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> dict[str, dict[int, dict[str, float]]]:
        """Compute year features for all decision times in the range.

        Generates one row per day per symbol (aligned to media item
        availability times from other feature modules).  Since this
        module doesn't consume items, it returns an empty dict — the
        year annotation is applied by the composer as a post-processing
        step via :meth:`annotate_rows`.
        """
        return {}

    def annotate_row(self, decision_time: int) -> dict[str, float]:
        """Return year features for a single decision time.

        Called by the composer for each row after other feature modules
        have generated the base features.
        """
        year = dt.datetime.fromtimestamp(
            decision_time / NS_PER_SECOND,
            tz=dt.UTC,
        ).year
        features: dict[str, float] = {"year": float(year)}
        for y in self.years:
            features[f"year_{y}"] = 1.0 if y == year else 0.0
        return features


__all__ = ["PerYearFeatures"]
