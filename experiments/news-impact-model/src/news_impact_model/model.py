from __future__ import annotations

from statistics import median

from news_impact_model.analogs import HistoricalAnalogIndex
from news_impact_model.schema import (
    AnalogMatch,
    HorizonImpact,
    MarketContext,
    NewsEvent,
    NewsImpactPrediction,
    SimilarEventSummary,
)


class NewsImpactModel:
    """Raw news-to-market-impact predictor.

    This is a deterministic, analog-based baseline.  It is deliberately shaped
    like the production model: event/context in, multi-horizon impact
    distribution out.  Later implementations can replace the internals with a
    text transformer, market-context encoder, and learned fusion layer while
    preserving the output contract.
    """

    def __init__(
        self,
        *,
        index: HistoricalAnalogIndex,
        horizons: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "1d"),
        top_k: int = 25,
    ) -> None:
        self._index = index
        self._horizons = horizons
        self._top_k = top_k

    def predict(
        self,
        event: NewsEvent,
        context: MarketContext,
    ) -> NewsImpactPrediction:
        matches = self._index.search(event, context, top_k=self._top_k)
        horizons = {
            horizon: self._impact_for_horizon(matches, horizon)
            for horizon in self._horizons
        }
        return NewsImpactPrediction(
            event_id=event.event_id,
            symbol=context.symbol,
            event_type=event.event_type,
            horizons=horizons,
            volatility_impact=_weighted_mean(
                [(m.outcome.volatility_impact, m.score) for m in matches]
            ),
            volume_impact=_weighted_mean(
                [(m.outcome.volume_impact, m.score) for m in matches]
            ),
            confidence=self._confidence(matches),
            similar_events=[
                SimilarEventSummary(
                    event_id=m.outcome.event_id,
                    source=m.outcome.source,
                    headline=m.outcome.headline,
                    event_type=m.outcome.event_type,
                    score=round(m.score, 6),
                    abnormal_returns=dict(m.outcome.abnormal_returns),
                )
                for m in matches[:5]
            ],
        )

    @staticmethod
    def _impact_for_horizon(
        matches: list[AnalogMatch],
        horizon: str,
    ) -> HorizonImpact:
        observations = [
            (match.outcome.abnormal_returns[horizon], match.score)
            for match in matches
            if horizon in match.outcome.abnormal_returns
        ]
        if not observations:
            return HorizonImpact(
                expected_return=0.0,
                p_up=0.5,
                q10=0.0,
                q50=0.0,
                q90=0.0,
                sample_size=0,
            )

        values = [value for value, _ in observations]
        expected = _weighted_mean(observations)
        up_weight = sum(weight for value, weight in observations if value > 0)
        total_weight = sum(weight for _, weight in observations)
        p_up = up_weight / total_weight if total_weight > 0 else 0.5
        return HorizonImpact(
            expected_return=expected,
            p_up=p_up,
            q10=_quantile(values, 0.10),
            q50=median(values),
            q90=_quantile(values, 0.90),
            sample_size=len(values),
        )

    @staticmethod
    def _confidence(matches: list[AnalogMatch]) -> float:
        if not matches:
            return 0.05
        top = matches[0].score
        coverage = min(1.0, len(matches) / 10.0)
        text_quality = sum(m.text_overlap for m in matches[:5]) / min(5, len(matches))
        confidence = 0.20 + 0.45 * min(1.0, top) + 0.20 * coverage + 0.15 * text_quality
        return max(0.05, min(0.98, confidence))


def _weighted_mean(observations: list[tuple[float, float]]) -> float:
    if not observations:
        return 0.0
    total_weight = sum(weight for _, weight in observations)
    if total_weight <= 0:
        return sum(value for value, _ in observations) / len(observations)
    return sum(value * weight for value, weight in observations) / total_weight


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = pos - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction
