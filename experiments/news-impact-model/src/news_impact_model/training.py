from __future__ import annotations

from dataclasses import dataclass
from math import inf
from statistics import mean

from news_impact_model.analogs import AnalogScoringWeights, HistoricalAnalogIndex
from news_impact_model.model import NewsImpactModel
from news_impact_model.schema import HistoricalOutcome, MarketContext, NewsEvent


@dataclass(frozen=True)
class PriorBucket:
    """Simple source/event prior learned from historical outcomes."""

    key: tuple[str, str]
    count: int
    mean_abnormal_return: dict[str, float]
    mean_volatility_impact: float
    mean_volume_impact: float


@dataclass(frozen=True)
class AnalogWeightEvaluation:
    """Out-of-sample score for one analog-weight set."""

    weights: AnalogScoringWeights
    horizon: str
    n_predictions: int
    mae: float
    directional_accuracy: float


@dataclass(frozen=True)
class AnalogWeightOptimizationResult:
    """Best weight set and its validation score."""

    weights: AnalogScoringWeights
    evaluation: AnalogWeightEvaluation
    candidates_tested: int


@dataclass(frozen=True)
class WalkForwardFold:
    """One chronological out-of-sample analog prediction."""

    target_event_id: str
    train_event_ids: tuple[str, ...]
    predicted: float
    actual: float
    abs_error: float
    direction_hit: bool


@dataclass(frozen=True)
class WalkForwardEvaluation:
    """Time-ordered score for one analog-weight set."""

    weights: AnalogScoringWeights
    horizon: str
    n_predictions: int
    mae: float
    directional_accuracy: float
    folds: tuple[WalkForwardFold, ...]


@dataclass(frozen=True)
class WalkForwardOptimizationResult:
    """Best weight set selected by chronological validation."""

    weights: AnalogScoringWeights
    evaluation: WalkForwardEvaluation
    candidates_tested: int


def fit_source_event_priors(outcomes: list[HistoricalOutcome]) -> list[PriorBucket]:
    """Estimate transparent priors by (source, event_type).

    This is not the final training stack.  It provides a small, auditable
    baseline and a useful feature for later GBM/transformer heads.
    """
    grouped: dict[tuple[str, str], list[HistoricalOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault((outcome.source.lower(), outcome.event_type), []).append(outcome)

    buckets: list[PriorBucket] = []
    for key, rows in sorted(grouped.items()):
        horizons = sorted({h for row in rows for h in row.abnormal_returns})
        buckets.append(
            PriorBucket(
                key=key,
                count=len(rows),
                mean_abnormal_return={
                    horizon: mean(
                        row.abnormal_returns[horizon]
                        for row in rows
                        if horizon in row.abnormal_returns
                    )
                    for horizon in horizons
                },
                mean_volatility_impact=mean(row.volatility_impact for row in rows),
                mean_volume_impact=mean(row.volume_impact for row in rows),
            )
        )
    return buckets


def evaluate_analog_weights(
    outcomes: list[HistoricalOutcome],
    *,
    weights: AnalogScoringWeights,
    horizon: str,
    top_k: int = 5,
) -> AnalogWeightEvaluation:
    """Leave-one-out evaluation for the analog baseline.

    Each historical event is scored as if it were new, using every other
    historical event as the analog index.  This gives a quick local measure of
    whether a scoring-weight set predicts realized abnormal return.
    """
    errors: list[float] = []
    direction_hits = 0
    for target in outcomes:
        if horizon not in target.abnormal_returns:
            continue
        history = [row for row in outcomes if row.event_id != target.event_id]
        if not history:
            continue
        index = HistoricalAnalogIndex(weights=weights)
        index.extend(history)
        model = NewsImpactModel(index=index, horizons=(horizon,), top_k=top_k)
        symbol = target.symbols[0] if target.symbols else ""
        prediction = model.predict(
            NewsEvent(
                event_id=target.event_id,
                available_at_ns=target.available_at_ns,
                source=target.source,
                headline=target.headline,
                body=target.body,
                symbols=target.symbols,
                event_type=target.event_type,
            ),
            MarketContext(symbol=symbol, market_regime=target.market_regime),
        )
        predicted = prediction.horizons[horizon].expected_return
        actual = target.abnormal_returns[horizon]
        errors.append(abs(predicted - actual))
        if _same_direction(predicted, actual):
            direction_hits += 1

    n_predictions = len(errors)
    return AnalogWeightEvaluation(
        weights=weights,
        horizon=horizon,
        n_predictions=n_predictions,
        mae=mean(errors) if errors else inf,
        directional_accuracy=direction_hits / n_predictions if n_predictions else 0.0,
    )


def optimize_analog_weights(
    outcomes: list[HistoricalOutcome],
    *,
    horizon: str,
    candidates: tuple[AnalogScoringWeights, ...] | None = None,
    top_k: int = 5,
) -> AnalogWeightOptimizationResult:
    """Pick the analog scoring weights with lowest leave-one-out MAE."""
    candidate_weights = candidates or _default_weight_grid()
    evaluations = [
        evaluate_analog_weights(
            outcomes,
            weights=weights,
            horizon=horizon,
            top_k=top_k,
        )
        for weights in candidate_weights
    ]
    if not evaluations:
        raise ValueError("at least one candidate weight set is required")
    best = min(
        evaluations,
        key=lambda item: (item.mae, -item.directional_accuracy),
    )
    return AnalogWeightOptimizationResult(
        weights=best.weights,
        evaluation=best,
        candidates_tested=len(evaluations),
    )


def walk_forward_evaluate_analog_weights(
    outcomes: list[HistoricalOutcome],
    *,
    weights: AnalogScoringWeights,
    horizon: str,
    min_train_events: int = 10,
    top_k: int = 5,
) -> WalkForwardEvaluation:
    """Evaluate weights using only events before each target event.

    This is stricter than leave-one-out and should be preferred once there are
    enough historical labels.  It sorts events by availability time, trains the
    analog index on prior events only, then predicts the next event.
    """
    if min_train_events < 1:
        raise ValueError("min_train_events must be >= 1")
    ordered = sorted(outcomes, key=lambda row: (row.available_at_ns, row.event_id))
    folds: list[WalkForwardFold] = []
    for idx, target in enumerate(ordered):
        if idx < min_train_events or horizon not in target.abnormal_returns:
            continue
        history = ordered[:idx]
        predicted = _predict_from_history(
            target,
            history=history,
            weights=weights,
            horizon=horizon,
            top_k=top_k,
        )
        actual = target.abnormal_returns[horizon]
        folds.append(
            WalkForwardFold(
                target_event_id=target.event_id,
                train_event_ids=tuple(row.event_id for row in history),
                predicted=predicted,
                actual=actual,
                abs_error=abs(predicted - actual),
                direction_hit=_same_direction(predicted, actual),
            )
        )

    n_predictions = len(folds)
    return WalkForwardEvaluation(
        weights=weights,
        horizon=horizon,
        n_predictions=n_predictions,
        mae=mean(fold.abs_error for fold in folds) if folds else inf,
        directional_accuracy=(
            sum(1 for fold in folds if fold.direction_hit) / n_predictions if n_predictions else 0.0
        ),
        folds=tuple(folds),
    )


def walk_forward_optimize_analog_weights(
    outcomes: list[HistoricalOutcome],
    *,
    horizon: str,
    candidates: tuple[AnalogScoringWeights, ...] | None = None,
    min_train_events: int = 10,
    top_k: int = 5,
) -> WalkForwardOptimizationResult:
    """Pick analog weights by chronological out-of-sample MAE."""
    candidate_weights = candidates or _default_weight_grid()
    evaluations = [
        walk_forward_evaluate_analog_weights(
            outcomes,
            weights=weights,
            horizon=horizon,
            min_train_events=min_train_events,
            top_k=top_k,
        )
        for weights in candidate_weights
    ]
    if not evaluations:
        raise ValueError("at least one candidate weight set is required")
    best = min(
        evaluations,
        key=lambda item: (item.mae, -item.directional_accuracy),
    )
    return WalkForwardOptimizationResult(
        weights=best.weights,
        evaluation=best,
        candidates_tested=len(evaluations),
    )


def _default_weight_grid() -> tuple[AnalogScoringWeights, ...]:
    """Small transparent candidate set before a real optimizer exists."""
    return (
        AnalogScoringWeights(),
        AnalogScoringWeights(
            text=0.45,
            symbol=0.20,
            event_type=0.20,
            regime=0.05,
            source=0.05,
            recency=0.05,
        ),
        AnalogScoringWeights(
            text=0.20,
            symbol=0.15,
            event_type=0.45,
            regime=0.10,
            source=0.05,
            recency=0.05,
        ),
        AnalogScoringWeights(
            text=0.20,
            symbol=0.15,
            event_type=0.25,
            regime=0.30,
            source=0.05,
            recency=0.05,
        ),
        AnalogScoringWeights(
            text=0.25,
            symbol=0.20,
            event_type=0.25,
            regime=0.10,
            source=0.15,
            recency=0.05,
        ),
    )


def _predict_from_history(
    target: HistoricalOutcome,
    *,
    history: list[HistoricalOutcome],
    weights: AnalogScoringWeights,
    horizon: str,
    top_k: int,
) -> float:
    index = HistoricalAnalogIndex(weights=weights)
    index.extend(history)
    model = NewsImpactModel(index=index, horizons=(horizon,), top_k=top_k)
    symbol = target.symbols[0] if target.symbols else ""
    prediction = model.predict(
        NewsEvent(
            event_id=target.event_id,
            available_at_ns=target.available_at_ns,
            source=target.source,
            headline=target.headline,
            body=target.body,
            symbols=target.symbols,
            event_type=target.event_type,
        ),
        MarketContext(symbol=symbol, market_regime=target.market_regime),
    )
    return prediction.horizons[horizon].expected_return


def _same_direction(predicted: float, actual: float) -> bool:
    if predicted == 0 and actual == 0:
        return True
    return (predicted > 0 and actual > 0) or (predicted < 0 and actual < 0)
