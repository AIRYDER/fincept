# Phase U · UI + API — Agent Prompts

**Tasks:** TASK-050 (FastAPI), TASK-051 (WebSocket), TASK-052 (Next.js shell), TASK-053 (positions panel), TASK-054 (strategy control), TASK-055 (live chart), TASK-056 (command palette), TASK-057 (risk panel + kill switch)
**Checkpoint:** Operator can sign in, see live P&L update at 10 Hz, start/stop a strategy, and trigger kill switch in under 3 seconds end-to-end.

---

## Phase kickoff

```text
You are now implementing the human-facing surface. Until now, agents talked only to other services. From this phase on, traders watch live P&L tick by tick and decide whether to intervene. The UI's job: present truth, surface anomalies, and make the kill switch one keystroke away.

PHASE-SPECIFIC RULES:

1. THE UI IS A READ MODEL. Period. The Next.js dashboard renders state served by services/api. It does NOT compute P&L, does NOT enforce risk limits, does NOT decide trades. Business logic lives in services/. UI bugs must never produce wrong trades.

2. AUTH ON EVERY MUTATING ENDPOINT. Every POST/DELETE in services/api requires a valid JWT (HS256 for MVP). No anonymous mutating endpoints. Reads can be authenticated optionally; mutations always.

3. KILL SWITCH IS A FIRST-CLASS UI ELEMENT. Always visible. Always one click + confirm. The button must work even if other panels error. Treat the kill switch as a separate component with its own error boundary.

4. LATENCY BUDGETS:
   - HTTP read endpoints: p99 < 200ms.
   - WebSocket per-message: p99 < 50ms server-side.
   - UI initial paint: p99 < 1.5s.
   - Position panel update: 10Hz max (don't render every tick — coalesce).

5. NO BUSINESS LOGIC IN COMPONENTS. React components are thin. Data fetching via typed API client. State via React Query (server state) + Zustand (UI state). Components do not compute Sharpe, do not transform symbols, do not validate orders.

6. DESIGN-LANGUAGE CONSISTENCY. shadcn/ui + Tailwind. Bloomberg-derived dark theme (almost-black backgrounds, monospace numerics, semantic colors: green=long, red=short, amber=warning). Don't reinvent the wheel each component.

7. ACCESSIBILITY MINIMUM. Keyboard navigation works. Screen-reader labels on every interactive element. Color is never the sole information channel (always pair with icon or text).

CONTEXT TO LOAD:
- spec/CONTRACTS.md §11 (HTTP API shape).
- spec/CONTRACTS.md §6 (stream names — what WebSocket subscribes to).
- libs/fincept-bus consumer pattern.
- libs/fincept-db read helpers.

WHEN STUCK:
- WebSocket dropping under load? Check that you're using consumer groups (each WS gets its own group with consumer_id=ws-{conn_id}); without unique groups, all WSs steal each other's messages.
- UI re-rendering too often? React Query subscriptions create render storms; use `select` and `notifyOnChangeProps` to scope. For high-frequency data (price ticks), bypass React state and use `requestAnimationFrame` with a ref-based update.
- Auth token expired? Refresh flow not in MVP — re-login required. Document this in the dashboard README.

Acknowledge by listing the 7 rules. Wait for the first task.
```

---

## TASK-050 prompt — FastAPI HTTP + WebSocket app

```text
Implement TASK-050 from spec/tasks/TASK-050-api.md.

Specific landmines:
- Auth: HS256 with JWT_SECRET from env. NO algorithm confusion vulnerability — hardcode `algorithms=["HS256"]`.
- CORS: only `http://localhost:3000` for dev. Production CORS must be configured per deployment; do NOT use `allow_origins=["*"]`.
- Pydantic models in routes: return them directly; FastAPI serializes via `.model_dump(mode='json')`. Do NOT manually convert.
- Decimal serialization: by default FastAPI converts Decimal to float (precision loss). Configure custom JSON encoder to emit strings. Test: roundtrip a Decimal('1.123456789') and verify no precision loss.
- All mutating routes accept `X-Idempotency-Key` header; first-write-wins by checking Redis SET NX EX 86400.
- WebSocket auth: send JWT in first message after connect. Reject + close if invalid.
- WebSocket subscriptions: client sends `{"topics": ["positions", "fills", "predictions"]}`. Server creates a consumer group per (topic, conn_id).

Append spec/tasks/TASK-050-api.md and implement.

Verification:
  uv run pytest services/api
  # Health, auth, kill-switch tests pass.

Manual:
  uv run uvicorn api.main:app --reload &
  curl http://localhost:8000/health   # 200
  curl -X POST http://localhost:8000/kill-switch -d '{"reason":"x"}'   # 401
  TOKEN=$(python -c "import jwt; print(jwt.encode({'sub':'alice'}, 'dev-only-change-me', algorithm='HS256'))")
  curl -X POST http://localhost:8000/kill-switch -H "Authorization: Bearer $TOKEN" -d '{"reason":"drill"}'   # 200
```

---

## TASK-051 prompt — WebSocket subscriptions

```text
Implement TASK-051 — extend the WS endpoint into a robust streaming surface.

Files:
- services/api/src/api/ws.py (already started in TASK-050) — extend with subscription management, backpressure, reconnection helpers.

Subscription contract (client → server):
  {"action": "subscribe", "topics": ["positions", "fills", "predictions"]}
  {"action": "unsubscribe", "topics": ["fills"]}
  {"action": "ping"}

Server → client envelope:
  {"topic": "positions", "ts": <ns>, "data": {...}}
  {"topic": "fills", "ts": <ns>, "data": {...}}
  {"topic": "_pong"}

Backpressure:
- If the client doesn't read for 5 seconds (server-side send buffer fills), drop the connection.
- Default rate-limit per topic: 10 messages/sec. Excess messages coalesced (latest wins for "positions"; dropped for "predictions" since they're event-shaped).

Reconnect:
- Server emits `last_event_id` in each message; client can include it on reconnect to resume from the last seen point. Implementation: stream X-IDs are monotonic, so the server passes that ID to xreadgroup.

Author spec/tasks/TASK-051-ws.md, implement.

Verification:
  # Run with autobahn or websockat
  websocat ws://localhost:8000/ws/stream
  # Manually send subscription. Watch messages flow.

Load test:
  python tests/perf/ws_load.py --connections 100 --duration 60s
  # Server handles 100 concurrent connections, no message loss for 60s.
```

---

## TASK-052 prompt — Next.js dashboard shell

```text
Implement TASK-052 — the dashboard application skeleton.

Files (apps/dashboard/):
- next.config.ts — strict mode, typed routes, App Router.
- src/app/layout.tsx — root layout: Inter font, dark theme by default, toaster for notifications.
- src/app/page.tsx — overview page (skeleton; actual widgets in TASK-053+).
- src/lib/api.ts — typed API client (use openapi-typescript codegen or write manually against spec/CONTRACTS.md §11).
- src/lib/ws.ts — WebSocket React hook (`useStream(topics)`).
- src/lib/auth.ts — NextAuth or simple JWT cookie auth.
- src/components/ui/ — shadcn/ui generated components.
- src/components/layout/sidebar.tsx — left nav.
- src/components/layout/header.tsx — top bar with user + kill switch.

Stack:
- Next.js 16 + React 19 + TypeScript strict.
- TailwindCSS + shadcn/ui.
- TanStack Query for server state.
- Zustand for UI state.
- TradingView Lightweight Charts for charts (TASK-055).
- cmdk for command palette (TASK-056).

Specific landmines:
- App Router only. No Pages Router.
- Server Components by default; "use client" only when necessary (interactive panels).
- React Query QueryClient lives in a Providers component wrapped around children.
- Type all API responses by importing from a generated types file (regenerate via openapi-typescript on api changes).
- env vars: NEXT_PUBLIC_API_URL, NEXT_PUBLIC_WS_URL.

Author spec/tasks/TASK-052-dashboard-shell.md, implement.

Verification:
  cd apps/dashboard
  pnpm dev
  # http://localhost:3000 renders the shell.
  # Navigation between routes works.
  # Login page redirects after auth.
```

---

## TASK-053 prompt — Positions + P&L panel

```text
Implement TASK-053 — the live positions table.

Files:
- apps/dashboard/src/app/positions/page.tsx — page route.
- apps/dashboard/src/components/panels/positions-table.tsx — virtualized table.
- apps/dashboard/src/hooks/use-positions.ts — data hook (React Query for snapshot + WS for live updates).

Table columns:
- Symbol | Strategy | Quantity | Avg Cost | Mark Price | Unrealized P&L | Realized P&L | Total P&L | % NAV

Behavior:
- Initial load: GET /positions snapshot.
- WS subscribe to "positions" + "fills" — merge updates into local state.
- 10Hz max render rate. Coalesce updates in a ref between frames; flush via requestAnimationFrame.
- Sort by P&L (descending) by default. Click any column to re-sort.
- Color: green for positive P&L, red for negative; monospace digits for alignment.
- Filter: search box (symbol contains), strategy dropdown (multi-select).
- Empty state: "No open positions" with link to start a strategy.
- Sparkline of recent P&L history per row (last 100 fills).

Specific landmines:
- Decimal arithmetic in TS: USE bignumber.js or string math; never rely on Number for currency.
- React Query staleTime for positions: 0 (always check) but the WS push keeps it fresh; HTTP refetch only on focus.
- Virtualization: use @tanstack/react-virtual for 1000+ rows. Without it, rendering crawls.
- Don't re-create TradingView charts on every render; use `useMemo` and stable refs.

Author spec/tasks/TASK-053-positions-panel.md, implement.

Verification:
  # Backend running with paper OMS in TASK-O.
  cd apps/dashboard && pnpm dev
  # Open /positions. See live updates as fills hit ord.fills.
  # Lighthouse perf score > 90.
```

---

## TASK-054 prompt — Strategy control panel

```text
Implement TASK-054 — start/stop strategies and edit params live.

Files:
- apps/dashboard/src/app/strategies/page.tsx — list of strategies.
- apps/dashboard/src/app/strategies/[id]/page.tsx — detail view.
- apps/dashboard/src/components/panels/strategy-form.tsx — Pydantic-driven form renderer.

List view:
- Table: Strategy ID | Status (running/stopped) | P&L (today / 7d / 30d) | Sharpe | Max DD | Actions (start, stop, edit, view backtest).

Detail view:
- Param editor: introspect strategy class via /strategies/{id}/schema endpoint (returns the Pydantic schema). Render appropriate inputs (number, slider, dropdown).
- Live signal feed: last 50 predictions/decisions from this strategy.
- Recent fills.
- Mini equity curve.

Start/stop:
- POST /strategies/{id}/start | POST /strategies/{id}/stop with confirmation modal.
- Optimistic UI update; revert if backend rejects.

Specific landmines:
- Param edits must be staged: edit form → preview diff → confirm → POST. Never POST on every keystroke.
- Strategy lifecycle: a "running" strategy is actually the strategy_runner process subscribed to its config in Redis. The UI flips a flag; the runner detects and acts.
- Validation: Pydantic schema → JSON schema → form validation client-side. But ALSO validate server-side; never trust client.

Author spec/tasks/TASK-054-strategy-control.md, implement.

Verification:
  # Backend with strategy runner from TASK-O.
  # In UI, start a strategy. Verify Redis hash strategies:registry shows "running".
  # Stop. Verify "stopped".
  # Edit a param. Verify the running strategy adopts the new value within 5 seconds.
```

---

## TASK-055 prompt — Live chart with fill overlays

```text
Implement TASK-055 — TradingView Lightweight Charts integration.

Files:
- apps/dashboard/src/components/chart/live-chart.tsx — chart component.
- apps/dashboard/src/components/chart/fill-overlay.tsx — fill markers.
- apps/dashboard/src/hooks/use-bars.ts — bar data hook (REST initial + WS live).

Chart features:
- Candlestick + volume (toggleable).
- Timeframe selector: 1m, 5m, 15m, 1h, 4h, 1d.
- Symbol selector.
- Crosshair with OHLCV readout.
- Fill markers: green up-arrow for BUY fills, red down-arrow for SELL fills, with price + qty in tooltip.
- Strategy filter: show only fills from selected strategy.
- Pan / zoom with smooth WebGL rendering (Lightweight Charts uses canvas2d, fast enough).

Performance:
- Live updates: append new bar via chart.update() — never replace the whole series.
- Resize observer to handle layout changes; debounce 100ms.
- Worker-side bar aggregation for sub-1m timeframes if backend doesn't pre-aggregate.

Specific landmines:
- TradingView Lightweight Charts has its own time format (UTCTimestamp seconds, not ns). Convert: `Math.floor(ts_event_ns / 1_000_000_000)`.
- Initial load: request enough bars to fill the visible window (~500). Subsequent panning fetches more lazily.
- Color picks must match the rest of the dashboard semantics.

Author spec/tasks/TASK-055-live-chart.md, implement.

Verification: visually inspect on multiple symbols; toggle strategies; pan/zoom must remain smooth at 60fps.
```

---

## TASK-056 prompt — Command palette (Bloomberg-style mnemonics)

```text
Implement TASK-056 — keyboard-driven navigation à la Bloomberg.

Files:
- apps/dashboard/src/components/command-palette/index.tsx — cmdk integration.
- apps/dashboard/src/components/command-palette/commands.ts — command definitions.

UX:
- Trigger: ⌘K (Mac), Ctrl+K (Win/Linux), or click search icon.
- Modal overlay with input field.
- Type: "BTC GP" → opens BTC-USD chart. "AAPL DES" → opens AAPL company description (placeholder until that page exists).
- Top-level commands: GP (graph price), DES (description), POS (positions for symbol), STR (strategy detail).
- Ranked fuzzy search; recent commands at top.
- Esc to close.

Mnemonic vocabulary (extensible map):
- `<symbol> GP` → /chart/<symbol>
- `<symbol> POS` → /positions?symbol=<symbol>
- `<strategy_id> STR` → /strategies/<strategy_id>
- `KILL` → triggers kill-switch confirmation modal
- `LOGOUT` → signs out
- `?` → help

Specific landmines:
- Keyboard handler at the document level — wrap in useEffect with cleanup.
- Don't capture ⌘K when an input has focus (avoid hijacking text fields). Use `event.target` check.
- Persist recent commands in localStorage.

Author spec/tasks/TASK-056-command-palette.md, implement.

Verification: power-user flow — open dashboard, hit ⌘K, type "BTC GP", land on chart in <500ms perceptual time.
```

---

## TASK-057 prompt — Risk panel + kill switch UI

```text
Implement TASK-057 — risk dashboard and the prominent kill switch.

Files:
- apps/dashboard/src/components/panels/risk-panel.tsx — VaR, exposures, limit utilization.
- apps/dashboard/src/components/panels/kill-switch-button.tsx — the big red button.
- apps/dashboard/src/app/risk/page.tsx — full risk page.

Risk panel:
- Real-time portfolio VaR (99%) — green if < 50% of max, amber if 50-80%, red if > 80%.
- Gross / net notional.
- Per-symbol exposures table.
- Per-strategy P&L heatmap.
- Daily P&L vs daily loss limit bar.

Kill switch:
- Always visible in header.
- Click → confirmation modal with text input ("type KILL to confirm").
- POST /kill-switch with reason from a dropdown (manual / drill / risk-breach / system-anomaly / other).
- Visual state: idle (gray), armed (modal open), active (red banner across entire dashboard).
- When active, displays "TRADING HALTED — <reason>" + clear button.

Specific landmines:
- Race against own UI: the kill-switch state must be observable across all open tabs/sessions. Use a shared subscription (BroadcastChannel) to sync.
- Always-visible: position absolute / sticky in the header, but tab-stop the focus into it on hover so keyboard users find it.
- Audit trail: every kill-switch press logs to audit_log with username + reason + timestamp via /kill-switch endpoint.

Author spec/tasks/TASK-057-risk-panel.md, implement.

Verification:
  # End-to-end: trigger kill switch from UI.
  # Backend services log "kill_switch.activated" within 1 second.
  # No new orders pass risk gate.
  # Kill-switch banner visible in all open browser tabs.
```

---

## Phase U exit verification

```text
Run the Phase U checkpoint validation:

1. Operator dry-run (timed):
   - Sign in. Stopwatch starts.
   - Navigate to /positions. Verify live P&L updates visible at ~10Hz.
   - Open command palette, jump to a strategy detail page.
   - Click stop on the strategy. Verify it stops.
   - Open kill switch modal, type KILL, confirm.
   - Verify red banner appears.
   - Verify backend services received the kill (logs).
   - Stop the timer.

   Target: under 3 seconds from kill-switch click to backend log of activation. Total flow < 90 seconds.

2. Performance:
   - Lighthouse perf score on /positions: ≥ 85.
   - WebSocket latency p99 (tick → render): < 100ms.
   - 100 concurrent positions in the table render at 60fps.

3. Auth:
   - All mutating endpoints reject anonymous (401).
   - Expired JWT rejected (401, "expired" in body).

4. Multi-tab kill-switch sync:
   - Open dashboard in two tabs.
   - Trigger kill switch in tab 1.
   - Tab 2 banner appears within 1 second.

5. CI:
   - apps/dashboard tests + Lighthouse run in CI.
   - lint + typecheck (TypeScript strict) green.

If all five pass, declare Phase U COMPLETE. Mark tasks 050–057 as [x]. Add "Checkpoint U: passed YYYY-MM-DD". Proceed to spec/prompts/phase-X-cutting-edge.md.

If kill-switch latency > 3s or sync between tabs fails, do NOT advance — Phase X adds LLM agents which require even tighter human oversight.
```
