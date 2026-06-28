"""
quant_foundry.modules.features.engagement_weighted — engagement-weighted sentiment features.

Each media item's sentiment is weighted by an *engagement score* derived
from platform-specific metadata.  The intuition: a heavily-discussed
StockTwits post or a highly-upvoted Reddit thread carries more market
attention (and therefore more price-impact signal) than a low-engagement
item.

Engagement metrics by source:
    - **StockTwits**: presence of ``metadata["stocktwits_sentiment"]``
      (tagged messages are higher-signal than untagged ones).
    - **Reddit**: ``metadata["reddit_score"]`` (upvotes) and
      ``metadata["reddit_num_comments"]``.
    - **X/Twitter**: ``metadata["x_retweet_count"]``,
      ``metadata["x_like_count"]``, ``metadata["x_reply_count"]``.
    - **NewsAPI**: no engagement metric available — weight = 1.0.

The raw engagement counts are transformed with a ``log1p`` scale to
prevent viral posts from dominating the weighted mean.  The composite
score is then normalized so the maximum weight in a window is 1.0.

Features produced (per event type + aggregate):
    ``sent_eng_{event_type}`` — engagement-weighted mean sentiment for
        each event type.
    ``sent_eng_mean`` — engagement-weighted mean sentiment across all
        items in the window.

This module is registered as ``feature:engagement-weighted:1.0.0``.
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


def _to_float(value: Any) -> float | None:
    """Safely convert a metadata value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@register_module(
    "feature",
    "engagement-weighted",
    "1.0.0",
    default_config={
        "lookback_days": 3,
        "engagement_metric": "auto",
        "event_types": list(DEFAULT_EVENT_TYPES),
    },
)
class EngagementWeightedFeatures:
    """Compute engagement-weighted sentiment features.

    For each ``(symbol, decision_time)`` in the date range, looks back
    ``lookback_days`` and computes the engagement-weighted mean sentiment
    of media items for that symbol, grouped by event type.  Items with
    higher engagement (upvotes, likes, retweets, etc.) receive higher
    weight.  If no items of a given type are found in the window, the
    feature value is 0.0.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.lookback_days: int = int(self.config.get("lookback_days", 3))
        self.lookback_ns = self.lookback_days * NS_PER_DAY
        self.engagement_metric: str = str(self.config.get("engagement_metric", "auto"))
        configured = self.config.get("event_types")
        if configured is not None:
            self.event_types: tuple[str, ...] = tuple(configured)
        else:
            self.event_types = DEFAULT_EVENT_TYPES

    def _engagement_score(self, item: MediaItem) -> float:
        """Compute a normalized engagement score for a single item.

        Uses a ``log1p`` transform on raw counts to prevent viral posts
        from dominating.  Returns a non-negative weight; NewsAPI items
        (no engagement metric) get weight 1.0.

        When ``engagement_metric == "auto"``, the metric is detected
        from the item's ``source``.
        """
        metric = self.engagement_metric
        md = item.metadata

        if metric == "auto":
            source = item.source.lower()
            if source == "stocktwits":
                metric = "stocktwits"
            elif source == "reddit":
                metric = "reddit"
            elif source in ("x_twitter", "x", "twitter"):
                metric = "x"
            else:
                # NewsAPI or unknown — no engagement metric
                return 1.0

        if metric == "stocktwits":
            # Tagged messages (with stocktwits_sentiment) are higher signal.
            # A tagged message gets a base engagement of 2.0, untagged 1.0.
            if "stocktwits_sentiment" in md:
                return 2.0
            return 1.0

        if metric == "reddit":
            score = _to_float(md.get("reddit_score")) or 0.0
            comments = _to_float(md.get("reddit_num_comments")) or 0.0
            # Composite: log1p of (score + comments).  Both contribute.
            raw = score + comments
            if raw <= 0.0:
                return 1.0
            return math.log1p(raw)

        if metric in ("x", "x_twitter", "twitter"):
            retweets = _to_float(md.get("x_retweet_count")) or 0.0
            likes = _to_float(md.get("x_like_count")) or 0.0
            replies = _to_float(md.get("x_reply_count")) or 0.0
            raw = retweets + likes + replies
            if raw <= 0.0:
                return 1.0
            return math.log1p(raw)

        # Unknown explicit metric — default to 1.0
        return 1.0

    def _weighted_mean(
        self,
        scores: list[tuple[float, float]],
    ) -> float:
        """Compute the weighted mean of ``(score, weight)`` pairs.

        Weights are normalized so the max is 1.0 before averaging.
        Returns 0.0 if there are no items (or total weight is zero).
        """
        if not scores:
            return 0.0
        max_weight = max(w for _, w in scores)
        if max_weight <= 0.0:
            # All zero weights — fall back to unweighted mean
            return round(sum(s for s, _ in scores) / len(scores), 6)
        normalized = [(s, w / max_weight) for s, w in scores]
        total_weight = sum(w for _, w in normalized)
        if total_weight <= 0.0:
            return 0.0
        weighted_sum = sum(s * w for s, w in normalized)
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
        """Compute engagement-weighted sentiment features.

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
                # Per-event-type engagement-weighted mean sentiment
                for et in self.event_types:
                    et_scores: list[tuple[float, float]] = []
                    for i in window_items:
                        if i.event_type != et:
                            continue
                        if i.item_id not in sentiment_by_id:
                            continue
                        weight = self._engagement_score(i)
                        et_scores.append((sentiment_by_id[i.item_id], weight))
                    features[f"sent_eng_{et}"] = self._weighted_mean(et_scores)

                # Aggregate engagement-weighted mean sentiment
                all_scores: list[tuple[float, float]] = []
                for i in window_items:
                    if i.item_id not in sentiment_by_id:
                        continue
                    weight = self._engagement_score(i)
                    all_scores.append((sentiment_by_id[i.item_id], weight))
                features["sent_eng_mean"] = self._weighted_mean(all_scores)

                sym_result[dt] = features

            if sym_result:
                result[sym] = sym_result

        return result


__all__ = ["EngagementWeightedFeatures", "DEFAULT_EVENT_TYPES"]
