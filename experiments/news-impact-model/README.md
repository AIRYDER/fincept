# News Impact Model Experiment

Isolated scaffold for the raw predictive model:

```text
new news item -> predicted market impact by affected symbol and horizon
```

This experiment does not size trades, submit orders, or change the live Fincept
runtime. It is intentionally outside the uv workspace and main service graph.

## What Is Implemented

- Point-in-time news/event contracts in `src/news_impact_model/schema.py`.
- Historical impact label builder in `src/news_impact_model/labels.py`.
- Vendor-neutral historical outcome loaders in `src/news_impact_model/data.py`.
- Transparent historical analog retrieval in `src/news_impact_model/analogs.py`.
- Deterministic baseline predictor in `src/news_impact_model/model.py`.
- Trainable analog scoring weights in `src/news_impact_model/training.py`.
- Small facade for loading historical outcomes and scoring live events in
  `src/news_impact_model/pipeline.py`.
- Source/event prior helper in `src/news_impact_model/training.py`.
- Demo script in `scripts/demo.py`.
- File-driven optimizer script in `scripts/optimize_weights.py`.
- Standalone local workbench in `workbench/` served by
  `scripts/serve_workbench.py`.
- Sample normalized history in `sample_data/historical_outcomes.jsonl`.
- Focused tests in `tests/`.

The current model is a baseline, not the final ML stack. It uses similar
historical events to estimate multi-horizon abnormal return distributions.
The first optimization layer can now evaluate candidate analog-scoring weights
with leave-one-out or walk-forward historical error and pick the lowest-MAE
candidate.

## Run It

From the repo root:

```powershell
python -m pytest experiments/news-impact-model/tests -q
python experiments/news-impact-model/scripts/demo.py
```

Run the standalone workbench:

```powershell
python experiments/news-impact-model/scripts/serve_workbench.py
```

Then open:

```text
http://127.0.0.1:8765
```

The workbench loads the sample dataset by default. It can load another
normalized JSONL/JSON/CSV file, run the analog-weight optimizer, score a manual
news event, show similar historical events, and export optimized weights.

## Historical Outcome Data Format

The training/optimization input is a provider-neutral `HistoricalOutcome`
dataset. Preferred format is JSONL, one event per line:

```json
{"event_id":"evt-1","available_at_ns":1700000000000000000,"source":"reuters","headline":"Acme receives FDA approval","body":"","symbols":["ACME"],"event_type":"regulatory","market_regime":"risk_on","abnormal_returns":{"5m":0.018,"30m":0.031},"volatility_impact":0.22,"volume_impact":0.71,"metadata":{"provider_event_id":"abc-123"}}
```

CSV is also supported. Return columns may be named `return_5m` or
`abnormal_return_5m`; symbols may be separated by `|`, `,`, or `;`.

## Optimize Analog Weights

Use real `HistoricalOutcome` rows, then run:

```python
from news_impact_model.training import optimize_analog_weights

result = optimize_analog_weights(outcomes, horizon="5m")
print(result.weights)
print(result.evaluation.mae, result.evaluation.directional_accuracy)
```

Feed `result.weights` into `HistoricalAnalogIndex(weights=result.weights)`.
This lets old labeled events decide whether the current corpus should trust
text similarity, event-type match, market regime, source quality, or recency
more heavily.

Once there are enough events, prefer the time-aware evaluator:

```python
from news_impact_model.training import walk_forward_optimize_analog_weights

result = walk_forward_optimize_analog_weights(
    outcomes,
    horizon="5m",
    min_train_events=250,
)
```

Walk-forward optimization sorts by `available_at_ns` and only uses prior events
to predict later events.

You can run the same optimizer from a labeled JSONL/CSV file:

```powershell
python experiments/news-impact-model/scripts/optimize_weights.py `
  path\to\historical_outcomes.jsonl `
  --horizon 5m `
  --mode walk-forward `
  --min-train-events 250
```

## Output Contract

The predictor returns:

- affected `symbol`
- `event_type`
- per-horizon `expected_return`
- per-horizon `p_up`
- per-horizon `q10`, `q50`, `q90`
- `volatility_impact`
- `volume_impact`
- model `confidence`
- top similar historical events

The downstream trading system can later decide whether the predicted impact is
worth trading. This experiment only predicts market effect.

## Design Principle

The target is not generic positive/negative sentiment. The target is:

```text
abnormal_return_h =
  asset_return_after_news_h
  - benchmark_or_beta_adjusted_return_h
```

This lets the model learn that the same words can mean different things for
different assets, regimes, and event types.

## Why Analog Retrieval Is First

The best production version should combine:

1. financial text/event encoder
2. market-context encoder
3. historical analog retrieval
4. multi-horizon probabilistic prediction heads

This scaffold implements the analog and output-contract parts first because
they are explainable, cheap to test, and useful before a large labeled corpus
exists.

## Current Limitations

- No direct vendor news ingestion adapters yet; loaders expect normalized
  historical outcome files.
- No ticker/entity resolver.
- No real embeddings or vector database.
- No trained text transformer.
- No learned market-context encoder.
- No calibrated probability model.
- No Fincept bus/API/dashboard wiring.
- Workbench is local-only and intentionally does not submit orders or emit
  trading signals.

See `docs/IMPLEMENTATION_STATUS.md` for the full status and next steps.
