from __future__ import annotations

from pathlib import Path
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.pipeline import NewsImpactPipeline
from news_impact_model.schema import HistoricalOutcome, MarketContext, NewsEvent


def main() -> None:
    outcomes = [
        HistoricalOutcome(
            event_id="hist-001",
            available_at_ns=1_699_000_000_000_000_000,
            source="benzinga",
            headline="FDA approval sends medical device maker higher",
            body="Approval unlocks a commercial launch.",
            symbols=("ACME",),
            event_type="regulatory",
            market_regime="risk_on",
            abnormal_returns={"5m": 0.018, "30m": 0.031, "1h": 0.026},
            volatility_impact=0.22,
            volume_impact=0.64,
        ),
        HistoricalOutcome(
            event_id="hist-002",
            available_at_ns=1_695_000_000_000_000_000,
            source="reuters",
            headline="Regulator approves launch of comparable product",
            body="The stock rose as approval removed uncertainty.",
            symbols=("ACME",),
            event_type="regulatory",
            market_regime="risk_on",
            abnormal_returns={"5m": 0.011, "30m": 0.024, "1h": 0.021},
            volatility_impact=0.17,
            volume_impact=0.52,
        ),
    ]
    pipeline = NewsImpactPipeline(outcomes=outcomes, horizons=("5m", "30m", "1h"))
    prediction = pipeline.predict(
        NewsEvent(
            event_id="live-001",
            available_at_ns=1_700_000_000_000_000_000,
            source="benzinga",
            headline="Acme receives FDA approval for new device",
            body="The approval allows Acme to begin commercial launch.",
            symbols=("ACME",),
            event_type="regulatory",
        ),
        context=MarketContext(symbol="ACME", market_regime="risk_on"),
    )
    print(json.dumps(prediction, default=lambda obj: obj.__dict__, indent=2))


if __name__ == "__main__":
    main()
