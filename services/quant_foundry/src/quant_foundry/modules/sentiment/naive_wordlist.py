"""
quant_foundry.modules.sentiment.naive_wordlist — zero-dependency sentiment baseline.

This is the fallback sentiment engine: a positive/negative word-list
scorer that produces a score in ``[-1, 1]`` with no external deps.  It
reuses the same word lists as
``quant_foundry.data_ingestion.news._sentiment_proxy`` so results are
consistent with the existing news ingestion pipeline.

This module is registered as ``sentiment:naive-wordlist:1.0.0``.
"""

from __future__ import annotations

from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)

# Reuse the same word lists as the existing news ingestion pipeline
# (data_ingestion/news.py) for consistency.
_POSITIVE_WORDS = frozenset(
    {
        "beat", "beats", "surge", "surges", "jump", "jumps", "rise", "rises",
        "gain", "gains", "profit", "profits", "raise", "raises", "upgrade",
        "outperform", "strong", "growth", "grow", "win", "wins", "approve",
        "approved", "launch", "unveil", "partner", "partnership", "record",
        "high", "boost", "boosts", "rally", "soar", "soars", "breakthrough",
    },
)
_NEGATIVE_WORDS = frozenset(
    {
        "miss", "misses", "fall", "falls", "drop", "drops", "cut", "cuts",
        "lower", "lowers", "loss", "losses", "downgrade", "weak", "decline",
        "declines", "sue", "sued", "sues", "lawsuit", "settlement", "probe",
        "investigation", "hack", "breach", "ban", "sanction", "recall",
        "halt", "delay", "fire", "fraud", "default", "bankrupt", "warning",
    },
)


@register_module(
    "sentiment",
    "naive-wordlist",
    "1.0.0",
    default_config={
        "positive_words": list(_POSITIVE_WORDS),
        "negative_words": list(_NEGATIVE_WORDS),
    },
)
class NaiveWordlistSentiment:
    """Naive word-list sentiment scorer.

    Scores each :class:`MediaItem` by counting positive/negative words
    in the headline + body.  Score = ``(pos - neg) / (pos + neg)`` in
    ``[-1, 1]``.  Confidence is ``min(1.0, total / 10.0)`` — more
    sentiment-bearing words = higher confidence.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._positive = frozenset(self.config.get("positive_words", _POSITIVE_WORDS))
        self._negative = frozenset(self.config.get("negative_words", _NEGATIVE_WORDS))

    def score(self, items: list[MediaItem]) -> list[SentimentResult]:
        results: list[SentimentResult] = []
        for item in items:
            words = item.text.lower().split()
            if not words:
                results.append(SentimentResult(
                    item_id=item.item_id,
                    provider="naive",
                    score=0.0,
                    confidence=0.0,
                ))
                continue
            pos = sum(1 for w in words if w.strip(".,!?;:\"'()[]") in self._positive)
            neg = sum(1 for w in words if w.strip(".,!?;:\"'()[]") in self._negative)
            total = pos + neg
            if total == 0:
                score = 0.0
                confidence = 0.0
            else:
                score = round((pos - neg) / total, 6)
                confidence = round(min(1.0, total / 10.0), 6)
            results.append(SentimentResult(
                item_id=item.item_id,
                provider="naive",
                score=score,
                confidence=confidence,
            ))
        return results


__all__ = ["NaiveWordlistSentiment"]
