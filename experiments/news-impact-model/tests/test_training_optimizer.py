from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.analogs import AnalogScoringWeights
from news_impact_model.schema import HistoricalOutcome
from news_impact_model.training import (
    evaluate_analog_weights,
    optimize_analog_weights,
    walk_forward_evaluate_analog_weights,
    walk_forward_optimize_analog_weights,
)


def _outcome(
    event_id: str,
    *,
    headline: str,
    event_type: str,
    return_5m: float,
    available_at_ns: int | None = None,
) -> HistoricalOutcome:
    return HistoricalOutcome(
        event_id=event_id,
        available_at_ns=available_at_ns
        if available_at_ns is not None
        else 1_700_000_000_000_000_000 + int(event_id[-1]) * 1_000,
        source="reuters",
        headline=headline,
        body="",
        symbols=("ACME",),
        event_type=event_type,
        market_regime="risk_on",
        abnormal_returns={"5m": return_5m},
    )


def test_evaluate_analog_weights_leave_one_out_excludes_target_event() -> None:
    outcomes = [
        _outcome(
            "hist-1",
            headline="Acme receives FDA approval for device",
            event_type="regulatory",
            return_5m=0.030,
        ),
        _outcome(
            "hist-2",
            headline="Acme wins regulator approval for product",
            event_type="regulatory",
            return_5m=0.026,
        ),
        _outcome(
            "hist-3",
            headline="Acme receives FDA lawsuit over device approval",
            event_type="litigation",
            return_5m=-0.030,
        ),
        _outcome(
            "hist-4",
            headline="Acme faces lawsuit after product approval",
            event_type="litigation",
            return_5m=-0.026,
        ),
    ]
    event_type_first = AnalogScoringWeights(
        text=0.0,
        symbol=0.0,
        event_type=1.0,
        regime=0.0,
        source=0.0,
        recency=0.0,
    )
    text_first = AnalogScoringWeights(
        text=1.0,
        symbol=0.0,
        event_type=0.0,
        regime=0.0,
        source=0.0,
        recency=0.0,
    )

    good = evaluate_analog_weights(
        outcomes,
        weights=event_type_first,
        horizon="5m",
    )
    bad = evaluate_analog_weights(
        outcomes,
        weights=text_first,
        horizon="5m",
    )

    assert good.n_predictions == 4
    assert good.mae < bad.mae
    assert good.directional_accuracy == 1.0


def test_optimize_analog_weights_picks_lowest_mae_candidate() -> None:
    outcomes = [
        _outcome(
            "hist-1",
            headline="Acme receives FDA approval for device",
            event_type="regulatory",
            return_5m=0.030,
        ),
        _outcome(
            "hist-2",
            headline="Acme wins regulator approval for product",
            event_type="regulatory",
            return_5m=0.026,
        ),
        _outcome(
            "hist-3",
            headline="Acme receives FDA lawsuit over device approval",
            event_type="litigation",
            return_5m=-0.030,
        ),
        _outcome(
            "hist-4",
            headline="Acme faces lawsuit after product approval",
            event_type="litigation",
            return_5m=-0.026,
        ),
    ]
    text_first = AnalogScoringWeights(
        text=1.0,
        symbol=0.0,
        event_type=0.0,
        regime=0.0,
        source=0.0,
        recency=0.0,
    )
    event_type_first = AnalogScoringWeights(
        text=0.0,
        symbol=0.0,
        event_type=1.0,
        regime=0.0,
        source=0.0,
        recency=0.0,
    )

    result = optimize_analog_weights(
        outcomes,
        horizon="5m",
        candidates=(text_first, event_type_first),
    )

    assert result.weights == event_type_first
    assert result.evaluation.directional_accuracy == 1.0
    assert (
        result.evaluation.mae
        < evaluate_analog_weights(
            outcomes,
            weights=text_first,
            horizon="5m",
        ).mae
    )


def test_walk_forward_evaluation_uses_only_prior_events() -> None:
    weights = AnalogScoringWeights(
        text=0.0,
        symbol=0.0,
        event_type=1.0,
        regime=0.0,
        source=0.0,
        recency=0.0,
    )
    outcomes = [
        _outcome(
            "future-4",
            headline="Acme regulatory approval later disappoints",
            event_type="regulatory",
            return_5m=-0.040,
            available_at_ns=400,
        ),
        _outcome(
            "old-1",
            headline="Acme regulatory approval lifts shares",
            event_type="regulatory",
            return_5m=0.020,
            available_at_ns=100,
        ),
        _outcome(
            "old-2",
            headline="Acme approval catalyst supports rally",
            event_type="regulatory",
            return_5m=0.030,
            available_at_ns=200,
        ),
        _outcome(
            "target-3",
            headline="Acme receives regulatory approval",
            event_type="regulatory",
            return_5m=0.025,
            available_at_ns=300,
        ),
    ]

    evaluation = walk_forward_evaluate_analog_weights(
        outcomes,
        weights=weights,
        horizon="5m",
        min_train_events=2,
    )

    assert evaluation.n_predictions == 2
    first_fold = evaluation.folds[0]
    assert first_fold.target_event_id == "target-3"
    assert first_fold.train_event_ids == ("old-1", "old-2")
    assert "future-4" not in first_fold.train_event_ids
    assert first_fold.predicted > 0


def test_walk_forward_optimizer_selects_candidate_using_time_ordered_error() -> None:
    outcomes = [
        _outcome(
            "hist-1",
            headline="Acme receives FDA approval for device",
            event_type="regulatory",
            return_5m=0.030,
            available_at_ns=100,
        ),
        _outcome(
            "hist-2",
            headline="Acme wins regulator approval for product",
            event_type="regulatory",
            return_5m=0.026,
            available_at_ns=200,
        ),
        _outcome(
            "hist-3",
            headline="Acme receives FDA lawsuit over device approval",
            event_type="litigation",
            return_5m=-0.030,
            available_at_ns=300,
        ),
        _outcome(
            "hist-4",
            headline="Acme faces lawsuit after product approval",
            event_type="litigation",
            return_5m=-0.026,
            available_at_ns=400,
        ),
    ]
    text_first = AnalogScoringWeights(
        text=1.0,
        symbol=0.0,
        event_type=0.0,
        regime=0.0,
        source=0.0,
        recency=0.0,
    )
    event_type_first = AnalogScoringWeights(
        text=0.0,
        symbol=0.0,
        event_type=1.0,
        regime=0.0,
        source=0.0,
        recency=0.0,
    )

    result = walk_forward_optimize_analog_weights(
        outcomes,
        horizon="5m",
        candidates=(text_first, event_type_first),
        min_train_events=2,
    )

    assert result.weights == event_type_first
    assert result.evaluation.n_predictions == 2
    assert (
        result.evaluation.mae
        < walk_forward_evaluate_analog_weights(
            outcomes,
            weights=text_first,
            horizon="5m",
            min_train_events=2,
        ).mae
    )
