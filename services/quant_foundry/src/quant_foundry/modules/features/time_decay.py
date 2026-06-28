"""
quant_foundry.modules.features.time_decay — exponential time-decay weighted sentiment features.

Instead of a flat mean over the lookback window, each media item is
weighted by an exponential decay factor based on its age relative to
the decision time::

    weight = exp(-ln(2) * age_days / half_life)

where ``age_days = (decision_time - item.available_at_ns) / NS_PER_DAY``.
This means an item exactly ``half_life`` days old gets half the weight
of a fresh item, so recent news dominates the feature value.

Features produced (per event type + aggregate):
    ``sent_decay_{event_type}`` — decay-weighted mean sentiment for each
        event type in ``event_types`` (default: the 11 news types plus
        ``"social"``).
    ``sent_decay_mean`` — decay-weighted mean sentiment across all items
        in the window.

This module is registered as ``feature:time-decay:1.0.0``.
"""

from __future__ import annotations

import math
from typing import Any

from quant_foundry.modules.features.per_event_type import EVENT_TYPES
from quant_foundry.modules.registry import (
    FeatureComputer,
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)

NS_PER_DAY = 86_400_000_000_000

#: Default event types: the 11 news types plus social posts.
DEFAULT_EVENT_TYPES: tuple[str, ...] = EVENT_TYPES + ("social",)

#: Natural log of 2, used for half-life based exponential decay.
_LN2 = math.log(2.0)


@register_module(
    "feature",
    "time-decay",
    "1.0.0",
    default_config={
        "half_life_days": 3.0,
        "lookback_days": 14,
        "event_types": list(DEFAULT_EVENT_TYPES),
    },
)
class TimeDecayFeatures:
    """Compute exponential time-decay weighted sentiment features.

    For each ``(symbol, decision_time)`` in the date range, looks back
    ``lookback_days`` and computes the decay-weighted mean sentiment of
    media items for that symbol, grouped by event type.  Recent items
    receive exponentially higher weight than old items, controlled by
    ``half_life_days``.  If no items of a given type are found in the
    window, the feature value is 0.0.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.half_life_days: float = float(self.config.get("half_life_days", 3.0))
        self.lookback_days: int = int(self.config.get("lookback_days", 14))
        self.lookback_ns = self.lookback_days * NS_PER_DAY
        configured = self.config.get("event_types")
        if configured is not None:
            self.event_types: tuple[str, ...] = tuple(configured)
        else:
            self.event_types = DEFAULT_EVENT_TYPES

    def _decay_weight(self, age_days: float) -> float:
        """Exponential decay weight for an item ``age_days`` old.

        ``weight = exp(-ln(2) * age_days / half_life)``.  An item exactly
        ``half_life`` days old gets weight 0.5.
        """
        if age_days <= 0.0:
            return 1.0
        return math.exp(-_LN2 * age_days / self.half_life_days)

    def _weighted_mean(
        self,
        scores: list[tuple[float, float]],
    ) -> float:
        """Compute the weighted mean of ``(score, weight)`` pairs.

        Returns 0.0 if there are no items (or total weight is zero).
        """
        if not scores:
            return 0.0
        total_weight = sum(w for _, w in scores)
        if total_weight <= 0.0:
            return 0.0
        weighted_sum = sum(s * w for s, w in scores)
        return round(weighted_sum / total_weight, 6)

    def compute_features(
        self,
        items: list[MediaItem],
        sentiments: list[SentimentResult],
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> dict[str, dict[int, dict[str, float]]]:
        """Compute time-decay weighted sentiment features.

        Returns ``{symbol: {decision_time: {feature_name: value}}}``.
        """
        sentiment_by_id = {s.item_id: s.score for s in sentiments}

        # Group items by symbol, sorted by available_at_ns
        items_by_symbol: dict[str, list[MediaItem]] = {}
        for item in items:
            for sym in item.symbols:
                if sym in symbols:
                    items_by_symbol.setdefault(sym, []).append(item)

        for sym in items_by_symbol:
            items_by_symbol[sym].sort(key=lambda i: i.available_at_ns)

        result: dict[str, dict[int, dict[str, float]]] = {}

        for sym in symbols:
            sym_items = items_by_symbol.get(sym, [])
            if not sym_items:
                continue

            sym_result: dict[int, dict[str, float]] = {}
            for item in sym_items:
                dt = item.available_at_ns
                if dt < start_ns or dt >= end_ns:
                    continue

                window_start = dt - self.lookback_ns
                window_items = [
                    i for i in sym_items
                    if window_start <= i.available_at_ns <= dt
                ]

                features: dict[str, float] = {}
                # Per-event-type decay-weighted mean sentiment
                for et in self.event_types:
                    et_scores: list[tuple[float, float]] = []
                    for i in window_items:
                        if i.event_type != et:
                            continue
                        if i.item_id not in sentiment_by_id:
                            continue
                        age_days = (dt - i.available_at_ns) / NS_PER_DAY
                        et_scores.append(
                            (sentiment_by_id[i.item_id], self._decay_weight(age_days)),
                        )
                    features[f"sent_decay_{et}"] = self._weighted_mean(et_scores)

                # Aggregate decay-weighted mean sentiment
                all_scores: list[tuple[float, float]] = []
                for i in window_items:
                    if i.item_id not in sentiment_by_id:
                        continue
                    age_days = (dt - i.available_at_ns) / NS_PER_DAY
                    all_scores.append(
                        (sentiment_by_id[i.item_id], self._decay_weight(age_days)),
                    )
                features["sent_decay_mean"] = self._weighted_mean(all_scores)

                sym_result[dt] = features

            if sym_result:
                result[sym] = sym_result

        return result


__all__ = ["TimeDecayFeatures", "DEFAULT_EVENT_TYPES"]
