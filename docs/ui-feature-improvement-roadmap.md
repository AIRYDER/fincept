# UI and Feature Improvement Roadmap

Last updated: 2026-05-10

## Purpose

This document captures the biggest UI and feature improvements available from the current Fincept Terminal codebase. It is intentionally grounded in the existing implementation: dashboard routes, API surfaces, OpenBB/research tooling, paper-spine receipts, strategy-host work, model lifecycle routes, news-impact experiments, reconciliation flows, and portfolio-builder internals.

The goal is not to add random pages. The goal is to turn Fincept into a sharper operator terminal: safer, more explainable, more evidence-driven, more beautiful, and more useful under real local/paper-trading conditions.

## Current baseline

Fincept already has a broad operator console:

- Overview
- Positions
- Orders
- Strategies and strategy detail pages
- Portfolio builder / optimizer
- News
- News lab
- News impact lab
- Predictions
- Markets
- Backtest
- Models and model detail pages
- Risk
- Research
- Reconciliation
- Signal cockpit demo

Backend capabilities already include:

- FastAPI routes for data, control, strategies, models, news, news impact, orders, positions, regime, research, services, and backtests.
- OpenBB quote, dispatcher, health, history, and readiness diagnostics.
- Exa-style research surface.
- Strategy config persistence and strategy-host runtime boundary.
- Model train/promote/shadow/prediction/log lifecycle pieces.
- Paper-spine replay receipt proving the local data-to-portfolio chain.
- Route-smoke and OpenBB live proof scripts.
- Portfolio optimizer candidate diagnostics, efficient-frontier internals, Black-Litterman helpers, CVaR proxy, scenario war-room stress surface, and AI report generation.

## Design principles for all improvements

1. **Safety state must be persistent.** Operators should always know whether the system is read-only, paper, sim, degraded, or live-gated.
2. **Every claim needs evidence.** Research, model predictions, optimizer outputs, and recommendations should show freshness, source, confidence, and raw trace links.
3. **AI should be structured, not chat-first.** Use fixed operator rails: focus, detected change, why it matters, suggested checks, caveats, evidence IDs.
4. **External providers must degrade cleanly.** OpenBB, Exa, Alpaca, NewsAPI, model artifacts, Redis, and Timescale failures should be distinct, visible, and non-mysterious.
5. **Receipts beat vibes.** Important workflows should produce JSON receipts or audit history: replay, route smoke, optimizer stress, model promotion, strategy start, manual order, research request.
6. **No direct order path from AI.** Planning and recommendation surfaces can propose checks or strategy config changes, but order creation remains explicit, gated, and audited.

## Priority map

| Priority | Theme | Why it matters |
| --- | --- | --- |
| P0 | Safety chrome and system state | Prevents dangerous ambiguity as the UI grows. |
| P0 | Evidence stack everywhere | Makes the terminal trustworthy instead of decorative. |
| P0 | Strategy readiness gate | Prevents stale-data or missing-model strategy starts. |
| P1 | Portfolio optimizer upgrade | Turns the optimizer into a true institutional planning cockpit. |
| P1 | Model validation dossier | Makes model promotion/shadow decisions evidence-based. |
| P1 | Source health and coverage control center | Connects data availability to models, strategies, and research. |
| P1 | Route/proof receipt center | Converts local proof scripts into operator-visible confidence. |
| P2 | Production signal cockpit | Graduates the mock graph into a real evidence navigation layer. |
| P2 | News intelligence command center | Connects news, impact model, symbols, positions, and source quality. |
| P2 | Backtest and scenario lab | Makes research-to-paper comparisons credible. |
| P3 | UI system polish | Makes the whole app feel cohesive, fast, and premium. |

---

## P0 Improvements

### 1. Persistent Safety-State Command Bar

#### 1 current gap

The dashboard has risk pages, kill switch controls, service status ideas, and paper-first boundaries, but the safety state is not yet an always-visible, cross-page mental model. Operators should not have to infer whether they are in demo data, paper mode, degraded mode, read-only mode, or future live-gated mode.

#### 1 build plan

Add a persistent top-level safety strip in `AppShell` / `Topbar` showing:

- Execution mode: `READ ONLY`, `PAPER`, `SIM`, `LIVE DISABLED`, `LIVE GATED`.
- Kill switch state.
- API connectivity.
- Redis/Timescale/service heartbeat state.
- Market data freshness state.
- OpenBB/research provider readiness state.
- Last successful route-smoke receipt timestamp.
- Last paper-spine receipt timestamp.

#### 1 UI behavior

- Green/cyan only when local system is fresh and paper-safe.
- Amber for degraded providers, stale coverage, missing optional keys, or demo data.
- Red for kill switch active, service down, stale market data on active strategy, or route-smoke failure.
- Purple for AI/model-generated content, never for verified system truth.

#### 1 likely files

- `apps/dashboard/src/components/shell/app-shell.tsx`
- `apps/dashboard/src/components/shell/topbar.tsx`
- `apps/dashboard/src/components/shell/sidebar.tsx`
- `apps/dashboard/src/lib/api.ts`
- `apps/dashboard/src/lib/types.ts`
- `services/api/src/api/routes/services.py`
- `services/api/src/api/routes/control.py`
- `services/api/src/api/routes/data.py`
- `services/api/src/api/routes/research.py`

#### 1 acceptance criteria

- Every route shows the same safety state.
- Safety state distinguishes provider degradation from core service degradation.
- Kill switch state is visible on every page.
- The bar links to Risk, Reconciliation, Services, and receipt center.
- The state is derived from API data, not hardcoded UI assumptions.

### 2. Evidence Stack Pattern Across Research, Models, Predictions, News, and Optimizer

#### 2 current gap

The agent UI analysis describes progressive disclosure L1-L4, and some pages already expose evidence or diagnostics. But the pattern is not unified. Operators need one common way to inspect a claim.

#### 2 build plan

Create a reusable `EvidenceStack` component with four levels:

1. **Summary** — human-readable claim.
2. **Evidence** — source rows, timestamps, provider names, confidence, model labels.
3. **Payload** — normalized API payload or receipt excerpt.
4. **Trace** — IDs, stream names, route, latency, request hash, correlation ID.

#### 2 application surfaces

- Research OpenBB/Exa results.
- News impact predictions.
- Model prediction cards.
- Portfolio optimizer selected/rejected holdings.
- Strategy readiness checks.
- Risk alerts.
- Reconciliation issues.
- Signal cockpit nodes.

#### 2 likely files

- `apps/dashboard/src/components/evidence/`
- `apps/dashboard/src/app/research/page.tsx`
- `apps/dashboard/src/app/news-impact-lab/page.tsx`
- `apps/dashboard/src/app/predictions/page.tsx`
- `apps/dashboard/src/app/models/page.tsx`
- `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`
- `apps/dashboard/src/features/signal-cockpit-demo/`

#### 2 acceptance criteria

- A user can inspect any AI/model/research claim to raw payload or trace level.
- Missing evidence renders as `insufficient evidence`, not blank UI.
- Provider health and freshness are displayed with each claim.
- Evidence rows can be copied as JSON for debugging.

### 3. Strategy Readiness Gate Before Start/Enable

#### 3 current gap

Strategy configs, lifecycle controls, data coverage, services, risk state, model state, and reconciliation all exist separately. Starting a strategy should have a preflight gate that synthesizes these dependencies.

#### 3 build plan

Add a read-only readiness API and UI panel that evaluates:

- Strategy config exists and is enabled.
- Required symbols exist in universe.
- Required market data frequencies are fresh.
- Required features are fresh.
- Bound model exists and is active or approved shadow.
- Risk limits are valid.
- Kill switch is not active.
- OMS/paper broker health is acceptable.
- Open route-smoke or paper-spine receipt is recent enough.

#### 3 UI behavior

Before any start/enable action:

- Show blocking failures.
- Show warnings.
- Show override-eligible warnings separately.
- Require explicit confirmation for warning overrides.
- Record the readiness result into strategy history.

#### 3 likely files

- `services/api/src/api/routes/strategies.py`
- `services/api/src/api/routes/data.py`
- `services/api/src/api/routes/models.py`
- `services/api/src/api/routes/control.py`
- `libs/fincept-core/src/fincept_core/strategy_config.py`
- `services/strategy_host/`
- `apps/dashboard/src/app/strategies/page.tsx`
- `apps/dashboard/src/app/strategies/[id]/page.tsx`

#### 3 acceptance criteria

- Strategy start is blocked when data is stale, kill switch is active, or model binding is missing.
- Overrides are impossible for hard blockers.
- Overrides for warnings are audited.
- Strategy history stores the readiness snapshot used at the time of action.

---

## P1 Improvements

### 4. Portfolio Optimizer 2.0: Institutional Portfolio Construction Cockpit

#### 4 current gap

The optimizer already has strong primitives: candidate audit, constraints, frontier internals, Black-Litterman helpers, CVaR proxy, war-room stress, exportable packets, and AI reports. The biggest upgrade is to make those internals visible, interactive, and connected to live/local data readiness.

#### 4 build plan

Turn `/portfolio-builder` into a multi-panel optimizer cockpit:

1. **Universe Builder**
   - Search symbols from the Fincept universe API.
   - Pull source freshness and coverage for each candidate.
   - Show why names are eligible, rejected, or missing.
   - Separate stocks, ETFs, treasuries, cash, watchlist, excluded list.

2. **Optimization Method Switcher**
   - Heuristic baseline.
   - Inverse volatility.
   - Risk parity.
   - Mean-variance.
   - Black-Litterman with operator views.
   - CVaR/min-drawdown mode.
   - Show side-by-side weight differences and risk metrics.

3. **Constraint Studio**
   - Max holding.
   - Min holding.
   - Sector cap.
   - Cash reserve.
   - ETF/stock controls.
   - Max beta.
   - Max estimated drawdown.
   - Max single-name earnings risk.
   - Rebalance cadence.
   - Turn constraints into visible binding/non-binding pills.

4. **Efficient Frontier Explorer**
   - Plot expected return vs volatility.
   - Mark current portfolio.
   - Mark selected method.
   - Mark infeasible zones.
   - Let user hover to inspect candidate weights.

5. **Scenario War Room Upgrade**
   - Existing stress regimes become a first-class tab.
   - Compare multiple optimized portfolios under the same regime.
   - Show top loss contributors, cash drag, hedge candidates, and guardrail breaches.
   - Export stress receipt.

6. **AI Investment Committee Rail**
   - Keep AI constrained to fixed sections.
   - Require citation to selected holdings, rejected candidates, constraints, and stress results.
   - Add `What would change my mind?` section.
   - Add `Do not act if...` caveats.

7. **Promotion Path to Strategy Config**
   - Do not place orders.
   - Allow exporting an allocation as a draft strategy config or risk-limit proposal.
   - Require operator review and readiness gate.

#### 4 likely files

- `apps/dashboard/src/features/portfolio-builder/`
- `apps/dashboard/src/features/portfolio-builder/optimizer/`
- `apps/dashboard/src/features/portfolio-builder/war-room/`
- `apps/dashboard/src/app/portfolio-builder/page.tsx`
- `apps/dashboard/src/app/api/portfolio-report/route.ts`
- `services/api/src/api/routes/data.py`
- `services/api/src/api/routes/models.py`
- `services/api/src/api/routes/strategies.py`

#### 4 acceptance criteria

- User can compare at least three optimization methods on the same input.
- Frontier chart shows feasible and infeasible points.
- Every selected holding has a scored reason and rejection alternatives.
- Every AI paragraph maps back to deterministic optimizer evidence.
- No optimizer output can place orders directly.

### 5. Model Validation and Promotion Dossier

#### 5 current gap

Models can be trained, promoted, shadowed, displayed, and inspected. The next improvement is to make promotion decisions institutionally defensible.

#### 5 build plan

For each model artifact, generate and display a dossier:

- Training window.
- Feature list and feature importance.
- Walk-forward folds.
- Holdout metrics.
- Calibration buckets.
- Brier score / log loss when labels exist.
- Accuracy by symbol, horizon, regime, and volatility bucket.
- Shadow vs active comparison.
- Prediction freshness.
- Missing-label status.
- Known provider/data limitations.
- Promotion recommendation: `promote`, `keep shadow`, `deprecate`, `insufficient evidence`.

#### 5 UI behavior

- Promotions should show dossier status before confirmation.
- Shadow models should be visually distinct from active order-driving models.
- Missing dossier means promotion requires stronger confirmation or is blocked by policy.

#### 5 likely files

- `apps/dashboard/src/app/models/page.tsx`
- `apps/dashboard/src/app/models/[name]/page.tsx`
- `apps/dashboard/src/components/models/`
- `services/api/src/api/routes/models.py`
- `services/api/src/api/feature_importance.py`
- `services/api/src/api/training.py`
- `libs/fincept-core/src/fincept_core/prediction_log.py`
- `services/agents/`

#### 5 acceptance criteria

- Each model detail page has a validation tab.
- Promotion UI displays current dossier age and pass/fail state.
- Calibration is shown even when results are weak.
- Shadow predictions are never confused with order-driving signals.

### 6. Source Health and Data Coverage Control Center

#### 6 current gap

Markets, research, OpenBB health, datasource registry, coverage, and reconciliation all touch data readiness. The operator needs one control center for source freshness and provider reliability.

#### 6 build plan

Create a Data Control Center surface with:

- Provider registry cards: Timescale, Redis, Alpaca, OpenBB, Exa, NewsAPI, prediction logs.
- Health mode and safety tier.
- Last successful probe.
- Last error type.
- Coverage by symbol/frequency.
- Freshness timeline.
- Provider capability matrix.
- Dependency map: which pages/strategies/models rely on each source.

#### 6 likely files

- `apps/dashboard/src/app/markets/page.tsx`
- Possibly new `apps/dashboard/src/app/data/page.tsx`
- `services/api/src/api/routes/data.py`
- `services/api/src/api/routes/research.py`
- `apps/dashboard/src/lib/types.ts`
- `docs/datasources.md`

#### 6 acceptance criteria

- Coverage errors use stable public error codes.
- Freshness history is visible, not just point-in-time status.
- Strategy readiness can link to exact data gaps.
- OpenBB readiness details are rendered, including per-check provider failure.

### 7. Proof Receipt Center

#### 7 current gap

The repo now has proof scripts, but receipts live in local files and are not operator-visible. A terminal should show its own proof state.

#### 7 build plan

Add a Receipt Center page or Risk/Reconciliation tab showing:

- Latest paper-spine replay receipt.
- Latest route-smoke receipt.
- Latest OpenBB live proof receipt.
- Latest optimizer war-room stress receipt.
- Latest model dossier receipt.
- Latest strategy readiness receipt.

For each receipt:

- Status.
- Timestamp.
- Git hash/config hash if available.
- Pass/fail counts.
- Degraded/skipped checks.
- Link to JSON payload.

#### 7 likely files

- `scripts/paper_spine_replay.py`
- `scripts/route_smoke.py`
- `scripts/openbb_live_proof.py`
- New API route for receipt summaries.
- `apps/dashboard/src/app/reconciliation/page.tsx`
- `apps/dashboard/src/app/risk/page.tsx`
- Possible new `apps/dashboard/src/app/receipts/page.tsx`

#### 7 acceptance criteria

- Operators can see proof recency without opening the filesystem.
- A stale or failed proof changes global safety state to degraded.
- Receipts never expose secrets.
- Generated receipt directories stay ignored by Git.

---

## P2 Improvements

### 8. Production Signal Cockpit

#### 8 current gap

`/signal-cockpit-demo` is a compelling mock route but explicitly seeded and not wired to live services. The concept should become a real evidence navigation layer, not a separate fantasy UI.

#### 8 build plan

Evolve the cockpit in stages:

1. **Read-only live graph**
   - Symbol nodes.
   - Provider/source nodes.
   - Model nodes.
   - News nodes.
   - Risk caveat nodes.
   - Strategy exposure nodes.

2. **Evidence drawer**
   - Use the shared `EvidenceStack`.
   - Show source freshness, payload, trace IDs.

3. **Operator rail**
   - Structured fixed sections.
   - No free-form chat.
   - Suggested checks only.

4. **Cross-page linking**
   - Node clicks navigate to predictions, research, models, risk, strategies, or markets.

#### 8 likely files

- `apps/dashboard/src/features/signal-cockpit-demo/`
- `apps/dashboard/src/app/signal-cockpit-demo/page.tsx`
- `apps/dashboard/src/app/predictions/page.tsx`
- `apps/dashboard/src/app/research/page.tsx`
- `apps/dashboard/src/app/risk/page.tsx`
- API read-model aggregation route if needed.

#### 8 acceptance criteria

- Demo route remains clearly labeled until live data is wired.
- Live cockpit never implies execution authority.
- Every node has evidence and freshness.
- Missing data creates caveat nodes, not hidden gaps.

### 9. News Intelligence Command Center

#### 9 current gap

News, news lab, and news impact lab exist, but the user experience can become a command center that connects headlines to symbols, positions, model views, source quality, and portfolio exposure.

#### 9 build plan

Unify news surfaces around:

- Live headlines by source and symbol.
- Source reliability and freshness.
- Impact model prediction by horizon.
- Affected open positions.
- Related model predictions.
- Suggested checks.
- Historical analogs.
- Outcome labeling state.
- Promotion/readiness status for news-alpha model.

#### 9 likely files

- `apps/dashboard/src/app/news/page.tsx`
- `apps/dashboard/src/app/news-lab/page.tsx`
- `apps/dashboard/src/app/news-impact-lab/page.tsx`
- `services/api/src/api/routes/news.py`
- `services/api/src/api/routes/news_impact.py`
- `services/agents/src/agents/news_alpha_predictor/`
- `services/agents/src/agents/news_outcome_labeler/`
- `services/jobs/src/jobs/news_alpha_candidate_train.py`

#### 9 acceptance criteria

- Every news-impact prediction shows source, model version, horizon, confidence, and caveat.
- If labels are missing, the UI says so.
- News-alpha cannot be presented as executable without promotion evidence.
- Operators can inspect why the model thinks an article matters.

### 10. Backtest and Scenario Lab

#### 10 current gap

Backtest route and backtester service exist. Portfolio war-room stress exists. The platform needs one research lab for comparing strategy behavior across historical, synthetic, and paper scenarios.

#### 10 build plan

Add:

- Backtest run browser.
- Strategy selector.
- Cost/slippage/latency scenario controls.
- Risk-gate simulation toggle.
- Paper-spine comparison lane.
- Regime stress presets.
- Attribution by symbol, feature family, model, rejected notional, fees, slippage, and turnover.
- Exportable run receipt.

#### 10 likely files

- `apps/dashboard/src/app/backtest/page.tsx`
- `services/api/src/api/routes/backtest.py`
- `services/backtester/`
- `services/risk/`
- `reports/backtests/`

#### 10 acceptance criteria

- Backtest reports show net metrics after fees/slippage.
- Risk rejected trades are visible, not just omitted.
- Backtest assumptions are exportable.
- Backtest and paper replay can be compared by common IDs where available.

### 11. Reconciliation as Daily Operator Workflow

#### 11 current gap

Reconciliation already compares positions, strategy configs, runtime rows, universe, coverage, and orders. It should become the daily operator checklist.

#### 11 build plan

Add a daily reconciliation checklist:

- Positions vs strategy config.
- Orders vs fills.
- Positions vs internal ledger.
- Universe coverage.
- Data freshness.
- Service heartbeat.
- Open rejections.
- Pending orders.
- Missing strategy runtime.
- Suggested repair action.
- One-click receipt export.

#### 11 likely files

- `apps/dashboard/src/app/reconciliation/page.tsx`
- `services/api/src/api/routes/positions.py`
- `services/api/src/api/routes/orders.py`
- `services/api/src/api/routes/strategies.py`
- `services/api/src/api/routes/data.py`

#### 11 acceptance criteria

- Each issue has severity and owner.
- Each repair action has confirmation and audit.
- Daily reconciliation can be exported as JSON.
- Global safety bar reflects unresolved critical reconciliation issues.

---

## P3 Improvements

### 12. Universal Command Palette 2.0

#### 12 current gap

The dashboard has navigation mnemonics. It can become a real operator command layer without becoming unsafe.

#### 12 build plan

Add command palette actions:

- Go to route.
- Search symbol.
- Search strategy.
- Search model.
- Run read-only checks.
- Open latest receipts.
- Open provider health.
- Start route smoke locally via documented command copy.
- Trigger safe refreshes.

Dangerous actions should not execute directly from the palette. They should navigate to the relevant page with context.

#### 12 likely files

- `apps/dashboard/src/components/shell/`
- `apps/dashboard/src/lib/api.ts`
- Route pages with search params.

#### 12 acceptance criteria

- Command palette can find any major entity.
- It never directly trips kill switch, starts strategies, promotes models, or places orders.
- Dangerous commands route to confirmation surfaces.

### 13. Visual Design System Hardening

#### 13 current gap

The app has good ingredients but needs a stricter semantic grammar to feel elite and consistent.

#### 13 build plan

Create a UI token contract:

- Cyan = verified/system truth.
- Amber = degraded/experimental/caveat.
- Red = critical/blocking/risk.
- Purple = AI/model-generated.
- Green = profitable/healthy/long.
- Gray = inactive/stale/unknown.

Add reusable components:

- `StatusPill`
- `FreshnessBadge`
- `ProviderHealthBadge`
- `SafetyStateBanner`
- `EvidenceStack`
- `ReceiptCard`
- `ReadinessGatePanel`
- `RiskActionConfirmDialog`
- `MetricDelta`
- `EntityHeader`

#### 13 likely files

- `apps/dashboard/src/app/globals.css`
- `apps/dashboard/src/components/ui/`
- `apps/dashboard/src/components/widgets/`
- `docs/agent-ui-analysis/02-data-shapes-and-tokens.md`
- `docs/uirecommendations.md`

#### 13 acceptance criteria

- Page-specific one-off badges are reduced.
- Colors mean the same thing everywhere.
- AI output is visually distinct from verified system state.
- Accessibility contrast is checked for key states.

### 14. Empty, Loading, Error, and Degraded State Framework

#### 14 current gap

As the app grows, page-level error/loading states can drift. A serious terminal should have consistent degraded-state UX.

#### 14 build plan

Add a shared state framework:

- Empty state.
- Loading skeleton.
- Auth-required state.
- Provider-unavailable state.
- Stale-data state.
- Partial-data state.
- Fatal API state.
- Demo-data state.

#### 14 likely files

- `apps/dashboard/src/components/states/`
- `apps/dashboard/src/lib/api.ts`
- All route pages over time.

#### 14 acceptance criteria

- External provider failures are never shown as generic crashes.
- Demo data is always labeled.
- Stale data includes last timestamp and likely remediation.
- Pages with partial data still render usable summaries.

### 15. Local Dev and Operator Launch Experience

#### 15 current gap

There are scripts for start/status/stop, feature start/stop, smoke proofs, and preflight. The UI could become the guide for local readiness.

#### 15 build plan

Add a local launch/readiness screen:

- API status.
- Redis status.
- Timescale status.
- Dashboard API URL.
- Service heartbeat table.
- Required env vars present/missing by name only.
- Copyable commands for start/status/stop/preflight/route-smoke/OpenBB proof.
- Last proof receipt status.

#### 15 likely files

- `scripts/start.ps1`
- `scripts/status.ps1`
- `scripts/stop.ps1`
- `scripts/route_smoke.py`
- `services/api/src/api/routes/services.py`
- `apps/dashboard/src/app/risk/page.tsx`
- Possible new `apps/dashboard/src/app/system/page.tsx`

#### 15 acceptance criteria

- New users can see exactly what is running and what is missing.
- No secrets are displayed.
- Copyable commands use Windows PowerShell defaults.
- Route smoke and proof status are visible.

---

## Page-by-page quick wins

### Overview

- Add global safety state summary.
- Add last proof receipt cards.
- Add top unresolved reconciliation issues.
- Add service heartbeat strip.
- Add open strategy readiness summary.

### Positions

- Add freshness badges for marks.
- Add source of mark price.
- Add linked orders/fills drawer.
- Add exposure by strategy and sector.
- Add stale mark warning.

### Orders

- Add order lifecycle timeline.
- Add risk-check result link.
- Add source decision ID.
- Add fill/position impact drawer.
- Add rejected-order reason analytics.

### Strategies

- Add readiness gate.
- Add config history viewer.
- Add strategy autopsy report.
- Add dependency graph: symbols, models, data, risk limits.
- Add start/stop audit receipt.

### Portfolio Builder

- Add method switcher.
- Add frontier explorer.
- Add live universe bridge.
- Add Black-Litterman view editor.
- Add portfolio-to-strategy draft export.
- Add constraint studio.
- Add AI report evidence citations.

### News and News Impact Lab

- Merge headline, impact, positions, and prediction context.
- Add outcome label status.
- Add historical analogs.
- Add source reliability/freshness.
- Add news-alpha promotion dossier.

### Predictions

- Add calibration buckets.
- Add forward-return label status.
- Add active vs shadow distinction.
- Add prediction-to-decision trace.
- Add regime-conditioned performance.

### Markets

- Add coverage timeline.
- Add provider matrix.
- Add route to source health.
- Add symbol freshness heatmap.
- Add data-quality incident panel.

### Models

- Add validation dossier.
- Add promotion gate.
- Add feature importance drift.
- Add shadow-vs-active comparison.
- Add stale artifact warning.

### Risk

- Add persistent safety-state source of truth.
- Add unresolved reconciliation issues.
- Add kill-switch drill mode.
- Add risk-limit usage over time.
- Add strategy readiness blockers.

### Research

- Render OpenBB readiness per-check results.
- Add curated OpenBB preset marketplace.
- Add research request ledger.
- Add evidence stack for every external claim.
- Add cost/rate-limit visibility.

### Reconciliation

- Promote to daily checklist.
- Add issue severity and owner.
- Add repair receipts.
- Add unresolved issue count to global safety bar.

### Signal Cockpit

- Keep demo label until wired.
- Gradually replace seeded nodes with live read models.
- Use EvidenceStack drawer.
- Add source freshness and safety caveats to every node.

---

## Recommended implementation order

### Sprint 1: Safety and proof visibility

1. Safety-state command bar.
2. Receipt Center.
3. Shared degraded-state components.
4. OpenBB readiness rendering in Research.

### Sprint 2: Strategy and data readiness

1. Strategy readiness API.
2. Strategy readiness panel.
3. Data Coverage Control Center.
4. Reconciliation daily checklist.

### Sprint 3: Portfolio optimizer leap

1. Optimization method switcher.
2. Efficient frontier explorer.
3. Constraint studio.
4. AI report citations.
5. Portfolio-to-strategy draft export.

### Sprint 4: Model and news intelligence

1. Model validation dossier.
2. Calibration board.
3. News impact evidence drawer.
4. News-alpha promotion dossier.

### Sprint 5: Signal cockpit productionization

1. Shared graph data contract.
2. Live symbol/source/model/risk nodes.
3. Evidence drawer.
4. Operator rail.

---

## Explicit non-goals

Do not prioritize these before the above work:

- Live capital order flow.
- More autonomous AI actions.
- More decorative graph UI without evidence wiring.
- New external providers without health/readiness contracts.
- New model types without validation dossiers.
- New strategy launch controls without readiness gates.
- Huge dashboard rewrites that do not improve safety, evidence, or proof.

## Final recommendation

The biggest unlock is not one flashy feature. It is a product pattern:

```text
Safety State + Evidence Stack + Readiness Gates + Receipts
```

Once those four patterns are shared across the app, Fincept becomes much more than a collection of pages. It becomes an operator-grade AI trading cockpit where every recommendation, model, provider, strategy, and allocation is inspectable, caveated, and provable.
