"""Experimental NewsImpactModel scaffold.

This package is intentionally isolated under ``experiments/`` and is not
imported by the main Fincept runtime.  It provides the contracts and
baseline logic for predicting raw market impact from newly arriving news.
"""

from news_impact_model.analogs import AnalogScoringWeights, HistoricalAnalogIndex
from news_impact_model.data import (
    load_historical_outcomes,
    write_historical_outcomes_jsonl,
)
from news_impact_model.labels import label_event_impact
from news_impact_model.model import NewsImpactModel
from news_impact_model.schema import (
    HistoricalOutcome,
    HorizonImpact,
    MarketContext,
    NewsEvent,
    NewsImpactPrediction,
    PricePoint,
)
from news_impact_model.training import (
    AnalogWeightEvaluation,
    AnalogWeightOptimizationResult,
    WalkForwardEvaluation,
    WalkForwardFold,
    WalkForwardOptimizationResult,
    evaluate_analog_weights,
    optimize_analog_weights,
    walk_forward_evaluate_analog_weights,
    walk_forward_optimize_analog_weights,
)
from news_impact_model.workbench import WorkbenchState

__all__ = [
    "AnalogScoringWeights",
    "AnalogWeightEvaluation",
    "AnalogWeightOptimizationResult",
    "WalkForwardEvaluation",
    "WalkForwardFold",
    "WalkForwardOptimizationResult",
    "HistoricalAnalogIndex",
    "HistoricalOutcome",
    "HorizonImpact",
    "load_historical_outcomes",
    "MarketContext",
    "NewsEvent",
    "NewsImpactModel",
    "NewsImpactPrediction",
    "PricePoint",
    "evaluate_analog_weights",
    "label_event_impact",
    "optimize_analog_weights",
    "walk_forward_evaluate_analog_weights",
    "walk_forward_optimize_analog_weights",
    "WorkbenchState",
    "write_historical_outcomes_jsonl",
]
