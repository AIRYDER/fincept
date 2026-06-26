from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.evaluation import (  # noqa: E402
    calibration_curve,
    error_analysis_by_event_source,
    event_type_holdout_split,
    impact_decay_accuracy,
    purged_walk_forward_splits,
    source_holdout_split,
)
from news_impact_model.features import (  # noqa: E402
    HashingTextEmbedder,
    VectorAnalogIndex,
    encode_market_context,
    event_surprise_features,
)
from news_impact_model.schema import HistoricalOutcome, MarketContext, NewsEvent  # noqa: E402


def _outcome(
    event_id: str,
    *,
    ts: int,
    source: str = "reuters",
    event_type: str = "guidance",
    returns: dict[str, float] | None = None,
) -> HistoricalOutcome:
    return HistoricalOutcome(
        event_id=event_id,
        available_at_ns=ts,
        source=source,
        headline=f"Acme {event_type} update {event_id}",
        body="Demand and management commentary moved expectations.",
        symbols=("ACME",),
        event_type=event_type,
        market_regime="risk_on",
        abnormal_returns=returns or {"5m": 0.01, "30m": 0.02},
    )


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashingTextEmbedder(dimensions=16)

    first = embedder.embed("Acme raises guidance after strong demand")
    second = embedder.embed("Acme raises guidance after strong demand")

    assert first == second
    assert round(sum(value * value for value in first), 6) == 1.0


def test_vector_analog_index_prefers_embedding_similarity() -> None:
    index = VectorAnalogIndex(embedder=HashingTextEmbedder(dimensions=64))
    strong = _outcome("hist-1", ts=100, event_type="guidance")
    weak = _outcome("hist-2", ts=200, event_type="security")
    index.extend([weak, strong])

    matches = index.search(
        NewsEvent(
            event_id="new-1",
            available_at_ns=300,
            source="reuters",
            headline="Acme raises guidance after demand jump",
            symbols=("ACME",),
            event_type="guidance",
        ),
        MarketContext(symbol="ACME", market_regime="risk_on"),
        top_k=2,
    )

    assert [match.outcome.event_id for match in matches] == ["hist-1", "hist-2"]
    assert matches[0].text_overlap > matches[1].text_overlap


def test_market_context_encoder_and_surprise_features_are_stable() -> None:
    context_features = encode_market_context(
        MarketContext(
            symbol="ACME",
            market_regime="risk_on",
            pre_event_return=0.02,
            realized_volatility=0.4,
            relative_volume=1.8,
            spread_bps=12.0,
            liquidity_score=0.7,
        )
    )
    novelty = event_surprise_features(
        similarity_scores=[0.90, 0.40, 0.10],
        source_event_count=4,
        event_type_count=10,
    )

    assert context_features["pre_event_return"] == 0.02
    assert context_features["market_regime:risk_on"] == 1.0
    assert novelty["novelty_score"] == 0.10
    assert novelty["source_event_rarity"] == 0.2
    assert novelty["event_type_rarity"] == 1 / 11


def test_purged_walk_forward_splits_exclude_future_and_nearby_events() -> None:
    rows = [
        _outcome("e1", ts=100),
        _outcome("e2", ts=200),
        _outcome("e3", ts=300),
        _outcome("e4", ts=500),
    ]

    folds = purged_walk_forward_splits(rows, min_train_events=1, purge_ns=100)

    assert [(f.target.event_id, [r.event_id for r in f.train]) for f in folds] == [
        ("e3", ["e1"]),
        ("e4", ["e1", "e2", "e3"]),
    ]


def test_event_type_and_source_holdout_splits() -> None:
    rows = [
        _outcome("g1", ts=100, event_type="guidance", source="reuters"),
        _outcome("g2", ts=200, event_type="guidance", source="benzinga"),
        _outcome("s1", ts=300, event_type="security", source="reuters"),
    ]

    type_split = event_type_holdout_split(rows, holdout_event_type="guidance")
    source_split = source_holdout_split(rows, holdout_source="reuters")

    assert [row.event_id for row in type_split.train] == ["s1"]
    assert [row.event_id for row in type_split.test] == ["g1", "g2"]
    assert [row.event_id for row in source_split.train] == ["g2"]
    assert [row.event_id for row in source_split.test] == ["g1", "s1"]


def test_calibration_impact_decay_and_error_analysis() -> None:
    calibration = calibration_curve(
        [
            (0.10, -0.01),
            (0.40, 0.02),
            (0.70, 0.03),
            (0.90, -0.02),
        ],
        buckets=2,
    )
    decay = impact_decay_accuracy(
        predicted={"5m": 0.03, "30m": 0.01},
        actual={"5m": 0.02, "30m": 0.015},
    )
    errors = error_analysis_by_event_source(
        [
            (_outcome("r1", ts=100, source="reuters", event_type="guidance"), 0.01, 0.03),
            (_outcome("b1", ts=200, source="benzinga", event_type="security"), -0.02, -0.01),
            (_outcome("r2", ts=300, source="reuters", event_type="guidance"), 0.04, 0.01),
        ]
    )

    assert calibration[0].observed_frequency == 0.5
    assert calibration[1].observed_frequency == 0.5
    assert decay.directional_hit is True
    assert decay.mean_abs_error == 0.0075
    assert errors[("guidance", "reuters")].count == 2
    assert errors[("guidance", "reuters")].mae == 0.025
