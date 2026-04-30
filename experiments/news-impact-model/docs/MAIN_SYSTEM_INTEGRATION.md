# Main System Integration Notes

This experiment should stay outside the main runtime until the prediction
contract, historical labels, and validation metrics are stable.

## Proposed Event Flow

```text
news vendor/API
  -> news ingestion
  -> entity/event extraction
  -> NewsImpactModel
  -> sig.news_impact
  -> orchestrator/risk/OMS consume only after their own gates
```

## Promotion Slice 1: Contract Only

Add schemas to `libs/fincept-core`:

- `NewsImpactSignal`
- `NewsImpactHorizon`
- `NewsAnalogEvidence`

The signal should include:

- `event_id`
- `symbol`
- `available_at_ns`
- `source`
- `event_type`
- `model_version`
- `horizons`
- `confidence`
- `similar_events`

## Promotion Slice 2: Agent Service

Create:

```text
services/agents/src/agents/news_impact_agent/
```

Suggested modules:

- `main.py` for Redis loop
- `entity_linker.py` for ticker mapping
- `event_classifier.py` for event type
- `impact_model.py` for predictor wrapper
- `history.py` for analog/outcome loading

## Promotion Slice 3: Storage

Add DB tables for:

- raw news event
- event-to-symbol link
- historical impact label
- model prediction
- analog evidence

Every record needs an availability timestamp. This is the most important guard
against training leakage.

The isolated experiment now expects normalized `HistoricalOutcome` rows, so the
first production bridge should be a one-way exporter from DB records to JSONL:

```text
raw news + symbol links + labels
  -> normalized historical_outcomes.jsonl
  -> scripts/optimize_weights.py
  -> candidate analog weights and validation metrics
```

Keep that bridge read-only until the validation reports are stable.

## Promotion Slice 4: API And Dashboard

API:

```text
GET /news-impact/recent
GET /news-impact/events/{event_id}
GET /news-impact/models/{model_version}/calibration
```

Dashboard:

- live event-impact tape
- per-horizon predicted reaction curve
- similar historical events
- calibration badge by event type
- source latency/reliability panel

## Guardrails

- Never submit orders from this agent.
- Never emit a signal without `available_at_ns`.
- Never train on article timestamps that are not actionability timestamps.
- Never mix future post-event prices into pre-event features.
- Always preserve top analog evidence for explainability.
