# Fincept Terminal Codebase Review

Date: 2026-05-02  
Scope: local worktree review of the recent data, research, Exa, OpenBB, dashboard, and infrastructure surfaces. This is not a full production certification because Postgres/Timescale and the local OpenBB API were not both live during review.

## Review Summary

The recent implementation is directionally solid: the research surface is read-only, Exa output is structured, OpenBB dispatch has a path allowlist and Redis rate limit, OpenBB health history is capped, and the new data coverage endpoint has focused tests. The main issues are contract drift, a React render-time state update, sequential database reads in the coverage endpoint, and a few operator-experience/security hardening items.

One staleness note from the Exa setup guide: the canonical Exa coding-agent docs currently show raw JSON uses `numResults` and camelCase keys. The local Exa tool uses `numResults`, so that part is correct. The earlier pasted guide using `num_results` for raw cURL JSON is stale.

## Verification Run

Passed:

```text
.venv\Scripts\python.exe -m pytest services/api/tests/test_data.py services/api/tests/test_research.py services/api/tests/test_rate_limit.py -q
31 passed

.venv\Scripts\ruff.exe check services/api/src/api/routes/data.py services/api/src/api/routes/research.py services/api/src/api/openbb_health_store.py services/api/src/api/rate_limit.py libs/fincept-tools/src/fincept_tools/research/openbb.py
All checks passed

npm run typecheck
@fincept/dashboard typecheck passed
```

Not verified:

- Live `/data/coverage` against Timescale/Postgres. The service was previously returning connection refused for universe reads.
- Live OpenBB API calls against `http://127.0.0.1:6900`. The code degrades cleanly, but live provider responses were not available here.
- Full browser visual regression pass. The dashboard typecheck passed, but no Playwright screenshot pass was run in this review.
- Full repo preflight. The worktree is very dirty with many unrelated edits, so this review stayed targeted.

## Findings

### P1: React State Is Updated During Render

Location: `apps/dashboard/src/app/markets/page.tsx:74`

`MarketsPage` default-selects the first symbol by calling `setSelected` directly during render:

```tsx
if (!selected && symbols[0]) {
  setSelected(symbols[0].symbol);
}
```

This can trigger React warnings and repeated render loops when query data or filtering changes. It also makes the selected symbol behavior harder to reason about because render is no longer pure.

Best fix options:

- Move the default selection into a `useEffect` keyed on `selected` and the first visible symbol.
- Decide whether filtering should preserve a now-hidden selected symbol or auto-select the first filtered result, then encode that behavior explicitly.

Recommended patch:

```tsx
useEffect(() => {
  if (!selected && symbols[0]) {
    setSelected(symbols[0].symbol);
  }
}, [selected, symbols]);
```

### P1: Universe API Contract Drift In The Dashboard Type

Locations:

- `libs/fincept-db/src/fincept_db/universe.py:38`
- `apps/dashboard/src/lib/types.ts:132`

`read_universe()` returns `venue_default`, but `UniverseRow` declares `venue`. The current Markets page only consumes `symbol` and `asset_class`, so typecheck does not catch it. Any future UI that reads `u.venue` will receive `undefined`.

Best fix options:

- Backend-compatible fix: keep returning `venue_default`, and change the frontend type to `venue_default: string`.
- Backward-compatible API fix: include both `venue_default` and `venue` in `/data/universe`, then gradually update frontend callers to the explicit `venue_default`.
- Best long-term fix: introduce a shared generated OpenAPI/TypeScript client so this class of drift cannot silently land.

### P2: `/data/coverage` Does Sequential N+1 Bar Reads

Location: `services/api/src/api/routes/data.py:108`

The endpoint loops over every active universe row and calls `read_bars` once per symbol. This is fine for a tiny demo universe, but it will become slow and database-heavy as the universe grows.

Best fix options:

- Add a batch reader in `fincept_db.bars`, e.g. `read_bar_coverage(symbols, freq, start_ns, end_ns, venue=None)`, using `GROUP BY symbol` plus latest timestamp and count.
- Keep the current response shape, but compute it from one DB query.
- Add an explicit `limit` or asset-class filter in the dashboard until the batch query exists.

### P2: Coverage Venue Filtering Can Create False Empty Results

Location: `services/api/src/api/routes/data.py:111`

When no venue override is provided, coverage uses each universe row's `venue_default` as the bar venue filter. If bars are stored under a different internal venue value, such as `sim`/`paper`/provider-specific labels, coverage can report symbols as empty even when bars exist.

Best fix options:

- Treat omitted `venue` as "all venues" and only filter when the operator explicitly passes one.
- Return `venue_default` in the response as metadata, but do not use it as the default bar filter until storage conventions are guaranteed.
- Add a venue-normalization layer between universe, bars, OpenBB, Alpaca, Binance, and paper/sim feeds.

### P2: Coverage Errors Expose Raw Exception Text

Locations:

- `services/api/src/api/routes/data.py:100`
- `services/api/src/api/routes/data.py:120`

The endpoint returns raw exception strings in the 503 detail and per-symbol `error` field. This is useful during development but can leak hostnames, connection strings, SQL text, or provider internals in the dashboard.

Best fix options:

- Log the full exception server-side with `exc_info=True`.
- Return a stable public error code and short operator message, such as `DataStoreUnavailable` or `BarReadFailed`.
- Keep raw details behind a debug flag if needed locally.

### P3: OpenBB Health Probe Can Block For Up To 15 Seconds

Locations:

- `libs/fincept-tools/src/fincept_tools/research/openbb.py:141`
- `libs/fincept-tools/src/fincept_tools/research/openbb.py:317`

The shared `_get_json` helper uses a 15 second timeout. `check_openbb_health()` calls the same helper, so a dashboard polling health every 30 seconds can hang longer than expected when OpenBB is degraded rather than hard-down.

Best fix options:

- Add a timeout parameter to `_get_json`.
- Use a short timeout for `/openapi.json`, e.g. 1.5-3 seconds.
- Keep the longer timeout for real OpenBB data calls.

### P3: OpenBB Dispatch Presets Are Hardcoded In The Page

Location: `apps/dashboard/src/app/research/page.tsx:40`

The OpenBB proof panel hardcodes three fundamentals endpoints. That is fine as a proof, but future OpenBB growth will be painful if every new dataset requires editing the page.

Best fix options:

- Move presets to a typed registry file, e.g. `apps/dashboard/src/features/research/openbb-presets.ts`.
- Later source the registry from backend metadata or `docs/datasources.md`.
- Add labels for latency, provider requirements, expected columns, and preferred UI renderer.

## Extension Points For Future Features

### Data Source Registry

Create a first-class datasource registry that powers docs, API routes, and UI. It should know:

- Source name: Exa, OpenBB API, OpenBB package, Alpaca, Binance, Timescale bars, Redis marks, local predictions, news impact model.
- Call surface: REST path, tool id, package method, DB table, or Redis key.
- Auth requirement: `.env` key, bearer token, local service, OAuth/MCP, or public.
- Return format: rows, structured brief, OHLCV bars, stream/log, model artifact, or health sample.
- Latency profile: instant, fast, deep, local DB, network provider, offline fallback.
- Safety tier: read-only, paper-trading only, write-capable, or external-cost.

This can become a backend endpoint such as `GET /data/sources`, then the dashboard can render a Data Control Center instead of relying on manually maintained docs.

### Batch Coverage And Freshness History

The new coverage endpoint should become the platform's data heartbeat:

- Batch coverage query by symbol/freq/venue.
- Store periodic snapshots so the UI can show freshness trends, not just current status.
- Alert when critical symbols go stale before backtests, model training, or strategy start.
- Split `coverage_pct` into `availability_pct` and `fresh_pct` so stale data does not look fully healthy.

### OpenBB Capability Browser

OpenBB is best exposed as a curated capability browser:

- Preset registry grouped by equities, ETFs, options, macro, fixed income, crypto, news, regulators.
- Per-endpoint param forms generated from metadata.
- Expected output columns and table renderers.
- Provider key status from the Open Data Platform environment.
- Result caching for expensive provider calls.

### Research Workspace

The Research page can evolve into a real analyst console:

- Combine Exa source-grounded briefs with OpenBB fundamentals and local chart/coverage context.
- Save research runs by symbol and timestamp.
- Compare bull/bear claims against recent bars, news impact scores, and model predictions.
- Add source quality badges and stale-source warnings.

### Provider And Credential Health

Add a provider-health layer separate from service-health:

- Exa key present and last successful search.
- OpenBB API reachable.
- OpenBB provider keys present in the Open Data Platform environment.
- Alpaca/Binance market-data connectivity.
- Redis and Timescale status.

This should be read-only and should never reveal key values.

### Feature Store Gatekeeping

Before running training/backtests/live strategy loops, add data readiness gates:

- Required symbols have fresh bars for the chosen horizon.
- Required features are available and current.
- Known provider outages are surfaced before a run starts.
- The run record captures datasource versions and staleness metadata.

## Suggested Fix Order

1. Move the Markets default selection into `useEffect`.
2. Resolve the universe `venue` / `venue_default` contract drift.
3. Stop leaking raw coverage exceptions into API responses.
4. Add a shorter OpenBB health timeout.
5. Build batch bar coverage in `fincept_db.bars`.
6. Extract OpenBB presets into a typed registry.
7. Add `GET /data/sources` and let the UI render from it.

## Overall Assessment

The platform is in a good feature-acceleration state, but the next step should be contract cleanup and datasource hardening before adding many more panels. Exa and OpenBB are wired in the right direction: read-only, structured, and visible in the Research surface. The data category now has the start of a useful health model, but it needs batching, clearer venue semantics, and safer error shaping to become a durable trading-platform primitive.
