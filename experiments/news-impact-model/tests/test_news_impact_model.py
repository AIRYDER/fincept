from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.analogs import HistoricalAnalogIndex
from news_impact_model.labels import label_event_impact
from news_impact_model.model import NewsImpactModel
from news_impact_model.schema import (
    HistoricalOutcome,
    MarketContext,
    NewsEvent,
    PricePoint,
)


def test_analog_retrieval_prefers_matching_event_type_symbol_and_text() -> None:
    index = HistoricalAnalogIndex()
    target = NewsEvent(
        event_id="n1",
        available_at_ns=1_700_000_000_000_000_000,
        source="benzinga",
        headline="Acme receives FDA approval for cardiac device",
        body="The company said the FDA approval unlocks commercial launch.",
        symbols=("ACME",),
        event_type="regulatory",
    )
    strong_match = HistoricalOutcome(
        event_id="h1",
        available_at_ns=1_699_000_000_000_000_000,
        source="benzinga",
        headline="Beta receives FDA approval for medical device",
        body="FDA approval allows the commercial launch to start.",
        symbols=("ACME",),
        event_type="regulatory",
        market_regime="risk_on",
        abnormal_returns={"5m": 0.021, "30m": 0.033},
        volatility_impact=0.24,
        volume_impact=0.70,
    )
    weak_match = HistoricalOutcome(
        event_id="h2",
        available_at_ns=1_698_000_000_000_000_000,
        source="unknown_blog",
        headline="Retail traders discuss Acme price chart",
        body="The article does not describe a regulatory catalyst.",
        symbols=("ACME",),
        event_type="general",
        market_regime="risk_on",
        abnormal_returns={"5m": -0.002, "30m": -0.004},
        volatility_impact=0.03,
        volume_impact=0.08,
    )

    index.add(strong_match)
    index.add(weak_match)

    matches = index.search(
        target,
        MarketContext(symbol="ACME", market_regime="risk_on"),
        top_k=2,
    )

    assert [m.outcome.event_id for m in matches] == ["h1", "h2"]
    assert matches[0].score > matches[1].score


def test_label_event_impact_computes_abnormal_returns_by_horizon() -> None:
    labels = label_event_impact(
        event_available_at_ns=100,
        asset_prices=[
            PricePoint(ts_ns=90, price=100.0),
            PricePoint(ts_ns=160, price=103.0),
            PricePoint(ts_ns=220, price=106.0),
        ],
        benchmark_prices=[
            PricePoint(ts_ns=90, price=200.0),
            PricePoint(ts_ns=160, price=202.0),
            PricePoint(ts_ns=220, price=204.0),
        ],
        horizons_ns={"1m": 60, "2m": 120},
    )

    assert round(labels.abnormal_returns["1m"], 4) == 0.02
    assert round(labels.abnormal_returns["2m"], 4) == 0.04
    assert labels.max_favorable_return == 0.04
    assert labels.max_adverse_return == 0.0


def test_news_impact_model_returns_multi_horizon_distribution_and_analogs() -> None:
    index = HistoricalAnalogIndex()
    index.add(
        HistoricalOutcome(
            event_id="h1",
            available_at_ns=1_699_000_000_000_000_000,
            source="benzinga",
            headline="FDA approval lifts medical device maker",
            body="FDA approval and commercial launch details.",
            symbols=("ACME",),
            event_type="regulatory",
            market_regime="risk_on",
            abnormal_returns={"5m": 0.02, "30m": 0.035},
            volatility_impact=0.22,
            volume_impact=0.65,
        )
    )
    index.add(
        HistoricalOutcome(
            event_id="h2",
            available_at_ns=1_698_000_000_000_000_000,
            source="reuters",
            headline="FDA approval sends similar stock higher",
            body="Approval surprise led to immediate price reaction.",
            symbols=("ACME",),
            event_type="regulatory",
            market_regime="risk_on",
            abnormal_returns={"5m": 0.01, "30m": 0.02},
            volatility_impact=0.18,
            volume_impact=0.50,
        )
    )
    model = NewsImpactModel(index=index, horizons=("5m", "30m"))

    prediction = model.predict(
        NewsEvent(
            event_id="n1",
            available_at_ns=1_700_000_000_000_000_000,
            source="benzinga",
            headline="Acme receives FDA approval",
            body="FDA approval unlocks commercial launch.",
            symbols=("ACME",),
            event_type="regulatory",
        ),
        MarketContext(symbol="ACME", market_regime="risk_on"),
    )

    assert prediction.symbol == "ACME"
    assert set(prediction.horizons) == {"5m", "30m"}
    assert prediction.horizons["5m"].expected_return > 0
    assert prediction.horizons["30m"].q90 >= prediction.horizons["30m"].q50
    assert prediction.horizons["30m"].q10 <= prediction.horizons["30m"].q50
    assert 0.5 <= prediction.confidence <= 1.0
    assert prediction.similar_events[0].event_id == "h1"
