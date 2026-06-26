# News Impact Promotion Progress

Last updated: 2026-05-17

## Desired End State

The news-impact layer should become a durable, point-in-time, operator-visible
shadow signal system. It should ingest enriched news, normalize events with
exact availability timestamps, score market impact with auditable model
evidence, persist every event and signal, and produce clean replay/export
datasets for calibration and promotion decisions.

The finished system should answer four questions clearly:

- What did the model know at the exact event availability time?
- What impact did it predict by horizon, with what confidence and evidence?
- What actually happened later, after benchmark and beta adjustment?
- Did the model beat source/event priors, generic sentiment, the analog
  baseline, and the no-news baseline without lookahead?

## Safety Invariants

- `NewsImpactSignal` stays a raw signal contract with no order, sizing, route,
  broker, or execution fields.
- Dashboard and API surfaces are read-only until a separate promotion gate is
  approved.
- Replay, export, labeling, and evaluation must use `available_at_ns` as the
  point-in-time boundary.
- No model output can bypass risk, sizing, paper-only controls, or the main
  orchestrator.
- Any promotion claim must include calibration, holdout, regime, and
  no-lookahead evidence.

## Completed Foundation

- [x] Normalize provider exports into `NewsEvent`-style records with exact
  `available_at_ns`.
- [x] Link affected entities and tickers with an alias-based first pass.
- [x] Classify regulatory, earnings, guidance, macro, product, security,
  litigation, partnership, financing, M&A, and general events.
- [x] Deduplicate same-story vendor events.
- [x] Compute source latency summary stats from normalized batches.
- [x] Add beta-adjusted abnormal-return labeling through `asset_beta`.
- [x] Add dependency-free text embeddings, vector analog retrieval, market
  context encoding, surprise features, and novelty features.
- [x] Add purged walk-forward splits, event/source holdouts, calibration
  buckets, impact-decay checks, and error analysis helpers.
- [x] Add core `NewsImpactSignal` and `NewsImpactHorizon` schemas.
- [x] Add the `sig.news_impact` Redis stream name.
- [x] Add `news_impact_agent` to consume `info.enriched`, preserve exact
  `available_at_ns`, suppress no-analog events, and publish shadow signals.
- [x] Add authenticated `GET /news-impact/signals` for recent read-only Redis
  stream inspection.

## Track 1: Dashboard Panel For Shadow Signals

### End Goal

Operators should have a dashboard panel that makes the shadow stream usable at
a glance. The panel should show the event, symbol, confidence, horizon-level
impact distribution, source URL, similar historical events, model version, and a
clear `shadow only / not trade-driving` badge.

The panel should help an operator understand the model's reasoning without
creating any impression that the signal is executable.

### Desired Operator View

- Event headline, event type, source, source URL, and availability time.
- Affected symbol and optional venue/security identifiers.
- Model confidence and model version.
- Horizon rows for expected return, `p_up`, `q10`, `q50`, and `q90`.
- Similar events with historical date, symbol, event type, score, and realized
  outcome where available.
- Empty, loading, stale-data, and API-error states.
- Permanent `SHADOW ONLY / NOT TRADE DRIVING` visual badge.
- No buy/sell, allocation, broker, order ticket, or sizing controls.

### Checklist

- [x] Add dashboard client method for `GET /news-impact/signals`.
- [x] Add TypeScript types for `NewsImpactSignal` and `NewsImpactHorizon`.
- [x] Add a dashboard panel for recent `sig.news_impact` signals.
- [x] Render event, symbol, confidence, horizons, source URL, similar events,
  and model version.
- [x] Add the permanent shadow-only badge.
- [x] Add empty, loading, stale, and error states.
- [x] Add tests proving no order, sizing, route, broker, or execution controls
  are rendered.
- [x] Add tests proving horizon rows and similar-event evidence render from a
  fixture payload.
- [ ] Add a dashboard navigation entry only after the panel is stable.
- [x] Run browser verification against the local dashboard route. DOM,
  interaction, console, and overlay checks passed; screenshot capture timed out
  in the in-app browser.

### Done Means

- The operator can inspect live shadow signals without opening Redis directly.
- The page remains read-only and visually labels every signal as non-driving.
- Tests fail if trade-driving controls are introduced accidentally.

## Track 2: Backtest Replay And Export Using `available_at_ns`

### End Goal

The replay/export path should reconstruct exactly what the model could have
known at each event availability time. This is the proof layer for no lookahead,
no cherry-picking, and clean training/evaluation datasets.

### Desired Replay Contract

- Inputs are normalized news events, persisted shadow signals, market bars,
  source metadata, and realized outcomes.
- Every join and retrieval is bounded by `available_at_ns`.
- Historical analog retrieval uses only events whose availability time is less
  than or equal to the replay clock.
- Exports are deterministic JSONL/CSV datasets that the workbench can load.
- Reports include leakage checks, event counts, missing-data counts, and horizon
  coverage.

### Checklist

- [ ] Define the replay input contract for events, signals, bars, source stats,
  and outcomes.
- [ ] Add a storage reader that filters all event and market data by
  `available_at_ns <= replay_time`.
- [ ] Add replay logic that rebuilds analog candidates from point-in-time
  history only.
- [ ] Export normalized event rows to JSONL and CSV.
- [ ] Export model-visible feature rows to JSONL and CSV.
- [ ] Export realized outcome labels by horizon after the allowed future window.
- [ ] Add leakage assertions that fail when a future event or future outcome is
  visible during scoring.
- [ ] Add a focused CLI command for replay/export.
- [ ] Add tests with intentionally future-dated analogs to prove they are
  excluded.
- [ ] Add a replay summary report with event count, horizon coverage, leakage
  checks, and missing-data counts.

### Done Means

- A reviewer can rerun a backtest and see that every scored event used only
  records available at that event's timestamp.
- The exported workbench dataset is deterministic and reproducible.
- Leakage tests protect the replay path from future regressions.

## Track 3: Timescale Persistence

### End Goal

The stream should stop being ephemeral. Timescale should persist normalized news
events, shadow signals, source latency stats, and realized outcomes so
calibration and promotion decisions have durable history.

### Desired Storage Shape

- Normalized news events keyed by event ID and `available_at_ns`.
- Shadow `NewsImpactSignal` rows keyed by signal ID, event ID, symbol, model
  version, and publication time.
- Horizon predictions stored in a queryable format.
- Similar-event evidence stored without bloating high-cardinality indexes.
- Source reliability and latency stats stored by vendor and time window.
- Realized outcomes stored by event, symbol, horizon, benchmark, and beta.
- Idempotent writers so replaying Redis streams does not duplicate rows.

### Checklist

- [ ] Design migrations for normalized events, shadow signals, source stats,
  and realized outcomes.
- [ ] Add `normalized_news_events` persistence.
- [ ] Add `news_impact_signals` persistence.
- [ ] Add queryable storage for horizon predictions.
- [ ] Add compact storage for similar-event evidence.
- [ ] Add `source_latency_stats` persistence.
- [ ] Add `news_impact_realized_outcomes` persistence.
- [ ] Add an idempotent consumer or writer for `sig.news_impact`.
- [ ] Add read APIs for persisted recent signals and event history.
- [ ] Add tests covering idempotent writes and exact `available_at_ns`
  preservation.
- [ ] Add migration and rollback documentation.

### Done Means

- Restarting Redis or services does not erase inspection history.
- Calibration jobs can query durable signal and outcome history.
- Backtest replay/export can read from storage instead of ad hoc files only.

## Promotion Gate

The model should remain shadow-only until all of these checks are satisfied:

- [ ] Calibration curves are generated separately for each horizon.
- [ ] Purged walk-forward results are generated by time period.
- [ ] Event-type holdout results are generated.
- [ ] Source holdout results are generated.
- [ ] Regime-conditioned metrics are generated.
- [ ] Impact-decay accuracy is measured.
- [ ] Error analysis is summarized by event type and source.
- [ ] Backtest replay report proves no lookahead through `available_at_ns`.
- [ ] The model beats source/event priors.
- [ ] The model beats generic FinBERT sentiment.
- [ ] The model beats the analog baseline after the new candidate is trained.
- [ ] The model beats the no-news baseline.
- [ ] A human promotion decision explicitly approves any orchestrator wiring.

## How To Update This Tracker

- Mark a checkbox only after the matching code, tests, and docs have landed.
- Add the verification command or evidence link next to newly completed work
  when the check is not self-evident.
- Keep shadow-safety wording explicit whenever a UI, API, stream, or storage
  surface is added.
- If a track changes scope, update its end goal before implementing more code.
