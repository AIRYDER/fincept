# Fincept Terminal — UI/UX Upgrade Slice
## Terminal UI Lead · 2026-06-03

This slice delivers the highest-impact pro-terminal upgrade without breaking
any existing functionality. All existing routes, queries, and data flows
are preserved. The new visual language is **additive** — semantic colors
(cyan/green/red/amber/purple) and all existing `Badge`/`StatusPill`/`Card`
behaviour stay untouched.

---

## 1 · UI Audit

A 139-line audit document was written at
`apps/dashboard/docs/ui-audit-2026-06-03.md`. Headlines:

- **Strong**: Bloomberg-style shell, monospace density, semantic status
  colors, well-organized widget kit (`kpi-tile`, `entity-header`,
  `freshness-badge`, `metric-delta`, `confidence-bar`, `sparkline`).
- **Weak**: No watchlist surface. No per-symbol detail page. Charts are
  minimal (no volume, no range chips, no crosshair pin). Mock data looks
  identical to live data. Brand color request (cobalt + orange on OLED
  gunmetal) is unmet. Previous audit (`aestheticaudit.md`) called the
  signal-cockpit-demo "the best contrast surface" but it was a one-off —
  the connected product still read as "dashboard with cards."

---

## 2 · Design System Notes

### Additive brand tokens (`globals.css`)

| Token | Value | Purpose |
|---|---|---|
| `--cobalt` | `218 100% 60%` | Primary brand accent (new) |
| `--cobalt-deep` | `224 90% 38%` | Line gradients / glow |
| `--cobalt-soft` | `218 100% 70%` | Light accent |
| `--orange` | `24 100% 56%` | Secondary brand accent (new) |
| `--orange-deep` | `18 100% 44%` | Bearish gradient |
| `--orange-soft` | `30 100% 68%` | Light bearish accent |
| `--surface-0` | `222 22% 3%` | OLED base |
| `--surface-1/2/3` | layered | Panel elevation |
| `--gunmetal` | `222 14% 16%` | Stronger separator strips |
| `--hairline` | `220 10% 22%` | 1px divider |
| `--led-*` | match semantics | LED dot glow palette |

**Existing tokens are unchanged.** Old `text-primary` (cyan) still works
the same; `text-cobalt` is new and sits alongside it.

### New utility classes

- `.glass` — semi-transparent panel with backdrop blur + inner stroke
- `.glass-hero` — cobalt-tinted variant for hero panels
- `.led` / `.led-sm` / `.led-lg` / `.led-pulse` — glowing dot indicators
- `.dot-matrix` — tabular-nums + extra letter-spacing (ticker feel)
- `.scanlines` — 3%-opacity repeating horizontal lines (instrument panel)
- `.top-glow-cobalt` / `.top-glow-orange` — 1px top hairline accent
- `.text-cobalt*` / `.text-orange*` / `.bg-cobalt-*` / `.bg-orange-*`
- `.border-cobalt*` / `.border-orange*` / `.border-gunmetal` / `.border-hairline`

### New components (`apps/dashboard/src/components/widgets/`)

| File | Purpose |
|---|---|
| `mock-badge.tsx` | Unmistakable dashed-amber MOCK chip; sizes: default / inline / corner |
| `led-dot.tsx` | Glowing status dot (sm/md/lg) + DotMatrix label helper |
| `signal-card.tsx` | Instrument-panel card for predictions/alerts/signals; 3 kinds (prediction/alert/signal); 3 sources (system/model/human); LEDDot + direction bar + confidence + ago |
| `trading-chart.tsx` | Area + volume composed chart, range chips (1D/1W/1M/3M/ALL), last-price horizontal pin, crosshair, scanline overlay, MOCK chip in header |
| `watchlist-row.tsx` | Dense 40px row: LEDDot + symbol + name + cap + last + change + sparkline |

### New atoms in `lib/`

| File | Exports |
|---|---|
| `lib/mock-data.ts` | `withMockFlag`, `isMock`, `seededRandom`, `mockPriceWalk`, `mockVolumeWalk` — all dev-warn-once |
| `lib/design-tokens.ts` (additive) | `BRAND`, `directionOf`, `formatSignedUsd`, `formatSignedPct`, `DIRECTION_CLASS` |

### Mock data discipline

Every mock-backed panel renders a `<MockBadge source="…" />` chip and the
mock helper logs a `MOCK:` warning to the browser console once per
source. This protects the dad-as-operator trust model.

---

## 3 · New Pages

### `/watchlist` (route is new, 3.78 kB)

- 20-row mock watchlist (mega / large / mid / ETF cap tiers)
- Sticky column headers, 40px row height
- Per-row: LEDDot · symbol · name · cap chip · MOCK chip · last · Δ% ·
  sparkline · change %
- Toolbar: filter, pinned-only toggle, sort by symbol/last/change%/volume
- 5-cell summary band: tracked · pinned · advancing · declining · flat
- Every row is a `Link` to `/symbol/{symbol}`
- Mock chip on panel header; row-level MOCK chips on each row

### `/symbol/[symbol]` (route is new, 14.8 kB, dynamic)

- 20 known symbol names + sensible default for unknown symbols
- **Header** — back link, symbol (cobalt), name, cap chip, MOCK chip,
  range/change badge, "Open in Markets" button
- **Hero** — `glass-hero` panel with last price + LEDDot + change, range
  bar, 8-cell quick stats (52W hi/lo, mkt cap, P/E, beta, ADV, yield,
  signals count)
- **Chart** — `TradingChart` (mock, MOCK-flagged) with all 5 range chips
- **Position card** — live data from `/positions` API; shows flat state
  when you have no position
- **Signals** — live `SignalStrip`s when `/models/gbm_predictor/predictions`
  returns rows for the symbol; otherwise 3 mock signals rendered as
  `SignalCard`s
- **News** — 3 mock articles, each linking to `/news?q={symbol}`
- **Mock disclosure** at the bottom of the page enumerates which
  sections are mock

### `/` (home) — Watchlist preview added

- New `WatchlistPreview` card between `FeatureLaunchPanel` and the
  activity/strategies row
- 6 mini watchlist rows using the same `WatchlistRow` component, with
  "Open" link to `/watchlist` and a MOCK chip in the header
- Reuses the same visual grammar so first-fold now reads: header →
  briefing → KPIs → feature control → watchlist → activity

### `/positions` — Sparkline column + glass summary band

- Summary tiles moved into a `glass` band (4-cell grid, hairline
  dividers, monospace values)
- New "Trend" column shows a per-symbol mini sparkline + % change
- MOCK chip in page header disclosing the inline sparkline fixture
- Existing columns, posture logic, pulse-update animation, freshness
  badge, status pill, and live WS upsert all preserved

---

## 4 · Navigation & command-palette wiring

- `nav-tabs.tsx` now includes `WATCH` (mnemonic `WL`) as the 2nd tab
  after OVERVIEW
- `sidebar.tsx` adds `Watchlist` (Star icon) between Overview and
  Positions
- `command-registry.ts` adds the `nav:watchlist` palette command
- `buildEntityResults` for symbols now points to `/symbol/{symbol}`
  (was `/markets?symbol=…`), so entity search in the command palette
  opens the new detail view

---

## 5 · Files Changed (additive / non-breaking)

### New files

```
apps/dashboard/docs/ui-audit-2026-06-03.md
apps/dashboard/src/app/watchlist/page.tsx
apps/dashboard/src/app/symbol/[symbol]/page.tsx
apps/dashboard/src/components/widgets/mock-badge.tsx
apps/dashboard/src/components/widgets/led-dot.tsx
apps/dashboard/src/components/widgets/signal-card.tsx
apps/dashboard/src/components/widgets/trading-chart.tsx
apps/dashboard/src/components/widgets/watchlist-row.tsx
apps/dashboard/src/components/overview/watchlist-preview.tsx
apps/dashboard/src/lib/mock-data.ts
```

### Modified files (additive only)

```
apps/dashboard/src/app/globals.css            — added brand/surface/led tokens + glass/LED/scanline utility classes
apps/dashboard/src/app/page.tsx               — added WatchlistPreview between FeatureLaunchPanel and activity
apps/dashboard/src/app/positions/page.tsx     — added sparkline column, glass summary band, MOCK chip
apps/dashboard/src/components/shell/nav-tabs.tsx          — added WATCH tab
apps/dashboard/src/components/shell/sidebar.tsx           — added Watchlist link
apps/dashboard/src/components/shell/command-registry.ts    — added nav:watchlist + symbol entity now goes to /symbol/{sym}
apps/dashboard/src/lib/design-tokens.ts       — added BRAND, directionOf, formatSignedUsd, formatSignedPct, DIRECTION_CLASS
```

No existing component was renamed or removed. No existing color
semantic was changed. No API contract was altered.

---

## 6 · Before / After Behavior

| Surface | Before | After |
|---|---|---|
| Home dashboard | 2nd fold starts with feature control + activity | Watchlist preview sits between feature control and activity, giving the page a "what I'm watching" anchor |
| Navigation | `OVERVIEW · POSITIONS · ORDERS …` | `OVERVIEW · WATCH · POSITIONS · ORDERS …` |
| Symbol lookup | `Cmd+K` → `AAPL` → routes to `/markets?symbol=AAPL` (no detail) | `Cmd+K` → `AAPL` → routes to `/symbol/AAPL` with full detail view |
| Positions table | 10 columns, no per-symbol trend | 11 columns with per-symbol sparkline + Δ% |
| Position summary | 4 separate bordered cards | Single `glass` band with hairline dividers |
| Mock data | Indistinguishable from live | `<MockBadge>` chips + console `MOCK:` warnings + footer disclosure |
| Brand color | Cyan-only primary | Cobalt (primary) + Orange (secondary) on OLED gunmetal, with glass / LED / scanline grammar |
| Charts | Single `LineChart` with tooltip | `TradingChart` with area + volume + range chips + last-price pin + crosshair + scanline overlay |

---

## 7 · Validation

Run from `apps/dashboard/`:

```bash
# Type check (passes)
npx tsc --noEmit

# Lint (passes)
npx next lint --max-warnings 0 --dir src

# Production build (passes, both new routes included)
npx next build

# Per-feature unit tests (existing tests still pass)
node scripts/run-design-tokens-tests.cjs       # 29 pass (1 pre-existing amber-string failure unrelated)
node scripts/run-page-state-tests.cjs          # 24 pass
node scripts/run-strategy-readiness-tests.cjs  #  4 pass
node scripts/run-command-registry-tests.cjs    # 17 pass (added nav:watchlist coverage)
```

### Manual smoke checklist (when running locally)

1. `/` — Watchlist preview renders below feature control; click any
   row → routes to `/symbol/{SYM}`; MOCK chip visible.
2. `/watchlist` — toolbar filters + sort work; pinned toggle filters
   to 7 pinned rows; each row is clickable; summary band updates.
3. `/symbol/NVDA` — hero shows price + change; chart range chips flip
   the data; MOCK chip on header; position card shows live data (or
   flat state); signals + news sections render.
4. `/positions` — Trend column shows mini sparklines; summary band is
   a single glass panel; existing filter and pulse animation work.
5. `Cmd+K` — type `AAPL` → routes to `/symbol/AAPL`; type `watch` →
   routes to `/watchlist`.

---

## 8 · Followups (not in this slice)

- Wire `/watchlist` to a real watchlist API (replace
  `buildMockWatchlist()` and `withMockFlag`).
- Wire `/symbol/[symbol]` chart to `/data/bars` per-symbol + WS for
  live last-trade tape.
- Wire news section to the real news API filtered by symbol.
- Mobile-first shell rework (header compression, nav overflow,
  hamburger fallback) — was called out in `aestheticaudit.md` and
  remains valid.
- Promote `mock-data.ts` to a typed `MockSource<T>` registry so the
  MOCK badge metadata flows automatically from the data layer up to
  the component layer.

---

## 9 · Risk Notes

- **Zero functional regressions.** All new tokens are additive, all
  new components are pure, and the only modified routes (`/`,
  `/positions`) were touched at the JSX-composition level — no data
  flow, query, or store change.
- **The pre-existing `degraded uses amber` test failure** is a
  test-side bug (the class is `text-warn`, not `text-amber`) that
  predates this slice. Out of scope; tracked in the codebase already.
- **New build sizes**: watchlist 3.78 kB, symbol 14.8 kB — both well
  within the existing 332 kB First Load JS envelope.
