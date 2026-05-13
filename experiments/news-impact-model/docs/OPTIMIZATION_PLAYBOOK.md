# Optimization Playbook

## Baselines To Beat

1. No-news baseline: predict zero abnormal return.
2. Source/event prior: mean historical reaction by `(source, event_type)`.
3. Generic FinBERT sentiment score.
4. Current analog baseline.

The model is only useful if it beats all four out of sample.

## Training Dataset

Each normalized `HistoricalOutcome` row should contain:

```text
event_id
available_at_ns
source
event_type
headline
body
symbols
market_regime
abnormal_return_1m
abnormal_return_5m
abnormal_return_15m
abnormal_return_30m
abnormal_return_1h
abnormal_return_1d
volatility_impact
volume_impact
metadata
```

JSONL is preferred for append-only backfills. CSV is accepted for quick
research exports; return columns may be named `return_5m` or
`abnormal_return_5m`, and metadata columns may use `metadata_`.

Run the current optimizer from a normalized file:

```powershell
python experiments/news-impact-model/scripts/optimize_weights.py `
  path\to\historical_outcomes.jsonl `
  --horizon 5m `
  --mode walk-forward `
  --min-train-events 250
```

`load_historical_outcomes` is the only ingestion boundary implemented so far.
Provider-specific Reuters/Benzinga/Polygon/NewsAPI adapters should normalize
into this contract rather than feeding provider payloads straight into
training.

For interactive inspection, run the local workbench:

```powershell
python experiments/news-impact-model/scripts/serve_workbench.py
```

Use the workbench to load the dataset, compare validation modes, inspect
similar-event evidence, and export candidate weights before promoting anything
into the main Fincept runtime.

## Feature Families

Text:

- financial text embeddings
- event type
- novelty
- surprise
- named entities
- source credibility
- vendor latency

Market context:

- pre-news momentum
- realized volatility
- relative volume
- spread
- liquidity score
- market regime
- sector/index movement

Analog retrieval:

- top-K mean return per horizon
- top-K return dispersion
- top-K p_up
- top-K event-type agreement
- top-K source agreement
- top-K recency

## Model Sequence

1. `source_event_prior`
2. analog baseline
3. trained analog scoring weights
4. LightGBM/CatBoost tabular impact model
5. text embedding + tabular model
6. transformer fusion model
7. asset/sector adapters

Do not start at step 5. It will be harder to debug and easier to overfit.

## Current Optimization Layer

`optimize_analog_weights` runs a transparent leave-one-out check over historical
outcomes. `walk_forward_optimize_analog_weights` runs the stricter time-ordered
version and should become the default when enough events exist. For each
candidate weight set it:

1. removes one historical event from the index
2. predicts that event's abnormal return from the remaining events
3. records mean absolute error and directional accuracy
4. selects the candidate with lowest MAE, using directional accuracy as the tie
   breaker

The walk-forward version instead:

1. sorts events by `available_at_ns`
2. starts after `min_train_events`
3. predicts each target from prior events only
4. records each fold's `target_event_id`, `train_event_ids`, predicted return,
   actual return, absolute error, and direction hit

The optimized weights feed directly into:

```python
HistoricalAnalogIndex(weights=result.weights)
```

This is the first version of "train on old data to weight new data." It is not
yet a full ML model, but it already turns the analog ranker from a fixed recipe
into a validation-driven component.

## Validation

Use time-ordered validation:

- train on old period
- validate on later period
- purge overlapping label windows
- hold out recent months untouched

Report metrics by:

- horizon
- event type
- source
- symbol liquidity bucket
- volatility regime

Required metrics:

- MAE of abnormal return
- directional accuracy
- Brier score for `p_up`
- calibration error
- q10/q90 coverage
- rank correlation between predicted and realized impact

## Production Promotion Gate

Promote only when:

- it beats all baselines on the frozen holdout
- calibration is sane at every horizon
- performance is not concentrated in one event type
- errors are explainable with analog evidence
- source latency is measured and stable
