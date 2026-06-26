# Frontend Audit — `apps/dashboard`

> Fincept Terminal operator dashboard. Next.js 14 App Router SPA-style
> application with a Bloomberg-terminal aesthetic, paper-trading safety
> rails, and a strong live-vs-mock data discipline.

---

## 1. Tech Stack & Tooling

| Layer | Choice | Notes |
|---|---|---|
| Framework | **Next.js 14.2.35** (App Router) | `reactStrictMode: true`; optimized package imports for `lucide-react` and `recharts` (`next.config.mjs`). |
| Language | **TypeScript 5.6** | `tsconfig.json` targets `ES2022`, `bundler` module resolution, strict JSX. |
| UI Runtime | **React 18.3** | All pages are `"use client"` — the app is effectively a client-rendered SPA. |
| Styling | **Tailwind CSS 3.4** + `tailwindcss-animate` | Custom finance-semantic color tokens; zero border radius (`--radius: 0px`). |
| Components | **Radix UI** primitives + shadcn/ui pattern | `Button`, `Badge`, `Card`, `Dialog`, `Input`, `ScrollArea`, `Tabs`, `Tooltip`, `Switch`, `Popover`, `DropdownMenu`, `Toast`. |
| Icons | **lucide-react 0.452** | Tree-shaken via `optimizePackageImports`. |
| Charts | **Recharts 2.13** | Area, line, volume bar charts. |
| Animation | **framer-motion 11** | Hover lifts on cards, subtle motion. |
| Data Fetching | **TanStack Query 5.59** | Global defaults: `staleTime 5s`, `refetchOnWindowFocus`, retry ≤3 (skip 401). |
| State | **Zustand 5** | Auth token store (localStorage-backed). |
| Command Palette | **cmdk 1.0** | Global Cmd/Ctrl+K palette. |
| JWT | **jose 5.9** | Token decoding/validation. |
| Date | **date-fns 4.1** | Relative time formatting. |
| Lint | **ESLint** (`next/core-web-vitals`) | `.eslintrc.json`. |
| Tests | **Custom tsx-based runners** (no Jest/Vitest) | 22 test files; each has a `scripts/run-*.cjs` wrapper invoking `tsx`. |

### Scripts (`package.json`)

- `dev` — `next dev -p 3000`
- `build` / `start` — production build & serve
- `lint` — `next lint`
- `typecheck` — `tsc --noEmit`
- 21 granular `test:*` scripts, each running a single component/logic module's test file via `npx tsx`.

### Environment Variables (`.env.example`)

| Variable | Default | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8010` | FastAPI backend base URL. |
| `NEXT_PUBLIC_WS_URL` | *(derived from API URL)* | WebSocket stream endpoint. |
| `NEXT_PUBLIC_DEFAULT_STRATEGY` | — | Default strategy selection. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | External AI service keys (server-side use). |

---

## 2. Design System & Visual Identity

### Aesthetic

The dashboard follows a deliberate **Bloomberg/Fincept terminal aesthetic**:
near-black surfaces, zero corner radius, monospace typography throughout,
ALL-CAPS labels with wide letter-spacing, and bright semantic colors for
financial meaning.

### Color Tokens (`globals.css` + `tailwind.config.ts`)

**Surface palette (HSL):**
- `--background: 0 0% 3%` (near-black)
- `--card: 0 0% 5%`
- `--foreground: 0 0% 92%`
- `--muted-foreground: 0 0% 55%`
- `--border: 0 0% 18%`
- `--radius: 0px` (sharp corners everywhere)

**Terminal semantics:**
- `--long: 142 76% 46%` (green — long/positive)
- `--short: 0 84% 60%` (red — short/negative)
- `--warn: 42 100% 52%` (amber — caution)
- `--cyan: 185 100% 50%` (cyan — system/info)
- `--info: 210 100% 60%` (blue — informational)
- `--cobalt: 220 100% 60%` (brand accent — `#2F6BFF`)
- `--cobalt-soft: 220 55% 38%` (muted brand)
- `--hairline: 217 36% 10%` (subtle dividers)
- `--primary: 28 100% 52%` (orange — primary CTA)

### Typography

- **Font:** `JetBrains_Mono` (Google Fonts) with weights 400–700.
- `font-feature-settings: "tnum" on, "zero" on` — tabular figures for stable column widths.
- Applied globally to `html, body` and reinforced on `table, .num, .font-mono, td, th`.

### Utility Classes (`globals.css`)

- `.widget` / `.widget-header` / `.widget-body` — bordered panel with cyan caps-title.
- `.text-long`, `.text-short`, `.text-warn`, `.text-cyan`, `.text-info` — semantic text colors.
- `.grid-overlay` — subtle scanline/grid background.
- `.gradient-mesh` — radial gradient mesh (login hero only).
- `.scrollbar-thin` — minimal 6px scrollbar.
- `.pulse-update` — row flash animation on WS update (700ms).
- `.live-dot` — 6px pulsing green dot for live indicators.
- `.animate-pulse-slow` — 2.4s pulse.
- `.glass` — (used in watchlist summary band) frosted panel.
- `.dot-matrix` — ticker-style text with edge fade mask.

### Print Styles

Comprehensive `@media print` block inverts to white background, strips
box-shadows, collapses colors to black, and provides `.print-report-page`
and `.print-ink-save` utilities for receipt/report printing.

### Design Tokens (`src/lib/design-tokens.ts`)

A semantic intent system mapping domain states to visual vocabulary:

**Semantic intents:** `verified`, `degraded`, `critical`, `ai`, `healthy`, `inactive`.

Each intent has mappings for: `INTENT_TEXT`, `INTENT_BG`, `INTENT_BORDER`,
`INTENT_DOT`, `INTENT_BADGE_VARIANT`.

**Inference helpers:**
- `healthIntent(status)` → intent from service health.
- `pnlIntent(value)` → long/short/neutral from PnL.
- `sourceIntent(source)` → AI/system/human mapping.
- `freshnessIntent(ageNs)` → verified/degraded/critical from data age.
- `severityIntent(severity)` → info/warning/critical mapping.
- `directionOf(value)` / `formatSignedPct` / `formatSignedUsd` — directional formatting.

**Brand constants:** `BRAND` object with canonical color references.

---

## 3. Application Architecture

### Routing & Layout

```
src/app/
├── layout.tsx          # Root layout: JetBrains_Mono font, dark mode, <Providers>
├── providers.tsx       # TanStack QueryClient + auth hydration
├── globals.css         # Design system
├── page.tsx            # / (Home — KPIs, activity, live streams)
├── login/page.tsx      # /login (JWT token submission)
├── positions/          # /positions
├── orders/             # /orders
├── strategies/         # /strategies + /strategies/[id]
├── quant-foundry/      # /quant-foundry (+ jobs, models, promotion, shadow, tournament)
├── models/             # /models + /models/[name]
├── markets/            # /markets (universe browser + bars chart)
├── watchlist/          # /watchlist (mock-only dense symbol table)
├── symbol/[symbol]/    # /symbol/[symbol] (stock detail)
├── news/               # /news (book-aware news terminal)
├── news-lab/           # /news-lab
├── news-impact-lab/    # /news-impact-lab
├── research/           # /research (Exa + OpenBB research)
├── risk/               # /risk (risk monitor + kill switch)
├── reconciliation/     # /reconciliation (recon checklist)
├── receipts/           # /receipts (proof receipts)
├── system/             # /system (readiness, env, modules)
├── backtest/           # /backtest
├── optimizer/          # /optimizer
├── portfolio-builder/  # /portfolio-builder
├── predictions/        # /predictions
└── signal-cockpit-demo/# /signal-cockpit-demo
```

All pages are `"use client"` — the app is a client-rendered SPA with
no server components doing data fetching. Auth gating happens
client-side via `AppShell` redirecting unauthenticated users to `/login`.

### Root Layout (`layout.tsx`)

- Loads `JetBrains_Mono` via `next/font/google` with `--font-mono` CSS variable.
- Hard-codes `className="dark"` on `<html>` (dark-mode-only design).
- `suppressHydrationWarning` to handle auth hydration.
- Wraps children in `<Providers>`.

### Providers (`providers.tsx`)

- Hydrates JWT token from `localStorage` on mount (avoids SSR hydration mismatch).
- Creates a single `QueryClient` with finance-dashboard defaults:
  - `staleTime: 5_000` (5s)
  - `refetchOnWindowFocus: true`
  - `retry`: up to 3 failures, **but never retries on 401**.

### AppShell (`src/components/shell/app-shell.tsx`)

The authenticated shell wraps every page. If no token is present,
it redirects to `/login`. Composed of:

| Component | File | Role |
|---|---|---|
| `TitleBar` | `shell/title-bar.tsx` | Branding, date/time, user info, status pills (API/OpenBB/WS health), Kill Switch dialog. |
| `SafetyStateBar` | `shell/safety-state-bar.tsx` | Safety chips: mode (Paper), kill switch, API, core services, data coverage, OpenBB, WS. |
| `NavTabs` | `shell/nav-tabs.tsx` | Horizontal nav with Bloomberg-style mnemonics + active highlighting. |
| `StatusBar` | `shell/status-bar.tsx` | Session duration, asset classes, feeds status, memory usage, latency. |
| `CommandPalette` | `shell/command-palette.tsx` | Cmd/Ctrl+K global search/action; dangerous actions route to confirmation pages. |
| `StreamInvalidator` | `shell/stream-invalidator.tsx` | Invalidates TanStack Query caches on WS events (positions/fills/predictions/alerts). |

---

## 4. Data Layer

### API Client (`src/lib/api.ts`)

A typed REST client for the FastAPI backend (`services/api`).

- **Base URL:** `NEXT_PUBLIC_API_URL` (default `http://localhost:8010`).
- **Timeout:** 8s default via `AbortController` (overridable per-call with `timeoutMs`).
- **Error hierarchy** (all extend `ApiError`):
  - `UnauthorizedError` (401) — "Session expired."
  - `UnavailableError` (5xx) — "Backend unavailable."
  - `TimeoutError` — "Request timed out."
  - `ValidationError` (422) — "Validation failed."
  - `StaleError` (409) — "Data is stale."
- Each error carries `status` + `body` so panels can render precise messages.
- Design goal (TASK-0204): "Backend unavailable is not confused with 'no data'."

**API surface** (60+ methods) covers:
- Health/readiness, services, kill-switch state.
- Data: universe, bars, coverage, sources, symbol search, Alpaca demo.
- Positions (global + per-strategy, with `include_flat` toggle).
- Orders (list + place order).
- Strategies (list, configs CRUD, lifecycle start/stop).
- Predictions (list + stats).
- Models (list, train, promote, promotion state, rollback).
- Quant Foundry (health, jobs, dossiers, tournament, shadow, promotion queue).
- News (list, impact signals, optimize, alpha candidates).
- Research (Exa search, OpenBB call/quote/health).
- Backtest (run, list, detail, strategies).
- Modules (list, start/stop, sweep idle, receipts).
- Receipts (proof receipts).

### Types (`src/lib/types.ts`)

TypeScript mirrors of Pydantic schemas from `libs/fincept-core`.
All `Decimal` fields arrive as `string` (Pydantic JSON mode preserves
precision); numeric conversion happens at the edge via `Number()` /
`formatUsd()`. Covers `Position`, `OrderRecord`, `PlaceOrderBody`,
`Prediction`, `StrategyRow`, `StrategyConfigRow`, `Bar`, `UniverseRow`,
`KillSwitchState`, `ServicesResponse`, `WsFrame`, and 40+ Quant Foundry
and news types.

### Auth (`src/lib/auth.ts`)

- **v1 approach:** localStorage-backed JWT (`fincept.token` key).
- Zustand store: `token`, `setToken`, `hydrate`.
- `hydrate()` reads from `localStorage` on mount (called in `Providers`).
- `decodeJwt()` — base64-decodes the JWT payload (no signature verification client-side).
- **Phase H roadmap:** replace with OAuth flow + httpOnly cookies.
- Login page (`/login`) accepts a pasted token, validates via API, stores it.

### WebSocket (`src/lib/ws.ts`)

`useFinceptStream` hook for `/ws/stream`:

- **Auth:** `?token=` query string (browsers can't set WS headers).
- **Subscription:** first frame after connect sends `{"topics": [...]}`.
- **Topics:** `positions`, `fills`, `predictions`, `alerts`.
- **Reconnection:** exponential backoff (1s → 2s → 4s → ... capped at 15s, max 6 attempts).
- **Frame buffer:** bounded ring buffer of 200 recent frames.
- **Status:** `connecting` | `open` | `closed`.
- Malformed frames are silently dropped.
- `StreamInvalidator` component consumes frames and invalidates relevant
  TanStack Query keys so tables repaint without manual refetch.

### Mock Data Discipline (`src/lib/mock-data.ts`)

A first-class concern. The system enforces:
- `withMockFlag()` wrapper for mock data objects.
- `MockBadge` component (`src/components/widgets/mock-badge.tsx`) —
  unmistakable dashed amber border + flask icon. Three sizes:
  `default` (page header), `inline` (table row), `corner` (chart overlay).
- Dev-mode console warnings when mock data is rendered.
- `mockPriceWalk()` / `mockVolumeWalk()` — deterministic seeded walks for charts.

---

## 5. Key Pages & Features

### Home (`/`)

KPI tiles, activity feed, and live prediction/fill/alert streams.
Operator briefing card aggregates safety, services, reconciliation,
and strategies into an at-a-glance status.

### Login (`/login`)

JWT token submission form. Validates token against the API, stores
in `localStorage`. Gradient-mesh hero background.

### Positions (`/positions`)

Table of open positions with filtering, live WebSocket updates
(row pulse animation), PnL calculations using mark price.

### Orders (`/orders`)

Historical order list with status filtering. `PlaceOrderDialog`
component for new orders with order types (market/limit/stop/stop_limit),
time-in-force (gtc/ioc/fok/day), and quick-ticket templates.

### Strategies (`/strategies` + `/strategies/[id]`)

Strategy management: live state, positions, PnL. Search, filtering,
lifecycle controls (start/stop via `LifecycleToggle` with optimistic UI).
`CreateStrategyDialog` for `POST /strategies/configs` with class picker,
symbols input, params editor, and model binding.

### Quant Foundry (`/quant-foundry/*`)

Six sub-pages:
- **Overview** — read-only module health, global mode, cost/budget.
- **Jobs** — job list with status filtering.
- **Models** — dossier registry with artifact hashes, status, evidence.
- **Promotion** — model promotion review/submission/approval.
- **Shadow** — shadow inference health (prediction counts, latencies, circuit-breaker).
- **Tournament** — ranked leaderboard with scores, baseline deltas, decay flags.

### Markets (`/markets`)

Universe browser with symbol search, bars chart (Recharts `LineChart`),
frequency selector (1m/1h/1d), data coverage panel, and Alpaca data demo.
Includes `SourceHealthControlCenter` for source registry/heartbeat/coverage.

### Watchlist (`/watchlist`)

Dense, scannable symbol table — **mock-only** (clearly badged).
Pin favorites, sort by any column, filter by symbol/name/cap tier.
Summary band: tracked, pinned, advancing, declining, flat.
Uses `WatchlistRow` component with sparkline, tone coloring, and LED dot.

### Symbol Detail (`/symbol/[symbol]`)

Bloomberg/TradingView-style layout:
1. Header — symbol, name, last price, change, MOCK chip.
2. `TradingChart` — area + volume, range chips (mock data).
3. Quick stats — 52w hi/lo, ADV, mkt cap, beta, P/E.
4. Your position — real API (if you have one).
5. Active signals — predictions filtered to symbol (real WS).
6. Recent news — mock until per-symbol news API.
7. Strategy exposure — which strategies touch this symbol.

This page is the model for the app's convergence direction: live data
with mock fallbacks that scream "MOCK".

### News (`/news`)

Book-aware news terminal with composite priority scoring.
Three lanes (server-classified by `tier`): ALERT, IMPACT, UNIVERSE.
Auto-refetches every 10s. Each row: age, primary symbol, adverse
indicator, headline, sparkline, $ impact, source.

### Research (`/research`)

Exa AI search (auto/fast/deep/deep-reasoning) + OpenBB dispatch
presets (income, balance, etc.) with evidence stack.

### Risk (`/risk`)

Risk monitor with kill switch dialog, alerts, and circuit-breaker state.

### Reconciliation (`/reconciliation`)

Recon checklist panel aggregating positions, strategies, configs,
universe, coverage, and orders into a pass/warn/fail checklist.

### System (`/system`)

System readiness packet, env var presence detection (names only —
values never read), module control panel, copyable commands.

### Receipts (`/receipts`)

Proof receipt center for audit trails.

---

## 6. Component Library

### UI Primitives (shadcn/ui pattern, Radix-backed)

| Component | Base | Notes |
|---|---|---|
| `Button` | `@radix-ui/react-slot` + CVA | Variants: default, destructive, outline, secondary, ghost, link. Sizes: default, sm, lg, icon. |
| `Badge` | CVA | Variants: default, secondary, destructive, outline, long, short, warn, muted, cyan. |
| `Card` | Custom | `Card`, `CardHeader`, `CardTitle`, `CardDescription`, `CardContent`, `CardFooter`. |
| `Dialog` | `@radix-ui/react-dialog` | Full set including `DialogClose`, `DialogTrigger`. |
| `Input` | Custom | Monospace styled. |
| `ScrollArea` | `@radix-ui/react-scroll-area` | Thin scrollbar. |
| `Tabs` | `@radix-ui/react-tabs` | |
| `Tooltip` | `@radix-ui/react-tooltip` | |
| `Switch` | `@radix-ui/react-switch` | |
| `Popover` | `@radix-ui/react-popover` | |
| `DropdownMenu` | `@radix-ui/react-dropdown-menu` | |
| `Toast` | `@radix-ui/react-toast` | |

### Domain Widgets (`src/components/widgets/`)

| Widget | Purpose |
|---|---|
| `SignalCard` | Compact instrument-panel card for predictions/alerts/signals. Variants: prediction, alert, signal. Carries LEDDot, source badge (AI/SYSTEM/HUMAN), direction bar, confidence opacity, MOCK chip, chevron link. |
| `SignalStrip` | Compact horizontal strip variant of SignalCard. |
| `TradingChart` | Recharts area + volume bars, range chips (1D/1W/1M/3M/1Y), last-price pin. |
| `WatchlistRow` | Dense table row with sparkline, tone coloring, LED dot, pin toggle. |
| `LEDDot` | Glowing status dot (sm/md/lg) with tone (long/short/warn/info/cyan/muted) + pulse. Uses `currentColor` for fill+glow. |
| `DotMatrix` | Ticker-style text with tabular-nums, letter-spacing, edge fade mask. |
| `MockBadge` | Unmistakable MOCK marker — dashed amber border + flask icon. Sizes: default, inline, corner. |
| `StatusPill` | Semantic intent pill with optional dot. |
| `EmptyState` | Consistent empty-state placeholder. |
| `PageHeader` | Standard page header with title, description, action slot. |

### Domain Components

- **Overview:** `OperatorBriefingCard` (aggregates 8 queries into briefing packet), `WatchlistPreview`.
- **Predictions:** `ProductionSignalCockpit` (read-only signal readiness from predictions + services + promotion).
- **Data:** `SourceHealthControlCenter` (source registry, OpenBB, heartbeat, coverage).
- **Strategies:** `LifecycleToggle` (optimistic start/stop), `CreateStrategyDialog`, `ClassPicker`, `ParamsEditor`, `SymbolsInput`.
- **Models:** `PromoteButton` (one-click model promotion with active/pending/error states).
- **Orders:** `PlaceOrderDialog`.
- **Reconciliation:** `ReconChecklistPanel`.
- **News:** `NewsIntelligencePanel`.
- **System:** `ModuleControlPanel`.
- **Evidence:** `EvidenceStack`.

---

## 7. Testing

### Approach

The project uses a **custom test harness** rather than Jest or Vitest.
Each test file is a standalone script that:

1. Defines a local `test()` registrar collecting `{ name, fn }` pairs.
2. Runs them sequentially with `assert` from `node:assert`.
3. Logs `ok - <name>` / `not ok - <name>` and sets `process.exitCode = 1` on failure.

Component tests use `renderToStaticMarkup` from `react-dom/server` for
SSR-based HTML assertion (no DOM testing library). Logic tests import
pure functions and assert on return values.

### Test Runner Scripts

Each test file has a `scripts/run-*.cjs` wrapper that invokes:
```
npx --yes tsx --tsconfig tsconfig.test.json "<test-file>"
```
with `stdio: "inherit"`. The `tsconfig.test.json` enables the automatic
JSX runtime so Next.js components can render outside Next.js.

### Test Coverage (22 files)

| Area | Test File | What's Covered |
|---|---|---|
| Design tokens | `lib/design-tokens.test.ts` | Every intent has text/bg/border/dot/badge mappings. |
| Signal card | `widgets/signal-card.test.tsx` | Title/symbol/kind rendering, source badges, MOCK chip, severity, direction bar, confidence, metric, SignalStrip. |
| LED dot | `widgets/led-dot.test.tsx` | Size/tone classes, pulse. |
| Mock badge | `widgets/mock-badge.test.tsx` | Sizes, tooltip, source/ticket. |
| Trading chart | `widgets/trading-chart.test.tsx` | Range chips, last-price pin. |
| Watchlist row | `widgets/watchlist-row.test.tsx` | Tone, sparkline, pin. |
| Watchlist preview | `overview/watchlist-preview.test.tsx` | Preview rendering. |
| Operator briefing | `overview/operator-briefing.test.ts` | Briefing packet from fixtures. |
| Signal cockpit | `predictions/signal-cockpit.test.ts` | Cockpit state/score/checks. |
| Source health | `data/source-health.test.ts` | Health summary. |
| Model dossier | `models/model-dossier.test.ts` | Dossier registry. |
| Shadow news impact | `news-impact/shadow-news-impact-panel.test.tsx` | Panel rendering. |
| News intelligence | `news/news-intelligence.test.ts` | News panel. |
| Position posture | `positions/position-posture.test.ts` | Posture inference. |
| Strategy readiness | `strategies/strategy-readiness.test.ts` | Readiness state. |
| System readiness | `system/system-readiness.test.ts` | Readiness packet. |
| Page state | `states/page-state.test.ts` | Page state machine. |
| Command registry | `shell/command-registry.test.ts` | Command palette registry. |
| Recon checklist | `reconciliation/recon-checklist.test.ts` | Checklist logic. |
| Proof receipts | `receipts/proof-receipts.test.ts` | Receipt logic. |
| Backtest lab | `backtest/backtest-lab.test.ts` | Backtest logic. |
| Portfolio builder | `features/portfolio-builder/portfolioBuilder.test.ts` | Portfolio logic. |

### Gaps

- **No end-to-end (E2E) tests** (no Playwright/Cypress).
- **No integration tests** against a running backend.
- **No DOM interaction tests** (clicking, typing) — only SSR HTML assertions.
- **No visual regression tests.**
- **No test for auth flow** (login, token hydration, 401 redirect).
- **No test for WebSocket reconnection logic.**
- Tests are siloed (one runner per file) — no aggregate `test` script;
  CI would need to run all 21 `test:*` scripts individually.

---

## 8. Safety & Operator Concerns

### Paper-Trading Mode

The app is explicitly an **operator dashboard for paper trading**.
`SafetyStateBar` prominently displays "Paper" mode. The README confirms
Phase H will harden auth.

### Kill Switch

- `TitleBar` has a Kill Switch dialog for emergency actions.
- `/risk` page has a dedicated kill switch section.
- Kill switch state is polled (30s interval in briefing).
- `CommandPalette` routes dangerous actions to confirmation pages
  rather than executing directly.

### Read-Only Quant Foundry

Quant Foundry pages are **read-only by design** — the overview shows
"No execution" badges. Promotion requires explicit review/approval flow.

### Mock vs. Live Discipline

This is a standout strength. The system makes it visually impossible
to confuse mock data with live data:
- `MockBadge` (dashed amber + flask icon) on every mock panel/row.
- `withMockFlag()` wrapper + dev console warnings.
- `/watchlist` and `/symbol/[symbol]` chart sections are explicitly
  badged as mock with comments explaining the migration path.

### Error Handling

The typed error hierarchy (`UnauthorizedError`, `UnavailableError`,
`TimeoutError`, `ValidationError`, `StaleError`) ensures operators
see precise messages: "Backend unavailable" vs. "No data" vs.
"Session expired." TanStack Query skips retries on 401.

---

## 9. Performance Observations

### Strengths

- `next/font` self-hosts JetBrains Mono (no FOUT).
- `optimizePackageImports` for `lucide-react` and `recharts` reduces bundle.
- Single `QueryClient` instance (no re-creation on render).
- Bounded WS frame buffer (200 max) prevents memory leaks.
- Tabular figures prevent layout shift in numeric tables.
- `staleTime: 5s` prevents hammering the backend on focus refetch.

### Concerns

- **All pages are `"use client"`** — no server-side data fetching or
  streaming SSR. Initial load fetches everything client-side after
  hydration. For a trading dashboard this is acceptable (auth-gated,
  real-time), but it means no SEO and a heavier client bundle.
- **No code-splitting beyond route-level** — each page imports its
  full dependency tree. Recharts and framer-motion are heavy.
- **Polling intervals are aggressive** in places: operator briefing
  polls 8 endpoints at 15–60s intervals simultaneously; `/news`
  refetches every 10s. On a slow backend this could compound.
- **No `React.memo` or `useMemo` on expensive chart renders** in
  several pages (though some use `useMemo` for derived data).
- **No image optimization** — but the app has essentially no images.

---

## 10. Accessibility

### Observations

- Radix UI primitives provide keyboard navigation, focus management,
  ARIA attributes for dialogs, menus, tabs, tooltips, etc.
- `LEDDot` includes `aria-label` (via `title` prop).
- `suppressHydrationWarning` on `<html>` for auth hydration.
- Color contrast: near-black background with 92% foreground text is
  high-contrast. However, `muted-foreground` at 55% lightness on 3%
  background may fail WCAG AA for small text.
- **No skip-to-content link.**
- **No visible focus styles beyond browser defaults** (the design
  relies on sharp borders but doesn't define explicit `:focus-visible`
  rings in `globals.css`).
- Semantic colors (long/short/warn) convey meaning by color alone
  in some places — though most have accompanying icons/text.
- `CommandPalette` (cmdk) is keyboard-accessible by design.
- No `aria-live` regions for WebSocket-driven updates (screen readers
  won't announce live position/fill changes).

---

## 11. Code Quality & Conventions

### Strengths

- **Excellent documentation:** Nearly every file and component has
  thorough JSDoc/TSDoc comments explaining the "why" — not just the
  "what." Examples: `api.ts` error hierarchy rationale, `ws.ts`
  protocol details, `mock-data.ts` discipline, `signal-card.tsx`
  variant taxonomy.
- **Consistent patterns:** Every page follows `AppShell` → `PageHeader`
  → content. Every query uses `enabled: !!token` gating. Every
  mutation invalidates relevant query keys.
- **Type safety:** `types.ts` mirrors Pydantic schemas with
  `Decimal`-as-`string` discipline. `tsc --noEmit` typecheck script.
- **Single-source utility functions:** `formatUsd`, `formatNumber`,
  `formatPercent`, `nsToDate`, `pnlClass` in `utils.ts`.
- **Design token system:** Semantic intents prevent ad-hoc color usage.

### Concerns

- **No Prettier config** — formatting may drift across contributors.
- **No Husky / lint-staged** — no pre-commit hooks enforce lint/typecheck.
- **No CI config visible** in the dashboard package.
- **`decodeJwt` uses `atob`** without try/catch validation of token
  structure beyond a null check (though it does catch errors).
- **Client-side token storage** in `localStorage` is XSS-vulnerable
  (acknowledged in README; Phase H will fix with httpOnly cookies).
- **No CSP headers** configured in `next.config.mjs`.
- **Some large page components** (e.g., `/news`, `/risk`, `/research`)
  are single files with significant inline logic — could benefit from
  extraction into smaller components/hooks.

---

## 12. Documentation

### In-Repo Docs

| File | Content |
|---|---|
| `README.md` | Overview, features, run/verify instructions, troubleshooting, architecture notes, conventions. Notes Phase H auth upgrade. |
| `docs/ui-audit-2026-06-03.md` | UI/UX audit: strengths (design DNA, consistent shell), weaknesses (dashboard-like feel, no watchlist/symbol detail, minimal charts, mock/live ambiguity), recommendations. |
| `docs/ui-upgrade-2026-06-03.md` | UI/UX upgrade plan: brand tokens, utility classes, new components (`mock-badge`, `led-dot`, `signal-card`, `trading-chart`, `watchlist-row`), nav changes for watchlist + symbol detail. |

### Inline Documentation

The codebase is exceptionally well-commented. Key examples:
- `api.ts` — error hierarchy rationale, timeout design (TASK-0204).
- `ws.ts` — WS protocol, auth via query string, reconnection strategy.
- `auth.ts` — v1 approach + Phase H roadmap.
- `mock-data.ts` — mock discipline enforcement.
- `signal-card.tsx` — variant taxonomy and visual semantics.
- `watchlist/page.tsx` — "why this exists" and data source migration path.
- `symbol/[symbol]/page.tsx` — section-by-section layout documentation.

---

## 13. Summary Assessment

### Strengths

1. **Cohesive design system** — Bloomberg-terminal aesthetic executed
   consistently with semantic color tokens, monospace typography, and
   zero-radius sharp panels.
2. **Safety-first operator UX** — paper mode badges, kill switch,
   read-only quant foundry, confirmation pages for dangerous actions.
3. **Mock/live data discipline** — best-in-class `MockBadge` system
   making mock data visually unmistakable.
4. **Typed error hierarchy** — operators see precise messages
   (timeout vs. unavailable vs. unauthorized vs. stale).
5. **Real-time integration** — WebSocket with auto-reconnect,
   exponential backoff, and TanStack Query cache invalidation.
6. **Excellent inline documentation** — every file explains the "why."
7. **Comprehensive feature surface** — 30 pages covering trading,
   quant research, risk, reconciliation, news, and system ops.
8. **Design token abstraction** — semantic intents decouple domain
   meaning from raw colors.

### Weaknesses

1. **Auth security** — localStorage JWT is XSS-vulnerable (acknowledged,
   Phase H will address).
2. **No E2E or integration tests** — only unit/SSR-render tests; no
   coverage for auth flow, WS reconnection, or full page interactions.
3. **All-client rendering** — no SSR/streaming; heavier initial load.
4. **Accessibility gaps** — no skip link, no `aria-live` for WS updates,
   muted-foreground contrast may fail WCAG AA, no explicit focus rings.
5. **No pre-commit hooks or CI** — lint/typecheck not enforced automatically.
6. **Aggressive polling** — multiple pages poll many endpoints at short
   intervals; could strain backend or browser under load.
7. **Some monolithic page components** — `/news`, `/risk`, `/research`
   could be decomposed.
8. **No CSP or security headers** in Next.js config.

### Recommendations

1. **Phase H auth** — implement httpOnly cookie + OAuth as planned.
2. **Add Playwright E2E tests** for critical flows: login → view
   positions → place order → kill switch.
3. **Add `aria-live` regions** for WebSocket-driven table updates.
4. **Configure security headers** (CSP, X-Frame-Options, etc.) in
   `next.config.mjs`.
5. **Add a unified `test` script** that runs all 21 `test:*` scripts
   for CI.
6. **Add Husky + lint-staged** for pre-commit lint/typecheck.
7. **Decompose large pages** (`/news`, `/risk`, `/research`) into
   smaller sub-components and custom hooks.
8. **Add `React.memo` / `useMemo`** to chart-heavy pages to prevent
   unnecessary re-renders on parent state changes.
9. **Consider server components** for read-only pages (e.g., receipts,
   system) to reduce client bundle.
10. **Add a skip-to-content link** and explicit `:focus-visible` styles.
