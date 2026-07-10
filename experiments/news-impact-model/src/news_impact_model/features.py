from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass

from news_impact_model.schema import AnalogMatch, HistoricalOutcome, MarketContext, NewsEvent
from news_impact_model.text import tokenize


@dataclass(frozen=True)
class HashingTextEmbedder:
    """Dependency-free financial text encoder baseline.

    This is not a substitute for FinBERT or a production LLM encoder. It gives
    the experiment a stable vector contract so retrieval, evaluation, and
    export paths can be built before heavyweight model dependencies land.
    """

    dimensions: int = 256

    def embed(self, text: str) -> tuple[float, ...]:
        if self.dimensions <= 0:
            raise ValueError("dimensions must be positive")
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            return tuple(vector)
        return tuple(value / norm for value in vector)


class VectorAnalogIndex:
    """In-memory vector retrieval scaffold for historical analogs."""

    def __init__(self, *, embedder: HashingTextEmbedder | None = None) -> None:
        self._embedder = embedder or HashingTextEmbedder()
        self._rows: list[tuple[HistoricalOutcome, tuple[float, ...]]] = []

    def add(self, outcome: HistoricalOutcome) -> None:
        self._rows.append((outcome, self._embedder.embed(outcome.text)))

    def extend(self, outcomes: Iterable[HistoricalOutcome]) -> None:
        for outcome in outcomes:
            self.add(outcome)

    def search(
        self,
        event: NewsEvent,
        context: MarketContext,
        *,
        top_k: int = 20,
    ) -> list[AnalogMatch]:
        event_vector = self._embedder.embed(event.text)
        matches = [
            self._score(outcome, vector, event=event, context=context, event_vector=event_vector)
            for outcome, vector in self._rows
        ]
        matches.sort(key=lambda match: match.score, reverse=True)
        return matches[:top_k]

    def _score(
        self,
        outcome: HistoricalOutcome,
        vector: tuple[float, ...],
        *,
        event: NewsEvent,
        context: MarketContext,
        event_vector: tuple[float, ...],
    ) -> AnalogMatch:
        similarity = cosine_similarity(event_vector, vector)
        symbol_match = context.symbol in outcome.symbols or any(
            symbol in outcome.symbols for symbol in event.symbols
        )
        event_type_match = event.event_type == outcome.event_type
        regime_match = (
            context.market_regime != "unknown" and context.market_regime == outcome.market_regime
        )
        score = (
            0.58 * similarity
            + 0.18 * float(symbol_match)
            + 0.16 * float(event_type_match)
            + 0.08 * float(regime_match)
        )
        return AnalogMatch(
            outcome=outcome,
            score=max(0.0, score),
            text_overlap=similarity,
            symbol_match=symbol_match,
            event_type_match=event_type_match,
            regime_match=regime_match,
        )


def encode_market_context(context: MarketContext) -> dict[str, float]:
    """Encode pre-news market state for tabular models."""

    features = {
        "pre_event_return": float(context.pre_event_return or 0.0),
        "realized_volatility": float(context.realized_volatility or 0.0),
        "relative_volume": float(context.relative_volume or 0.0),
        "spread_bps": float(context.spread_bps or 0.0),
        "liquidity_score": float(context.liquidity_score or 0.0),
    }
    features[f"market_regime:{context.market_regime or 'unknown'}"] = 1.0
    return features


def event_surprise_features(
    *,
    similarity_scores: Iterable[float],
    source_event_count: int,
    event_type_count: int,
) -> dict[str, float]:
    """Transparent novelty/surprise features before learned heads exist."""

    scores = [max(0.0, min(1.0, value)) for value in similarity_scores]
    max_similarity = max(scores, default=0.0)
    return {
        "max_similarity": max_similarity,
        "mean_similarity": sum(scores) / len(scores) if scores else 0.0,
        "novelty_score": round(1.0 - max_similarity, 12),
        "source_event_rarity": 1.0 / (max(0, source_event_count) + 1),
        "event_type_rarity": 1.0 / (max(0, event_type_count) + 1),
    }


def cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have the same dimensionality")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
