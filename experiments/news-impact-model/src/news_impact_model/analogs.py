from __future__ import annotations

import math
from dataclasses import dataclass

from news_impact_model.schema import AnalogMatch, HistoricalOutcome, MarketContext, NewsEvent
from news_impact_model.text import weighted_jaccard

DEFAULT_SOURCE_CREDIBILITY = {
    "reuters": 1.0,
    "bloomberg": 1.0,
    "dow_jones": 0.95,
    "benzinga": 0.82,
    "marketwatch": 0.72,
    "newsapi": 0.55,
    "unknown_blog": 0.25,
}


@dataclass(frozen=True)
class AnalogScoringWeights:
    """Weights used to rank historical analogs.

    The defaults preserve the original hand-tuned baseline.  Training can
    provide alternate weights learned from historical prediction error.
    """

    text: float = 0.34
    symbol: float = 0.22
    event_type: float = 0.18
    regime: float = 0.10
    source: float = 0.10
    recency: float = 0.06


class HistoricalAnalogIndex:
    """In-memory retrieval layer for similar historical news outcomes.

    The production version should use a vector index backed by stored text
    embeddings.  This baseline intentionally uses transparent scoring so the
    scaffold is deterministic, dependency-free, and testable.
    """

    def __init__(
        self,
        *,
        source_credibility: dict[str, float] | None = None,
        weights: AnalogScoringWeights | None = None,
    ) -> None:
        self._outcomes: list[HistoricalOutcome] = []
        self._source_credibility = {
            **DEFAULT_SOURCE_CREDIBILITY,
            **(source_credibility or {}),
        }
        self._weights = weights or AnalogScoringWeights()

    def add(self, outcome: HistoricalOutcome) -> None:
        self._outcomes.append(outcome)

    def extend(self, outcomes: list[HistoricalOutcome]) -> None:
        self._outcomes.extend(outcomes)

    def search(
        self,
        event: NewsEvent,
        context: MarketContext,
        *,
        top_k: int = 20,
    ) -> list[AnalogMatch]:
        scored = [self._score(outcome, event=event, context=context) for outcome in self._outcomes]
        scored = [m for m in scored if m.score > 0]
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:top_k]

    def _score(
        self,
        outcome: HistoricalOutcome,
        *,
        event: NewsEvent,
        context: MarketContext,
    ) -> AnalogMatch:
        text_overlap = weighted_jaccard(event.text, outcome.text)
        symbol_match = context.symbol in outcome.symbols or any(
            symbol in outcome.symbols for symbol in event.symbols
        )
        event_type_match = event.event_type == outcome.event_type
        regime_match = (
            context.market_regime != "unknown" and context.market_regime == outcome.market_regime
        )
        source_quality = self._source_credibility.get(outcome.source.lower(), 0.45)
        age_ns = max(0, event.available_at_ns - outcome.available_at_ns)
        age_days = age_ns / 1_000_000_000 / 86_400
        recency = math.exp(-age_days / 730) if age_days else 1.0

        score = (
            self._weights.text * text_overlap
            + self._weights.symbol * float(symbol_match)
            + self._weights.event_type * float(event_type_match)
            + self._weights.regime * float(regime_match)
            + self._weights.source * source_quality
            + self._weights.recency * recency
        )
        return AnalogMatch(
            outcome=outcome,
            score=score,
            text_overlap=text_overlap,
            symbol_match=symbol_match,
            event_type_match=event_type_match,
            regime_match=regime_match,
        )
