from __future__ import annotations

from news_impact_model.analogs import HistoricalAnalogIndex
from news_impact_model.model import NewsImpactModel
from news_impact_model.schema import (
    HistoricalOutcome,
    MarketContext,
    NewsEvent,
    NewsImpactPrediction,
)


class NewsImpactPipeline:
    """Convenience facade for ingesting history and scoring new events."""

    def __init__(
        self,
        *,
        outcomes: list[HistoricalOutcome] | None = None,
        horizons: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "1d"),
    ) -> None:
        self.index = HistoricalAnalogIndex()
        if outcomes:
            self.index.extend(outcomes)
        self.model = NewsImpactModel(index=self.index, horizons=horizons)

    def add_outcome(self, outcome: HistoricalOutcome) -> None:
        self.index.add(outcome)

    def predict(
        self,
        event: NewsEvent,
        *,
        context: MarketContext,
    ) -> NewsImpactPrediction:
        return self.model.predict(event, context)
