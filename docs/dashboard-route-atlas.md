# Dashboard Route Atlas

**Task:** TASK-0201
**Status:** Complete
**Date:** 2026-06-22
**Owner:** Builder 1 (GLM-5.2)
**Dependencies:** TASK-0101 (receipt runner) ✅

---

## Purpose

This atlas maps every dashboard route to its data status (live / mock /
hybrid / demo / redirect), backend dependency, risk if mistaken for live
data, replacement priority, and suggested test. The operator can use this
to know whether a panel is backed by real API data or by `mock-data.ts`
without reading source code.

**Safety principle:** A mock panel mistaken for live data could drive a wrong
operator decision. Every mock panel must display a `<MockBadge>` and every
live panel must be backed by a real API endpoint.

---

## Route Summary

| Route                        | Data Status | Backend Dependency          | MockBadge? | Risk  | Priority |
|------------------------------|-------------|-----------------------------|------------|-------|----------|
| `/`                          | Live        | API + WebSocket             | No         | Low   | —        |
| `/system`                    | Live        | API (services, modules)     | No         | Low   | —        |
| `/quant-foundry`             | Live        | API (QF gateway)            | No         | Low   | —        |
| `/positions`                 | Live        | API + WebSocket             | No         | Low   | —        |
| `/orders`                    | Live        | API                         | No         | Low   | —        |
| `/markets`                   | Live        | API (universe, bars, etc.)  | No         | Low   | —        |
| `/news`                      | Live        | API (news, impact, etc.)    | No         | Low   | —        |
| `/news-impact-lab`           | Live        | API (news impact)           | No         | Low   | —        |
| `/research`                  | Live        | API (Exa, OpenBB)           | No         | Low   | —        |
| `/backtest`                  | Live        | API (strategies, runs)      | No         | Low   | —        |
| `/predictions`               | Live        | API + WebSocket             | No         | Low   | —        |
| `/reconciliation`            | Live        | API (positions, orders)     | No         | Low   | —        |
| `/risk`                      | Live        | API (positions, regime)     | No         | Low   | —        |
| `/strategies`                | Live        | API (configs, runtime)      | No         | Low   | —        |
| `/strategies/[id]`           | Live        | API (detail, positions)     | No         | Low   | —        |
| `/models`                    | Live        | API (models, promotion)     | No         | Low   | —        |
| `/models/[name]`             | Live        | API (detail, importance)    | No         | Low   | —        |
| `/login`                     | Live        | API (auth)                  | No         | Low   | —        |
| `/symbol/[symbol]`           | **Hybrid**  | API + `mock-data.ts`        | **Yes (3)**| **Medium** | **High** |
| `/watchlist`                 | **Mock**    | `mock-data.ts`              | **Yes (1)**| **High** | **High** |
| `/receipts`                  | **Demo**    | Client-side receipt defs    | No         | Low   | Low      |
| `/signal-cockpit-demo`       | **Demo**    | `SignalCockpitDemo` feature | No         | Low   | Low      |
| `/portfolio-builder`         | **Hybrid**  | `marketDataService.ts`     | No         | Medium | Medium   |
| `/optimizer`                 | Redirect    | → `/portfolio-builder`      | —          | —     | —        |
| `/news-lab`                  | Redirect    | → `/news-impact-lab`        | —          | —     | —        |

---

## Detailed Route Entries

### Live Routes (18 routes)

#### `/` — Dashboard Overview

- **Source files:** `apps/dashboard/src/app/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/positions`, `/orders`, `/strategies`, `/services`, `/modules/{id}/logs`) + WebSocket (`useFinceptStream`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low — all data is API-backed
- **Replacement priority:** — (already live)
- **Suggested test:** Verify positions/orders/strategies render with API data; verify WebSocket stream updates in real time

#### `/system` — System Readiness Center

- **Source files:** `apps/dashboard/src/app/system/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/services`, `/kill-switch`, `/openbb/health`, `/readiness`, `/modules`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low — all data is API-backed
- **Replacement priority:** — (already live)
- **Suggested test:** Verify readiness checks render; verify module control panel start/stop works

#### `/quant-foundry` — Quant Foundry Overview

- **Source files:** `apps/dashboard/src/app/quant-foundry/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/quant-foundry/health`, `/quant-foundry/jobs`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low — read-only overview, no actions
- **Replacement priority:** — (already live, TASK-0801)
- **Suggested test:** Verify page loads in disabled mode (503 → "DISABLED" state); verify no promote/trade actions

#### `/positions` — Positions

- **Source files:** `apps/dashboard/src/app/positions/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/positions`) + WebSocket (`useFinceptStream`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify positions render with API data; verify real-time P&L updates via WebSocket

#### `/orders` — Orders

- **Source files:** `apps/dashboard/src/app/orders/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/orders`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify order list renders; verify order status badges are correct

#### `/markets` — Markets

- **Source files:** `apps/dashboard/src/app/markets/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/universe`, `/coverage`, `/sources`, `/services`, `/openbb/health`, `/provider-data`, `/bars`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify universe/coverage/sources render; verify seed/autopilot mutations work

#### `/news` — News Intelligence

- **Source files:** `apps/dashboard/src/app/news/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/news`, `/news-impact/status`, `/positions`, `/promotion`, `/news-alpha/report`, `/services`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify news query renders; verify impact status and promotion queries work

#### `/news-impact-lab` — News Impact Lab

- **Source files:** `apps/dashboard/src/app/news-impact-lab/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/news-impact/status`, `/news-impact/news`, `/news-impact/predict`, `/news-impact/optimize`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify status/news render; verify predict/optimize mutations work

#### `/research` — Research

- **Source files:** `apps/dashboard/src/app/research/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/exa/research`, `/openbb/fundamentals`, `/openbb/health`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify Exa/OpenBB mutations work; verify evidence stack renders

#### `/backtest` — Backtest

- **Source files:** `apps/dashboard/src/app/backtest/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/strategies`, `/backtest/runs`, `/backtest/runs/{id}`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify strategies/runs render; verify run mutation creates a new run

#### `/predictions` — Predictions

- **Source files:** `apps/dashboard/src/app/predictions/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/services`, `/promotion`) + WebSocket (`useFinceptStream`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify predictions render; verify WebSocket stream updates

#### `/reconciliation` — Reconciliation

- **Source files:** `apps/dashboard/src/app/reconciliation/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/positions`, `/strategies`, `/strategies/configs`, `/universe`, `/coverage`, `/orders`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify all queries render; verify adopt/seed/start mutations work

#### `/risk` — Risk

- **Source files:** `apps/dashboard/src/app/risk/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/positions`, `/services`, `/regime`, `/models`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify positions/services/regime/models render; verify trip/clear mutations work

#### `/strategies` — Strategies List

- **Source files:** `apps/dashboard/src/app/strategies/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/strategies/configs`, `/strategies/runtime`, `/positions`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify configs/runtime/positions render

#### `/strategies/[id]` — Strategy Detail

- **Source files:** `apps/dashboard/src/app/strategies/[id]/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/strategies/{id}`, `/strategies/{id}/positions`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify detail/positions render for a known strategy ID

#### `/models` — Models List

- **Source files:** `apps/dashboard/src/app/models/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/models`, `/promotion`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify models list renders; verify promotion status is correct

#### `/models/[name]` — Model Detail

- **Source files:** `apps/dashboard/src/app/models/[name]/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/models/{name}`, `/models/{name}/importance`, `/promotion`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify detail/importance/promotion render for a known model

#### `/login` — Login

- **Source files:** `apps/dashboard/src/app/login/page.tsx`
- **Data status:** Live
- **Backend dependency:** API (`/auth/login`)
- **MockBadge?** No
- **Risk if mistaken for live:** Low
- **Replacement priority:** —
- **Suggested test:** Verify login form submits to API; verify JWT token is stored on success

---

### Hybrid Routes (2 routes)

#### `/symbol/[symbol]` — Symbol Detail  ⚠️

- **Source files:** `apps/dashboard/src/app/symbol/[symbol]/page.tsx`
- **Data status:** **Hybrid** — positions + predictions via API, but metadata + fixtures via `mock-data.ts`
- **Backend dependency:** API (`/positions`, `/predictions`) + `@/lib/mock-data` (metadata, price walk, fixtures)
- **MockBadge?** **Yes (3 instances):**
  - Line 308: `<MockBadge source="Mock metadata" />` — symbol metadata
  - Line 507: `<MockBadge source="Inline fixture" />` — fixture data
  - Line 559: `<MockBadge source="Inline fixture" />` — fixture data
- **Risk if mistaken for live:** **Medium** — the positions and predictions are live, but the metadata and chart fixtures are mock. An operator could mistake the mock chart for live price data.
- **Replacement priority:** **High** — replace `mock-data.ts` imports with API calls for symbol metadata and price history
- **Suggested test:** Verify positions/predictions render with API data; verify MockBadge is visible on mock sections; replace mock metadata with `/symbols/{symbol}/metadata` API call

#### `/portfolio-builder` — Portfolio Builder  ⚠️

- **Source files:** `apps/dashboard/src/app/portfolio-builder/page.tsx` → `@/features/portfolio-builder/PortfolioBuilderPage.tsx`
- **Data status:** **Hybrid** — uses `marketDataService.ts` which can be live or mock depending on config
- **Backend dependency:** `@/features/portfolio-builder/marketDataService.ts` (wraps `liveMarketDataClient.ts`) + `@/features/portfolio-builder/portfolioOptimizer.ts`
- **MockBadge?** No (but has "placeholder" references in feature components)
- **Risk if mistaken for live:** **Medium** — market data may be mock depending on configuration; optimizer results could be based on mock data
- **Replacement priority:** **Medium** — ensure `marketDataService.ts` always uses live API in production; add MockBadge when using mock data
- **Suggested test:** Verify portfolio builder renders; verify market data source is clearly indicated; verify optimizer results are labeled with data source

---

### Mock Routes (1 route)

#### `/watchlist` — Watchlist  ⚠️

- **Source files:** `apps/dashboard/src/app/watchlist/page.tsx`
- **Data status:** **Mock** — uses `mockPriceWalk` from `@/lib/mock-data`
- **Backend dependency:** None (all data is mock)
- **MockBadge?** **Yes (1 instance):**
  - Line 148: `<MockBadge source="Inline fixture" />`
- **Risk if mistaken for live:** **High** — the entire watchlist is mock. An operator could mistake mock price movements for real market data.
- **Replacement priority:** **High** — replace `mockPriceWalk` with API call to `/markets/bars` or `/markets/quotes`
- **Suggested test:** Verify MockBadge is visible; replace mock data with API call; verify real-time price updates via WebSocket

---

### Demo Routes (2 routes)

#### `/receipts` — Receipts Center

- **Source files:** `apps/dashboard/src/app/receipts/page.tsx`
- **Data status:** **Demo** — uses `buildProofReceiptCenter` (client-side receipt definitions, no API)
- **Backend dependency:** None (static receipt definitions)
- **MockBadge?** No
- **Risk if mistaken for live:** Low — the page is clearly a receipt center, not live trading data
- **Replacement priority:** Low — could be wired to the verification receipt runner API in the future
- **Suggested test:** Verify receipt definitions render; verify links to receipt details work

#### `/signal-cockpit-demo` — Signal Cockpit Demo

- **Source files:** `apps/dashboard/src/app/signal-cockpit-demo/page.tsx` → `@/features/signal-cockpit-demo/signal-cockpit-demo.tsx`
- **Data status:** **Demo** — uses `SignalCockpitDemo` feature component with inline fixtures
- **Backend dependency:** None (all data is demo/fixture)
- **MockBadge?** No (but the route name includes "demo")
- **Risk if mistaken for live:** Low — the route name clearly says "demo"
- **Replacement priority:** Low — this is a UI/UX demo, not a production panel
- **Suggested test:** Verify demo renders; verify interactive controls work

---

### Redirect Routes (2 routes)

#### `/optimizer` — Redirect

- **Source files:** `apps/dashboard/src/app/optimizer/page.tsx`
- **Data status:** Redirect → `/portfolio-builder`
- **Backend dependency:** None
- **Suggested test:** Verify redirect works

#### `/news-lab` — Redirect

- **Source files:** `apps/dashboard/src/app/news-lab/page.tsx`
- **Data status:** Redirect → `/news-impact-lab`
- **Backend dependency:** None
- **Suggested test:** Verify redirect works

---

## Mock Data Sources

### `@/lib/mock-data.ts`

- **Location:** `apps/dashboard/src/lib/mock-data.ts`
- **Exports:** `mockPriceWalk` and other mock data generators
- **Used by:** `/watchlist`, `/symbol/[symbol]`, `@/components/overview/watchlist-preview`
- **Convention:** Every consumer of `mock-data.ts` must display a `<MockBadge>` on the panel that consumes it.

### `@/components/widgets/mock-badge.tsx`

- **Location:** `apps/dashboard/src/components/widgets/mock-badge.tsx`
- **Purpose:** Unmistakable "MOCK" marker for any panel that uses mock data
- **Props:** `source` (description of the mock source), `ticket` (optional issue ticket), `size`
- **Used by:** `/watchlist`, `/symbol/[symbol]`, `@/components/overview/watchlist-preview`

---

## Next Conversion Targets

Based on the route atlas, the highest-priority conversion targets are:

1. **`/watchlist`** (High priority) — entirely mock, high risk if mistaken for live. Replace `mockPriceWalk` with API call to `/markets/bars` or `/markets/quotes`. Add WebSocket for real-time updates.

2. **`/symbol/[symbol]`** (High priority) — hybrid, 3 MockBadge instances. Replace `mock-data.ts` imports for metadata and chart fixtures with API calls. Keep MockBadge until all mock data is replaced.

3. **`/portfolio-builder`** (Medium priority) — hybrid, market data source unclear. Ensure `marketDataService.ts` always uses live API in production; add MockBadge when using mock data.

4. **`/receipts`** (Low priority) — demo, could be wired to the verification receipt runner API.

5. **`/signal-cockpit-demo`** (Low priority) — demo, not a production panel.

---

## References

- `apps/dashboard/src/lib/mock-data.ts` — mock data generators
- `apps/dashboard/src/components/widgets/mock-badge.tsx` — MockBadge component
- `apps/dashboard/src/lib/api.ts` — API client (all live routes use this)
- `apps/dashboard/src/lib/ws.ts` — WebSocket client (live routes with real-time updates)
- `docs/NEXT_STEPS_PLAN.md` — TASK-0201 spec
