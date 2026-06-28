"""
quant_foundry.modules.features.interactions — pairwise interaction features.

Computes pairwise products of per-event-type sentiment features.  For
each pair ``(et1, et2)`` where ``et1 != et2``, produces::

    sent_{et1}_x_{et2} = sent_{et1} * sent_{et2}

Only interactions for event types that have non-zero sentiment in the
row are produced (sparse interactions).  When the number of candidate
pairs exceeds ``max_interactions``, the top pairs by variance across
rows are selected — but since this is a row-level passthrough, the
variance ranking is approximated by the magnitude of the product
(``|sent_{et1} * sent_{et2}|``), which is a monotone proxy for variance
on a single row.

This module is a *passthrough* feature computer — like
:mod:`per_year`, it doesn't consume media items or sentiments directly;
it annotates existing feature rows produced by other feature modules
(e.g. :class:`PerEventTypeFeatures`) via :meth:`annotate_row`.

This module is registered as ``feature:interactions:1.0.0``.
"""

from __future__ import annotations

from typing import Any

from quant_foundry.modules.features.per_event_type import EVENT_TYPES
from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)


@register_module(
    "feature",
    "interactions",
    "1.0.0",
    default_config={
        "event_types": list(EVENT_TYPES),
        "max_interactions": 20,
    },
)
class InteractionsFeatures:
    """Compute pairwise interaction features between event types.

    This module is a passthrough — it doesn't generate new rows, it
    only adds interaction features to existing decision times.  When
    composed via :class:`DatasetComposer`, it runs after other feature
    modules and annotates their rows via :meth:`annotate_row`.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        configured = self.config.get("event_types")
        if configured is not None:
            self.event_types: tuple[str, ...] = tuple(configured)
        else:
            self.event_types = EVENT_TYPES
        self.max_interactions: int = int(self.config.get("max_interactions", 20))

    def compute_features(
        self,
        items: list[MediaItem],
        sentiments: list[SentimentResult],
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> dict[str, dict[int, dict[str, float]]]:
        """Passthrough — returns an empty dict.

        Interaction features are applied by the composer as a
        post-processing step via :meth:`annotate_row`.
        """
        return {}

    def annotate_row(self, row_features: dict[str, float]) -> dict[str, float]:
        """Add pairwise interaction features to a single row.

        For each pair ``(et1, et2)`` where ``et1 != et2``, if both
        ``sent_{et1}`` and ``sent_{et2}`` are present and non-zero in
        ``row_features``, produces ``sent_{et1}_x_{et2}`` =
        ``sent_{et1} * sent_{et2}``.

        When the number of candidate pairs exceeds ``max_interactions``,
        the pairs with the largest product magnitude are kept.
        """
        # Collect candidate pairs with non-zero sentiment
        candidates: list[tuple[str, float]] = []
        ets = self.event_types
        for i, et1 in enumerate(ets):
            key1 = f"sent_{et1}"
            v1 = row_features.get(key1)
            if v1 is None or v1 == 0.0:
                continue
            for j, et2 in enumerate(ets):
                if i == j:
                    continue
                key2 = f"sent_{et2}"
                v2 = row_features.get(key2)
                if v2 is None or v2 == 0.0:
                    continue
                # Canonical ordering: et1 < et2 to avoid duplicates
                if et1 >= et2:
                    continue
                product = v1 * v2
                feat_name = f"sent_{et1}_x_{et2}"
                candidates.append((feat_name, product))

        # If too many candidates, keep the top by |product|
        if len(candidates) > self.max_interactions:
            candidates.sort(key=lambda kv: abs(kv[1]), reverse=True)
            candidates = candidates[: self.max_interactions]

        result = dict(row_features)
        for name, value in candidates:
            result[name] = round(value, 6)
        return result


__all__ = ["InteractionsFeatures"]
