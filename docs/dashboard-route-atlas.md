# Dashboard Route & Mock-Data Atlas

**Generated for:** TASK-0201  
**Date:** 2026-06-22  
**Purpose:** Single source of truth for which dashboard routes are live (real API + WS), hybrid (live + labeled mocks), demo, or pure mock. Prevents operator confusion where fake numbers look real.  
**Scope:** All folders containing `page.tsx` under `apps/dashboard/src/app/`. API routes under `app/api/` noted only where they serve UI.  
**Cross-reference:** `featuresmenu.md` (Operator UX row: "Design dense, low-latency dashboard panels..."; Roadmap Placement calls for dashboard work during Phase U; this atlas is the prerequisite map before conversion work).  
**How to maintain:** Re-run the scan in TASK-0201 steps when new pages or data wiring changes. Update status column first.

## Summary

- **Total UI routes (page.tsx):** 24 (including dynamic segments)
- **Pure mock:** 2 (`/watchlist`, `/signal-cockpit-demo`)
- **Hybrid (live + explicit mock sections):** 6 (`/`, `/markets`, `/positions`, `/symbol/[symbol]`, `/portfolio-builder`, `/news-impact-lab`)
- **Live / real API backed:** 13
- **Redirects / special:** 2 (`/optimizer` → `/portfolio-builder`, `/news-lab` → `/news-impact-lab`)
- **Static / Catalog:** 1 (`/receipts`)
- **Next conversion target (first mock-heavy):** `/watchlist` (and its preview component). Clear inline fixture, fully replaceable with watchlist API endpoint once available. Low risk to other surfaces.

## Full Route Inventory

| Route                  | Primary Source Files                                      | Data Status | Backend Dependency                          | Risk if Mistaken for Live | Replacement Priority | Suggested Test |
|------------------------|-----------------------------------------------------------|-------------|---------------------------------------------|---------------------------|----------------------|---------------|
| `/` (home)            | `app/page.tsx`, `components/overview/*`, `components/shell/*` | Hybrid     | `api.services`, `api.*` (predictions, etc.); `WatchlistPreview` uses `mockPriceWalk` | High for watchlist preview prices / activity | High (preview) | `test:strategy-readiness`; snapshot of operator briefing with mock flag |
| `/backtest`           | `app/backtest/page.tsx`, `components/backtest/backtest-lab-panel.tsx` | Live       | `api.backtestStrategies`, `api.backtestRuns`, `api.backtestRun`, `api.runBacktest` (real API + mutations) | Low (explicit lab UI, form-driven) | Low                 | Backtest run E2E + report validation (uses existing test_backtest) |
| `/login`              | `app/login/page.tsx`                                      | Live       | `api.strategies` (token validation)        | Low                      | Low                 | Integration test for 401 vs success token flow |
| `/markets`            | `app/markets/page.tsx`                                    | Hybrid     | `api.universe`, `api.bars`, `api.dataCoverage`, `api.dataSources`, `api.services`, `api.openbbHealth`, `api.providerData`; separate `api.alpacaDataDemo` | Medium (demo panel can be confused with main view) | Medium | Verify demo button produces isolated data; coverage freshness tests |
| `/models`             | `app/models/page.tsx`, components/models/*               | Live       | `api.models`, promotion, runs              | Low                      | Low                 | Model list + promotion state contract test |
| `/models/[name]`      | `app/models/[name]/page.tsx`                              | Live       | `api.modelDetail`, `api.modelFeatureImportance`, runs | Low                      | Low                 | Meta.json contract + CV folds render test |
| `/news`               | `app/news/page.tsx`, `components/news/*`                 | Live       | `api.news`, news impact signals            | Low                      | Low                 | Tier + score roundtrip; adverse flag tests |
| `/news-impact-lab`    | `app/news-impact-lab/page.tsx`, components/news-impact/* | Hybrid / Lab | `api.newsImpact*` (predict, optimize, status, signals) + shadow panels | Medium (lab inputs look like prod) | Medium | Lab predict/optimize E2E with seeded headline |
| `/news-lab`           | `app/news-lab/page.tsx`                                   | Redirect   | N/A (redirects to /news-impact-lab)        | N/A                      | N/A                 | N/A |
| `/optimizer`          | `app/optimizer/page.tsx`                                  | Redirect   | N/A (→ /portfolio-builder)                | N/A                      | N/A                 | N/A |
| `/orders`             | `app/orders/page.tsx`, components/orders/*               | Live       | `api.orders`, `api.placeOrder`             | Low                      | Low                 | Order status filter + WS update test |
| `/portfolio-builder`  | `app/portfolio-builder/page.tsx`, `features/portfolio-builder/*` (PortfolioBuilderPage, marketDataService, PortfolioReportView, ...) | Hybrid     | `marketDataService` (DEMO vs live via `liveMarketDataClient`); allocation uses demo candidates when `dataMode === "demo"`; no real broker | High (allocations with "Demo data" badge still look authoritative) | High | Toggle demo/live; report schema + deterministic allocation tests |
| `/positions`          | `app/positions/page.tsx`                                  | Hybrid     | `api.positions`, WS `positions` topic; `sparklineForSymbol` uses `mockPriceWalk` + `MockBadge` | Medium (sparks mislead on mark price direction) | High (sparks) | Position posture + markSource tests; mock spark flag assertions |
| `/predictions`        | `app/predictions/page.tsx`, components/predictions/*     | Live       | `api.services`, model predictions, WS      | Low                      | Low                 | Confidence floor + consensus calc tests |
| `/receipts`           | `app/receipts/page.tsx`, components/receipts/*           | Static / Catalog | `buildProofReceiptCenter()` (local only)  | Low (explicitly read-only artifacts) | Low                 | Proof receipt center unit tests already exist |
| `/reconciliation`     | `app/reconciliation/page.tsx`, components/reconciliation/* | Live     | `api.positions` (includeFlat), recon checklist | Low                      | Low                 | Position recon checklist + P&L match test |
| `/research`           | `app/research/page.tsx`                                   | Live       | `api.exaResearch`, `api.openbb*`, provider data | Low                      | Low                 | Exa + OpenBB quote contract tests |
| `/risk`               | `app/risk/page.tsx`                                       | Live       | `api.killSwitch*`, regime, etc.            | Low                      | Low                 | Kill switch trip/clear + state machine test |
| `/signal-cockpit-demo`| `app/signal-cockpit-demo/page.tsx`, `features/signal-cockpit-demo/*` | Demo (pure mock) | None (placeholder artifacts, gbm-placeholder, demoMeta) | High (explicit "demo state" everywhere but isolated) | Low (intentional demo) | N/A - keep as demo harness |
| `/strategies`         | `app/strategies/page.tsx`                                 | Live       | `api.strategyConfigs`, `api.strategies`, `api.positions` | Low                      | Low                 | Lifecycle merge + orphan detection tests |
| `/strategies/[id]`    | `app/strategies/[id]/page.tsx`                            | Live       | `api.strategy*` + history, positions scoped | Low                      | Low                 | Strategy detail position + history roundtrip |
| `/symbol/[symbol]`    | `app/symbol/[symbol]/page.tsx`                            | Hybrid     | Live: position, signals/WS; Mock: meta, chart (mockPriceWalk + withMockFlag), news | High (header price + chart + news fixtures) | High | Symbol mock flag on meta/chart; live position integration test |
| `/system`             | `app/system/page.tsx`, components/system/*               | Live       | `api.health`, readiness packet (env presence + verification), services | Low (env only; never secrets) | Low                 | Source health + readiness packet tests |
| `/watchlist`          | `app/watchlist/page.tsx`, `components/widgets/watchlist-row`, `lib/mock-data` | Mock (pure inline fixture) | None (`buildMockWatchlist`, `mockPriceWalk`, explicit `MOCK: ...` warning + `isMock`) | High (prices, changes, volume all synthetic) | **Highest** | Replace target: watchlist API contract + migration test for rows |

## Observations & Non-Obvious Decisions

- The `lib/mock-data.ts` + `withMockFlag` + `MockBadge` discipline (and dev `console.warn`) is already enforced on pure-mock surfaces. Atlas makes the surfaces discoverable without reading every file.
- Hybrid pages correctly label their mock subsections (e.g. sparklines, symbol metadata). The risk column flags pages where a partial live surface + unlabeled mock can still mislead.
- Demo mode in portfolio-builder is explicit (`dataMode === "demo"` badge + static candidates). Still high replacement priority because allocations look production-grade.
- `featuresmenu.md` cross-ref confirms dashboard panels are a focus area; this document is the "scoreboard" before any panel conversion or new feature landing.
- No route currently uses forward-looking data or leaks; the live calls go through typed `api.ts` which sets `cache: "no-store"`.
- `/news-lab` and `/optimizer` are pure redirects; documented for completeness.

## First Mock-Heavy Route Identified for Conversion

**`/watchlist`** (and the `WatchlistPreview` component embedded on home).

- 100% fixture data.
- Explicit `MOCK` badges + dev log + comment: "replace with watchlist API when the endpoint ships".
- Low coupling (simple WatchRow shape).
- High operator value (dense symbol table).
- Suggested first service-backed read-only route: add `api.watchlist(...)` stub → wire real universe + last-price feed.

**Secondary quick win:** Extract sparkline generation behind a `usePriceSeries(symbol, opts)` hook that can swap `mockPriceWalk` for real bars call, then enable on `/positions`.

## Verification

```powershell
# Run exactly as specified in assignment
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

Expected: clean (documentation-only change; no source edits).

See `TASK-0201` acceptance: "Every dashboard route has a readiness status in the atlas; mock-heavy screens visible in one doc; the next conversion target is obvious."

---

*This file was produced following the exact steps in the swarm assignment for TASK-0201. Only `docs/dashboard-route-atlas.md` was created/modified.*