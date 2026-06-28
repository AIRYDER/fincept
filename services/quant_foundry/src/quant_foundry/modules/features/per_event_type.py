"""
quant_foundry.modules.features.per_event_type — per-event-type sentiment features.

Produces one sentiment feature per event type (11 types from the
news-impact-model classifier).  For each ``(symbol, decision_time)``,
the feature value is the mean sentiment of all media items for that
symbol in a lookback window that are classified as that event type.

Event types: ``regulatory, earnings, guidance, macro, product,
security, litigation, partnership, financing, m&a, general``.

Features produced (11):
    ``sent_regulatory``, ``sent_earnings``, ``sent_guidance``,
    ``sent_macro``, ``sent_product``, ``sent_security``,
    ``sent_litigation``, ``sent_partnership``, ``sent_financing``,
    ``sent_m&a``, ``sent_general``

Plus aggregate:
    ``sent_mean`` — mean sentiment across all items in the window
    ``sent_count`` — number of items in the window

This module is registered as ``feature:per-event-type:1.0.0``.
"""

from __future__ import annotations

from typing import Any

from quant_foundry.modules.registry import (
    FeatureComputer,
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)

NS_PER_DAY = 86_400_000_000_000

#: The 11 event types from news_impact_model.events.EVENT_TYPES.
EVENT_TYPES: tuple[str, ...] = (
    "regulatory",
    "earnings",
    "guidance",
    "macro",
    "product",
    "security",
    "litigation",
    "partnership",
    "financing",
    "m&a",
    "general",
)


@register_module(
    "feature",
    "per-event-type",
    "1.0.0",
    default_config={
        "lookback_days": 3,
    },
)
class PerEventTypeFeatures:
    """Compute per-event-type sentiment features.

    For each ``(symbol, decision_time)`` in the date range, looks back
    ``lookback_days`` and computes the mean sentiment of media items
    for that symbol, grouped by event type.  If no items of a given
    type are found in the window, the feature value is 0.0.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.lookback_days: int = self.config.get("lookback_days", 3)
        self.lookback_ns = self.lookback_days * NS_PER_DAY

    def compute_features(
        self,
        items: list[MediaItem],
        sentiments: list[SentimentResult],
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> dict[str, dict[int, dict[str, float]]]:
        """Compute per-event-type sentiment features.

        Returns ``{symbol: {decision_time: {feature_name: value}}}``.
        """
        # Build a lookup: item_id -> sentiment score
        sentiment_by_id = {s.item_id: s.score for s in sentiments}

        # Group items by symbol, sorted by available_at_ns
        items_by_symbol: dict[str, list[MediaItem]] = {}
        for item in items:
            for sym in item.symbols:
                if sym in symbols or sym in symbols:
                    items_by_symbol.setdefault(sym, []).append(item)

        # Sort each symbol's items by time
        for sym in items_by_symbol:
            items_by_symbol[sym].sort(key=lambda i: i.available_at_ns)

        result: dict[str, dict[int, dict[str, float]]] = {}

        # Decision times: daily bars from start_ns to end_ns
        # We align to the media item availability times to avoid
        # generating empty rows for days with no media.
        for sym in symbols:
            sym_items = items_by_symbol.get(sym, [])
            if not sym_items:
                continue

            sym_result: dict[int, dict[str, float]] = {}
            for item in sym_items:
                dt = item.available_at_ns
                if dt < start_ns or dt >= end_ns:
                    continue

                # Look back from dt
                window_start = dt - self.lookback_ns
                window_items = [
                    i for i in sym_items
                    if window_start <= i.available_at_ns <= dt
                ]

                features: dict[str, float] = {}
                # Per-event-type mean sentiment
                for et in EVENT_TYPES:
                    et_scores = [
                        sentiment_by_id[i.item_id]
                        for i in window_items
                        if i.event_type == et and i.item_id in sentiment_by_id
                    ]
                    features[f"sent_{et}"] = (
                        round(sum(et_scores) / len(et_scores), 6)
                        if et_scores else 0.0
                    )

                # Aggregate
                all_scores = [
                    sentiment_by_id[i.item_id]
                    for i in window_items
                    if i.item_id in sentiment_by_id
                ]
                features["sent_mean"] = (
                    round(sum(all_scores) / len(all_scores), 6)
                    if all_scores else 0.0
                )
                features["sent_count"] = float(len(window_items))

                sym_result[dt] = features

            if sym_result:
                result[sym] = sym_result

        return result


__all__ = ["PerEventTypeFeatures", "EVENT_TYPES"]
