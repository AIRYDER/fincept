from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from statistics import mean

from news_impact_model.schema import HistoricalOutcome


@dataclass(frozen=True)
class WalkForwardSplit:
    """One purged chronological validation fold."""

    target: HistoricalOutcome
    train: tuple[HistoricalOutcome, ...]


@dataclass(frozen=True)
class HoldoutSplit:
    """Train/test rows for one categorical holdout."""

    train: tuple[HistoricalOutcome, ...]
    test: tuple[HistoricalOutcome, ...]


@dataclass(frozen=True)
class CalibrationBucket:
    """Observed outcome frequency for a predicted probability bucket."""

    bucket_index: int
    lower: float
    upper: float
    count: int
    mean_prediction: float
    observed_frequency: float


@dataclass(frozen=True)
class ImpactDecayScore:
    """Accuracy of multi-horizon impact magnitude and decay direction."""

    horizons: tuple[str, ...]
    mean_abs_error: float
    directional_hit: bool
    decay_shape_hit: bool


@dataclass(frozen=True)
class ErrorBucket:
    """Error summary grouped by event type and source."""

    event_type: str
    source: str
    count: int
    mae: float
    bias: float


def purged_walk_forward_splits(
    outcomes: list[HistoricalOutcome],
    *,
    min_train_events: int,
    purge_ns: int,
) -> list[WalkForwardSplit]:
    """Build chronological folds with a no-leakage gap before each target."""

    if min_train_events < 1:
        raise ValueError("min_train_events must be >= 1")
    if purge_ns < 0:
        raise ValueError("purge_ns must be >= 0")
    ordered = sorted(outcomes, key=lambda row: (row.available_at_ns, row.event_id))
    folds: list[WalkForwardSplit] = []
    for target in ordered:
        cutoff = target.available_at_ns - purge_ns
        train = tuple(row for row in ordered if row.available_at_ns < cutoff)
        if len(train) >= min_train_events:
            folds.append(WalkForwardSplit(target=target, train=train))
    return folds


def event_type_holdout_split(
    outcomes: list[HistoricalOutcome],
    *,
    holdout_event_type: str,
) -> HoldoutSplit:
    holdout = holdout_event_type.strip().lower()
    train = tuple(row for row in outcomes if row.event_type.lower() != holdout)
    test = tuple(row for row in outcomes if row.event_type.lower() == holdout)
    return HoldoutSplit(train=train, test=test)


def source_holdout_split(
    outcomes: list[HistoricalOutcome],
    *,
    holdout_source: str,
) -> HoldoutSplit:
    holdout = holdout_source.strip().lower()
    train = tuple(row for row in outcomes if row.source.lower() != holdout)
    test = tuple(row for row in outcomes if row.source.lower() == holdout)
    return HoldoutSplit(train=train, test=test)


def calibration_curve(
    predictions: list[tuple[float, float]],
    *,
    buckets: int = 10,
) -> list[CalibrationBucket]:
    """Bucket `p_up` predictions and compare against realized direction."""

    if buckets <= 0:
        raise ValueError("buckets must be positive")
    grouped: list[list[tuple[float, float]]] = [[] for _ in range(buckets)]
    for probability, actual_return in predictions:
        clipped = max(0.0, min(1.0, probability))
        index = min(buckets - 1, int(clipped * buckets))
        grouped[index].append((clipped, actual_return))

    curve: list[CalibrationBucket] = []
    for index, rows in enumerate(grouped):
        lower = index / buckets
        upper = (index + 1) / buckets
        if not rows:
            curve.append(
                CalibrationBucket(
                    bucket_index=index,
                    lower=lower,
                    upper=upper,
                    count=0,
                    mean_prediction=0.0,
                    observed_frequency=0.0,
                )
            )
            continue
        curve.append(
            CalibrationBucket(
                bucket_index=index,
                lower=lower,
                upper=upper,
                count=len(rows),
                mean_prediction=mean(probability for probability, _ in rows),
                observed_frequency=sum(1 for _, actual in rows if actual > 0.0) / len(rows),
            )
        )
    return curve


def impact_decay_accuracy(
    *,
    predicted: dict[str, float],
    actual: dict[str, float],
) -> ImpactDecayScore:
    """Compare multi-horizon impact magnitude, sign, and decay shape."""

    horizons = tuple(horizon for horizon in predicted if horizon in actual)
    if not horizons:
        raise ValueError("predicted and actual must share at least one horizon")
    errors = [abs(predicted[horizon] - actual[horizon]) for horizon in horizons]
    direction_hits = [_same_direction(predicted[horizon], actual[horizon]) for horizon in horizons]
    predicted_decay = [abs(predicted[horizon]) for horizon in horizons]
    actual_decay = [abs(actual[horizon]) for horizon in horizons]
    return ImpactDecayScore(
        horizons=horizons,
        mean_abs_error=round(mean(errors), 12),
        directional_hit=all(direction_hits),
        decay_shape_hit=_monotonic_direction(predicted_decay) == _monotonic_direction(actual_decay),
    )


def error_analysis_by_event_source(
    rows: list[tuple[HistoricalOutcome, float, float]],
) -> dict[tuple[str, str], ErrorBucket]:
    """Group prediction error by `(event_type, source)`."""

    grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for outcome, predicted, actual in rows:
        key = (outcome.event_type, outcome.source)
        grouped.setdefault(key, []).append((predicted, actual))

    return {
        key: ErrorBucket(
            event_type=key[0],
            source=key[1],
            count=len(values),
            mae=round(mean(abs(predicted - actual) for predicted, actual in values), 12),
            bias=round(mean(predicted - actual for predicted, actual in values), 12),
        )
        for key, values in sorted(grouped.items())
    }


def _same_direction(predicted: float, actual: float) -> bool:
    if predicted == 0.0 and actual == 0.0:
        return True
    return (predicted > 0.0 and actual > 0.0) or (predicted < 0.0 and actual < 0.0)


def _monotonic_direction(values: list[float]) -> str:
    if all(left >= right for left, right in pairwise(values)):
        return "decay"
    if all(left <= right for left, right in pairwise(values)):
        return "build"
    return "mixed"
