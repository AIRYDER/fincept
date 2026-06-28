"""
quant_foundry.modules.sentiment.naive_wordlist — zero-dependency sentiment baseline.

This is the fallback sentiment engine: a positive/negative word-list
scorer that produces a score in ``[-1, 1]`` with no external deps.  It
reuses the same word lists as
``quant_foundry.data_ingestion.news._sentiment_proxy`` so results are
consistent with the existing news ingestion pipeline.

Two modules are registered here:

- ``sentiment:naive-wordlist:1.0.0`` — the original English-only scorer
  (kept for backward compatibility).
- ``sentiment:naive-wordlist-ml:1.0.0`` — the multilingual scorer that
  detects each item's language via :func:`detect_language` and uses the
  appropriate word list from :data:`MULTILINGUAL_WORDLISTS`, falling
  back to English for unsupported languages.  The detected language is
  recorded in the :class:`SentimentResult` ``metadata``.
"""

from __future__ import annotations

from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)
from quant_foundry.modules.sentiment.language import (
    DEFAULT_LANGUAGE,
    MULTILINGUAL_WORDLISTS,
    detect_language,
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


def _score_text(
    text: str,
    positive: frozenset[str],
    negative: frozenset[str],
) -> tuple[float, float]:
    """Score a single text string with the given word lists.

    Returns ``(score, confidence)``.  Score = ``(pos - neg) / (pos + neg)``
    in ``[-1, 1]``.  Confidence = ``min(1.0, total / 10.0)``.
    """
    words = text.lower().split()
    if not words:
        return 0.0, 0.0
    pos = sum(1 for w in words if w.strip(".,!?;:\"'()[]") in positive)
    neg = sum(1 for w in words if w.strip(".,!?;:\"'()[]") in negative)
    total = pos + neg
    if total == 0:
        return 0.0, 0.0
    score = round((pos - neg) / total, 6)
    confidence = round(min(1.0, total / 10.0), 6)
    return score, confidence


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
            score, confidence = _score_text(item.text, self._positive, self._negative)
            results.append(SentimentResult(
                item_id=item.item_id,
                provider="naive",
                score=score,
                confidence=confidence,
            ))
        return results


@register_module(
    "sentiment",
    "naive-wordlist-ml",
    "1.0.0",
    default_config={
        # Per-language word lists are sourced from MULTILINGUAL_WORDLISTS
        # at score time; these overrides let callers customize a single
        # language's lists if needed.
        "wordlists": {},
        "fallback_language": DEFAULT_LANGUAGE,
    },
)
class NaiveWordlistMultilingualSentiment:
    """Multilingual naive word-list sentiment scorer.

    Detects the language of each :class:`MediaItem` via
    :func:`detect_language` and scores it with the matching word list
    from :data:`MULTILINGUAL_WORDLISTS`.  Falls back to English
    (``fallback_language``) for unsupported languages.  The detected
    language is recorded in :attr:`SentimentResult.metadata` under the
    ``"language"`` key.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._overrides: dict[str, dict[str, list[str]]] = self.config.get(
            "wordlists", {},
        )
        self._fallback_language: str = self.config.get(
            "fallback_language", DEFAULT_LANGUAGE,
        )
        # Pre-build frozensets for each supported language.
        self._wordsets: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        for lang, lists in MULTILINGUAL_WORDLISTS.items():
            override = self._overrides.get(lang)
            pos = override.get("positive", lists["positive"]) if override else lists["positive"]
            neg = override.get("negative", lists["negative"]) if override else lists["negative"]
            self._wordsets[lang] = (frozenset(pos), frozenset(neg))

    def _wordsets_for(self, language: str) -> tuple[frozenset[str], frozenset[str]]:
        """Return the (positive, negative) word sets for a language.

        Falls back to ``fallback_language`` (English by default) if the
        language is not supported.
        """
        if language in self._wordsets:
            return self._wordsets[language]
        return self._wordsets.get(
            self._fallback_language, self._wordsets[DEFAULT_LANGUAGE],
        )

    def score(self, items: list[MediaItem]) -> list[SentimentResult]:
        results: list[SentimentResult] = []
        for item in items:
            language = detect_language(item.text)
            positive, negative = self._wordsets_for(language)
            score, confidence = _score_text(item.text, positive, negative)
            results.append(SentimentResult(
                item_id=item.item_id,
                provider="naive-ml",
                score=score,
                confidence=confidence,
                metadata={"language": language},
            ))
        return results


__all__ = [
    "NaiveWordlistMultilingualSentiment",
    "NaiveWordlistSentiment",
]

