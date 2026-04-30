# News Impact Model Implementation Status

## Goal

Build the strongest possible raw news-impact predictor:

```text
news arrives -> predict market reaction curve
```

It should not decide trade direction, sizing, or execution. Those remain
downstream concerns.

## Implemented Now

### Contracts

`NewsEvent` captures the news item with a strict `available_at_ns`, source,
headline/body, affected symbols, and event type.

`MarketContext` captures the pre-news state for one affected symbol:
regime, recent return, volatility, volume, spread, and liquidity placeholders.

`HistoricalOutcome` stores a past event plus realized abnormal returns by
horizon, volatility impact, and volume impact.

`NewsImpactPrediction` is the downstream-safe output contract:

```text
symbol
event_type
horizons -> expected_return, p_up, q10, q50, q90
volatility_impact
volume_impact
confidence
similar_events
```

### Historical Labeling

`label_event_impact` creates abnormal-return labels from asset and benchmark
price series. It uses the latest price at or before event availability as the
base price and the first price at or after each horizon as the future price.

This is the correct shape for point-in-time training because it uses only what
would have been known at event arrival.

### Historical Data Loading

`load_historical_outcomes` reads normalized `HistoricalOutcome` datasets from
JSONL, JSON, or CSV. JSONL is the preferred append-friendly format. CSV rows
can use `return_5m` or `abnormal_return_5m` style columns, and metadata columns
can use the `metadata_` prefix.

`write_historical_outcomes_jsonl` provides a small round-trip path for derived
or cleaned training sets. `scripts/optimize_weights.py` can now run the analog
weight optimizer directly from one of these files.

### Analog Retrieval

`HistoricalAnalogIndex` scores historical events using:

- text overlap
- symbol match
- event-type match
- market-regime match
- source credibility
- recency

This is a deterministic stand-in for the production vector retrieval layer.

### Analog Weight Optimization

`evaluate_analog_weights` and `optimize_analog_weights` let historical outcomes
choose the best retrieval weights. The evaluator uses leave-one-out prediction:
each old event is scored as if it were new, using every other old event as
history. The optimizer selects the candidate scoring weights with the lowest
mean absolute error for a chosen horizon.

`walk_forward_evaluate_analog_weights` and
`walk_forward_optimize_analog_weights` add the stricter chronological version:
events are sorted by `available_at_ns`, and each target event can only retrieve
prior events. Every fold records the training event IDs so leakage can be
audited.

This is the first implemented optimization loop for the model.

### Baseline Prediction

`NewsImpactModel` retrieves similar historical outcomes and produces weighted
multi-horizon impact distributions. It reports expected abnormal return,
probability of up move, simple quantiles, volatility impact, volume impact, and
similar-event evidence.

## Logic Behind The Model

Generic sentiment is too weak for trading news. The same event phrase can have
opposite effects depending on the asset and context. For example:

- rate hikes can hurt growth stocks but help banks
- oil supply cuts can help producers but hurt airlines
- regulatory approvals can help one company and hurt competitors
- security breaches can hurt an exchange but move rival venues upward

So the model uses event-specific historical reaction patterns instead of
positive/negative sentiment alone.

The baseline formula is:

```text
prediction_h = weighted_average(abnormal_return_h of similar past events)
```

The production formula should become:

```text
impact_distribution_h =
  fusion(news_text, event_metadata, market_context, historical_analogs)
```

## How To Use In The Main System Later

Keep this experiment isolated until it has enough historical labels and tests.
Then promote it in slices:

1. Add a `NewsImpactSignal` schema to `libs/fincept-core`.
2. Create `services/agents/src/agents/news_impact_agent`.
3. Reuse or extend the existing news ingestion path in
   `services/agents/src/agents/sentiment_agent`.
4. Store historical outcomes in Timescale via `libs/fincept-db`.
5. Publish predictions to a signal stream such as `sig.news_impact`.
6. Expose recent predictions through `services/api`.
7. Add a dashboard panel under `apps/dashboard/src/app/news` or a dedicated
   model page.

The main orchestrator should treat this as one raw signal source. It should not
allow this model to bypass risk, sizing, or paper-only controls.

## What Is Left To Implement

### Data

- Direct vendor news backfill adapters with exact availability timestamps.
- Entity/ticker linker that maps each article to affected symbols.
- Event classifier for regulatory, earnings, guidance, macro, product,
  security, litigation, partnership, financing, M&A, and general events.
- Benchmark/beta-adjusted abnormal return labeling.
- Deduplication across vendors for the same news event.
- Source reliability and latency stats per vendor.

### Model

- Financial text embedding model, initially FinBERT or a small financial LLM
  encoder.
- Vector index for historical analog retrieval.
- Learned event-surprise and novelty features.
- Market-context encoder over pre-news bars, volume, volatility, and spread.
- Multi-horizon quantile heads.
- Probability calibration per horizon.
- Asset-specific or sector-specific adapters.
- Continuous/grid search beyond the small built-in analog-weight candidate set.

### Evaluation

- Purged walk-forward validation by time.
- Event-type holdout validation.
- Source holdout validation.
- Regime-conditioned metrics.
- Calibration curves for `p_up`.
- Impact-decay accuracy.
- Error analysis by event type and source.

### Integration

- Fincept-core schemas.
- Redis stream producer/consumer.
- Timescale persistence.
- API endpoint.
- Dashboard view.
- Backtest replay using only `available_at_ns`.

## Optimization Path

1. Start with this analog baseline and real historical outcomes.
2. Normalize provider exports into JSONL/CSV with exact `available_at_ns`.
3. Optimize analog scoring weights per horizon with leave-one-out while data is
   scarce, then walk-forward once enough events exist.
4. Add embeddings and compare analog retrieval quality.
5. Train a LightGBM/CatBoost model on:
   - event metadata
   - source metadata
   - text embedding features
   - market context
   - analog summary features
6. Add transformer/TFT-style fusion only after the tabular baseline saturates.
7. Calibrate every horizon separately.
8. Keep old frozen test periods that are never used during feature selection.
9. Promote only if the model beats:
   - source/event priors
   - generic FinBERT sentiment
   - analog baseline
   - no-news baseline

## Verification Run

Current focused tests:

```powershell
python -m pytest experiments/news-impact-model/tests -q
```

Expected result:

```text
11 passed
```
