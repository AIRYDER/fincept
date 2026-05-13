# Portfolio Optimizer Tracks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the current AI portfolio builder into a three-track roadmap that can be implemented by GPT-5.4 low/medium without re-discovering architecture.

**Architecture:** Keep `/portfolio-builder` as the operator UI, but move real optimization math and persistence behind explicit modules and API routes. Preserve the current product boundary: analysis first, paper-only handoff later, no live order placement.

**Tech Stack:** Next.js dashboard, TypeScript portfolio-builder feature modules, FastAPI service routes, Redis-backed portfolio state, existing Python `uv` workspace, existing `pnpm --filter @fincept/dashboard test:portfolio-builder` test harness.

---

## Current Baseline

The existing builder is a deterministic allocation packet generator:

- UI entry: `apps/dashboard/src/app/portfolio-builder/page.tsx`
- Client page: `apps/dashboard/src/features/portfolio-builder/PortfolioBuilderPage.tsx`
- Form: `apps/dashboard/src/features/portfolio-builder/PortfolioBuilderForm.tsx`
- Current heuristic allocator: `apps/dashboard/src/features/portfolio-builder/portfolioOptimizer.ts`
- Demo data source: `apps/dashboard/src/features/portfolio-builder/marketDataService.ts`
- AI report route: `apps/dashboard/src/app/api/portfolio-report/route.ts`
- Tests: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`
- Docs: `docs/portfoliooptimizer.md`

Core boundary to preserve:

- The optimizer proposes allocations only.
- It must not place live trades.
- Any trading effect must go through explicit review, paper-only target creation, risk controls, and existing strategy/OMS/portfolio workflows.

Implementation order:

1. Institutional optimizer core.
2. Operator decision workbench.
3. Execution-grade portfolio operating system.

---

## Track 1: Institutional Optimizer Core

**Outcome:** Replace heuristic-only scoring with a real, testable optimizer engine that supports expected returns, covariance, efficient frontier, Black-Litterman, risk parity, CVaR/min-drawdown, constraints, and live-or-demo market inputs.

**Why first:** The current surface can explain a portfolio, but the math is still heuristic. This track gives every later feature a stronger foundation.

**Initial MVP Slice:** Add a TypeScript engine that can build return/risk inputs from normalized candidate data and solve four deterministic methods: heuristic baseline, inverse-volatility, risk-parity approximation, and mean-variance grid search. Keep CVaR, Black-Litterman, and live quote wiring as second-slice modules that return explicit unavailable diagnostics until their inputs are present.

### File Map

- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/optimizerTypes.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/returnInputs.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/covariance.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/constraintSolver.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/efficientFrontier.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/riskParity.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/blackLitterman.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/cvar.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/index.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.types.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/portfolioOptimizer.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`
- Modify: `docs/portfoliooptimizer.md`

### Data Contracts

Add these TypeScript concepts:

- `OptimizationMethod`: `"heuristic" | "inverse_volatility" | "risk_parity" | "mean_variance" | "black_litterman" | "cvar_min_drawdown"`
- `ReturnInput`: ticker, expected annual return, annual volatility, beta, confidence, source label, warnings.
- `CovarianceMatrix`: ticker order plus square matrix of annualized covariances.
- `OptimizationConstraintSet`: max holding, min holding, max sector, cash reserve, included/excluded tickers, long-only flag.
- `OptimizationRunDiagnostics`: method, iterations, feasible flag, binding constraints, warnings, objective score.
- `OptimizationCandidatePortfolio`: weights, expected return, volatility, Sharpe-like score, max drawdown proxy, CVaR proxy, diagnostics.
- `EfficientFrontierPoint`: target return, expected return, volatility, weights, feasible flag.

### Task 1.1: Optimizer Types And Input Normalization

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/optimizerTypes.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/returnInputs.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.types.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Add optimizer type exports with no external dependencies.
- [ ] Add `buildReturnInputs(candidates, riskLevel, horizon)` that converts current demo candidates into annualized expected-return and volatility estimates.
- [ ] Make every value finite and clamp confidence to `0..1`.
- [ ] Add tests for missing prices, zero volatility, speculative risk levels, and stable ticker ordering.
- [ ] Run `pnpm --filter @fincept/dashboard test:portfolio-builder`.

Acceptance criteria:

- Return inputs are deterministic for the same candidate list.
- Every generated number is finite.
- Missing-price candidates are excluded with a warning.
- The existing tests still pass.

### Task 1.2: Covariance And Constraint Solver

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/covariance.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/constraintSolver.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Add `buildDiagonalCovariance(inputs)` as the MVP covariance model.
- [ ] Add `applyLongOnlyConstraints(weights, inputs, constraints)` that normalizes weights, enforces max holding, min holding, max sector, and cash-adjusted investable total.
- [ ] Add `redistributeResidualWeight(...)` with capped passes and explicit infeasible diagnostics.
- [ ] Add tests for over-constrained portfolios, sector caps, min allocation dropping, and all-zero proposed weights.
- [ ] Run `pnpm --filter @fincept/dashboard test:portfolio-builder`.

Acceptance criteria:

- Output weights never exceed `100 - cashReservePct`.
- Sector caps are respected within rounding tolerance.
- Infeasible constraints return warnings instead of NaN or silent bad math.

### Task 1.3: Initial Real Optimizers

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/riskParity.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/efficientFrontier.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/index.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/portfolioOptimizer.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Implement `optimizeInverseVolatility(inputs, constraints)`.
- [ ] Implement `optimizeRiskParity(inputs, covariance, constraints)` using an iterative inverse risk-contribution approximation.
- [ ] Implement `buildEfficientFrontier(inputs, covariance, constraints)` using deterministic grid search over target-return bands.
- [ ] Add `optimization` diagnostics to `PortfolioAllocationResult`.
- [ ] Keep the current heuristic path as `OptimizationMethod = "heuristic"` for backward-compatible output.
- [ ] Add tests proving each method returns feasible finite weights and includes diagnostics.
- [ ] Run `pnpm --filter @fincept/dashboard test:portfolio-builder`.

Acceptance criteria:

- Existing form output remains usable.
- The allocation result names the method used.
- Frontier points are sorted by increasing expected return.
- Inverse-volatility and risk-parity produce different outputs on mixed-volatility candidates.

### Task 1.4: Black-Litterman And CVaR Scaffolds

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/blackLitterman.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/optimizer/cvar.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Implement a conservative Black-Litterman MVP using market-cap proxy weights from candidate liquidity and risk scores.
- [ ] Support operator views as `{ ticker, expectedReturnDeltaPct, confidence }`.
- [ ] Implement CVaR/min-drawdown proxy scoring from supplied or synthetic return scenarios.
- [ ] Add clear warnings when scenario data is synthetic or insufficient.
- [ ] Add tests for view confidence, invalid tickers, scenario scarcity, and deterministic output.
- [ ] Run `pnpm --filter @fincept/dashboard test:portfolio-builder`.

Acceptance criteria:

- Black-Litterman never invents tickers outside the selected universe.
- Low-confidence views barely move posterior returns.
- CVaR mode degrades gracefully when scenario history is too small.

### Task 1.5: Live Data Adapter Boundary

**Files:**

- Modify: `apps/dashboard/src/features/portfolio-builder/marketDataService.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/liveMarketDataClient.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Split demo candidates from the snapshot provider.
- [ ] Add a `MarketDataProvider` interface with `getSnapshot(): Promise<MarketDataSnapshot>`.
- [ ] Keep demo provider as the default.
- [ ] Add a live provider client that can later call existing `/data` and symbol/search APIs, but returns a clear unavailable result until the API contract is wired.
- [ ] Add tests proving demo mode remains default and live-unavailable mode carries warnings.

Acceptance criteria:

- The UI still works offline.
- Demo mode remains explicitly labeled.
- Live adapter failure never blocks deterministic demo allocation.

### GPT-5.4 Implementation Prompt

```text
Implement Track 1 from docs/superpowers/plans/2026-05-08-portfolio-optimizer-tracks.md only.

Rules:
- Work in C:\Users\nolan\CascadeProjects\fincept-terminal.
- Do not touch execution, OMS, strategy host, or paper trading files.
- Preserve /portfolio-builder as analysis-only.
- Use TypeScript modules under apps/dashboard/src/features/portfolio-builder/optimizer/.
- Keep all numeric outputs finite and deterministic.
- Start with tests in apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts.
- Run pnpm --filter @fincept/dashboard test:portfolio-builder after each task.
- Update docs/portfoliooptimizer.md with the methods that actually landed.
```

---

## Track 2: Operator Decision Workbench

**Outcome:** Turn the optimizer from a single report generator into a decision surface where an operator can compare scenarios, inspect frontier tradeoffs, run stress/regime checks, compare benchmarks, save candidates, and see why a portfolio changed.

**Why second:** Once the math produces multiple valid candidate portfolios, the operator needs a workbench to choose between them.

**Initial MVP Slice:** Add scenario comparison entirely in the dashboard with local persistence. Do not introduce server persistence until the scenario model is stable.

### File Map

- Create: `apps/dashboard/src/features/portfolio-builder/workbench/workbenchTypes.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/workbench/scenarioStore.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/workbench/scenarioDiff.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/workbench/stressScenarios.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/workbench/benchmarkCompare.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/workbench/approvalState.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/ScenarioComparisonPanel.tsx`
- Create: `apps/dashboard/src/features/portfolio-builder/FrontierExplorer.tsx`
- Create: `apps/dashboard/src/features/portfolio-builder/StressTestPanel.tsx`
- Create: `apps/dashboard/src/features/portfolio-builder/ApprovalRail.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioBuilderPage.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.types.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`
- Modify: `docs/portfoliooptimizer.md`

### Data Contracts

Add these TypeScript concepts:

- `PortfolioScenario`: id, label, createdAt, input hash, allocation result, report status, approval state.
- `ScenarioDiff`: added tickers, removed tickers, weight changes, risk metric changes, constraint changes, top explanation lines.
- `StressScenario`: id, label, return shock by sector/asset type/ticker, volatility multiplier, rate shock label.
- `StressResult`: scenario id, estimated portfolio impact, worst contributors, breached risk rules, warnings.
- `BenchmarkComparison`: benchmark ticker, active weights, tracking-error proxy, overlap percentage, risk delta.
- `ApprovalState`: `"draft" | "reviewed" | "approved_for_paper" | "rejected"`

### Task 2.1: Scenario Store And Diff Engine

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/workbench/workbenchTypes.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/workbench/scenarioStore.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/workbench/scenarioDiff.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Add `createPortfolioScenario(allocation, label)` with a stable id derived from timestamp plus input summary.
- [ ] Add localStorage-backed `loadScenarios`, `saveScenario`, and `deleteScenario`.
- [ ] Add `diffScenarios(base, candidate)` with explicit weight, risk, ticker, sector, and constraint changes.
- [ ] Add tests for scenario round-trip, corrupt localStorage payload handling, and deterministic diff output.
- [ ] Run `pnpm --filter @fincept/dashboard test:portfolio-builder`.

Acceptance criteria:

- Corrupt saved data is ignored with a warning.
- Diff output is stable and easy to render.
- Scenario persistence is browser-local only in the MVP.

### Task 2.2: Scenario Comparison UI

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/ScenarioComparisonPanel.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioBuilderPage.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`

- [ ] Add a save scenario action after allocation generation.
- [ ] Add a scenario drawer/panel that lists saved scenarios with method, risk, horizon, total value, and approval state.
- [ ] Render scenario diff against the currently selected allocation.
- [ ] Add empty, loading, and corrupt-store states.
- [ ] Keep visual style aligned with the existing industrial dashboard.

Acceptance criteria:

- Operator can save, select, compare, and delete scenarios without a server.
- No card-inside-card layout.
- The workbench is useful on desktop and not broken on mobile.

### Task 2.3: Frontier Explorer

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/FrontierExplorer.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Render efficient-frontier points from Track 1 diagnostics when present.
- [ ] Add a compact table fallback if charting is not available.
- [ ] Highlight the selected portfolio and the lowest-volatility feasible portfolio.
- [ ] Add tests for frontier sorting and selected-point lookup.

Acceptance criteria:

- Frontier explorer renders from deterministic data only.
- Missing frontier data shows a concise unavailable state.
- No AI text is required to understand the tradeoff.

### Task 2.4: Stress And Benchmark Panels

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/workbench/stressScenarios.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/workbench/benchmarkCompare.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/StressTestPanel.tsx`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Add built-in stress scenarios: equity drawdown, rates up, AI multiple compression, energy shock, liquidity squeeze.
- [ ] Compute estimated impact from holdings, sector, beta, volatility, and asset type.
- [ ] Add benchmark comparison against SPY, QQQ, VTI, and SGOV proxies from demo candidates.
- [ ] Add tests for stress impact signs, benchmark overlap, and missing benchmark data.
- [ ] Run `pnpm --filter @fincept/dashboard test:portfolio-builder`.

Acceptance criteria:

- Stress results are labeled estimates, not predictions.
- Benchmark compare works in demo mode.
- Missing benchmark inputs produce warnings instead of empty charts.

### Task 2.5: Approval State And Change Explanation

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/workbench/approvalState.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/ApprovalRail.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Add approval transitions: draft to reviewed, reviewed to approved_for_paper, any state to rejected.
- [ ] Require at least one saved scenario before approval.
- [ ] Generate deterministic "why this changed" lines from `ScenarioDiff`, not from LLM output.
- [ ] Add tests for allowed transitions and blocked approval without saved scenario context.

Acceptance criteria:

- Approval state has no trading side effects.
- "Why this changed" names the top drivers by weight and risk delta.
- Approved scenario can be exported for Track 3 later.

### GPT-5.4 Implementation Prompt

```text
Implement Track 2 from docs/superpowers/plans/2026-05-08-portfolio-optimizer-tracks.md only.

Rules:
- Work in C:\Users\nolan\CascadeProjects\fincept-terminal.
- Do not add server persistence in the MVP.
- Do not wire OMS, strategy host, or paper execution.
- Build local scenario compare, frontier explorer, stress testing, benchmark compare, approval state, and deterministic "why this changed" diagnostics.
- Follow existing dashboard visual language.
- Add tests to apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts.
- Run pnpm --filter @fincept/dashboard test:portfolio-builder after each task.
- Update docs/portfoliooptimizer.md with exactly what landed.
```

---

## Track 3: Execution-Grade Portfolio Operating System

**Outcome:** Turn approved allocations into paper-only target portfolios, rebalance plans, drift monitoring, and guarded handoff into existing strategy/risk config workflows.

**Why third:** This track depends on stronger optimizer outputs and explicit operator approval. It should be boring, auditable, and hard to misuse.

**Initial MVP Slice:** Create paper-only target portfolio records and drift calculations. Do not place orders. Do not auto-start strategies.

### File Map

- Create: `libs/fincept-core/src/fincept_core/target_portfolio.py`
- Create: `libs/fincept-core/tests/test_target_portfolio.py`
- Create: `services/api/src/api/routes/target_portfolios.py`
- Create: `services/api/tests/test_target_portfolios.py`
- Create: `apps/dashboard/src/features/portfolio-builder/paperHandoff/paperHandoffTypes.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/paperHandoff/targetPortfolioClient.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/paperHandoff/driftMonitor.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/PaperHandoffPanel.tsx`
- Modify: `services/api/src/api/main.py`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`
- Modify: `docs/portfoliooptimizer.md`
- Modify: `docs/SYSTEM_OVERVIEW.md`

### Data Contracts

Add these Python/TypeScript concepts:

- `TargetPortfolio`: id, source scenario id, created at, status, paper only flag, target weights, cash reserve, constraints, approval metadata.
- `TargetPortfolioHolding`: ticker, target weight pct, target dollars, target shares, tolerance pct, asset type.
- `RebalancePlan`: target portfolio id, generated at, current positions source, drift rows, suggested paper actions, blocked live execution flag.
- `DriftRow`: ticker, target weight, current weight, absolute drift, dollar drift, status.
- `PaperHandoffGuard`: approved scenario required, paper mode required, no live broker action, risk acknowledgement required.

### Task 3.1: Core Target Portfolio Schema

**Files:**

- Create: `libs/fincept-core/src/fincept_core/target_portfolio.py`
- Create: `libs/fincept-core/tests/test_target_portfolio.py`

- [ ] Add dataclasses or Pydantic models for `TargetPortfolio`, `TargetPortfolioHolding`, `RebalancePlan`, and `DriftRow`.
- [ ] Add `validate_target_portfolio(target)` that requires paper-only mode and weights summing to `100 +/- 0.25`.
- [ ] Add `calculate_drift(target, current_positions, nav)` with finite numeric output.
- [ ] Add tests for weight validation, paper-only guard, missing current positions, and drift sign.
- [ ] Run `uv run pytest libs/fincept-core/tests/test_target_portfolio.py -q`.

Acceptance criteria:

- Live execution cannot be represented by the MVP schema.
- Drift math is deterministic and finite.
- The core package can be tested without Redis, FastAPI, or dashboard dependencies.

### Task 3.2: API Route For Paper Targets

**Files:**

- Create: `services/api/src/api/routes/target_portfolios.py`
- Create: `services/api/tests/test_target_portfolios.py`
- Modify: `services/api/src/api/main.py`

- [ ] Add authenticated `POST /target-portfolios` that accepts an approved scenario export and stores a Redis-backed paper target using the app's existing Redis dependency pattern.
- [ ] Add `GET /target-portfolios` for listing paper targets.
- [ ] Add `GET /target-portfolios/{id}/drift` that reads current positions through the existing portfolio store path and returns a rebalance plan.
- [ ] Return `400` for unapproved scenarios and `403` for any non-paper handoff request.
- [ ] Add async API tests using the existing `httpx.AsyncClient` and ASGI transport pattern.
- [ ] Run `uv run pytest services/api/tests/test_target_portfolios.py -q`.

Acceptance criteria:

- Auth behavior follows existing API route conventions.
- Tests do not need a live Redis service.
- Non-paper requests fail closed.

### Task 3.3: Dashboard Paper Handoff Panel

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/paperHandoff/paperHandoffTypes.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/paperHandoff/targetPortfolioClient.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/PaperHandoffPanel.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`

- [ ] Add a panel visible only when a scenario is approved for paper.
- [ ] Show target weights, tolerances, paper-only warning, and handoff guard status.
- [ ] Add a create-paper-target action that calls `/target-portfolios`.
- [ ] Show API errors without hiding guard failures.
- [ ] Keep live execution copy out of the UI.

Acceptance criteria:

- The panel cannot be used from a draft scenario.
- Every action says paper-only.
- API failure is visible and actionable.

### Task 3.4: Drift Monitor And Rebalance Plan

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/paperHandoff/driftMonitor.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/PaperHandoffPanel.tsx`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Add `summarizeDrift(plan)` for largest drift, total absolute drift, breached tolerance count, and status.
- [ ] Render current versus target weights and suggested paper actions.
- [ ] Add tests for drift summary and tolerance breach labeling.
- [ ] Run `pnpm --filter @fincept/dashboard test:portfolio-builder`.

Acceptance criteria:

- Rebalance plan is framed as suggested paper actions.
- Drift calculations tolerate missing current-position rows.
- No order intent is created in this track.

### Task 3.5: Strategy/Risk Config Handoff Draft

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/paperHandoff/strategyConfigDraft.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/PaperHandoffPanel.tsx`
- Modify: `docs/SYSTEM_OVERVIEW.md`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] Add a deterministic export that maps target portfolio constraints into a draft strategy/risk config payload.
- [ ] Label the payload as a draft only.
- [ ] Include target weights, sector caps, max holding cap, cash reserve, rebalance cadence, and approval metadata.
- [ ] Add tests proving the draft contains no start command, no order command, and no broker venue.
- [ ] Document the handoff boundary in `docs/SYSTEM_OVERVIEW.md`.

Acceptance criteria:

- Draft config is exportable.
- No strategy lifecycle route is called.
- No OMS route is called.
- The handoff remains review-first.

### GPT-5.4 Implementation Prompt

```text
Implement Track 3 from docs/superpowers/plans/2026-05-08-portfolio-optimizer-tracks.md only.

Rules:
- Work in C:\Users\nolan\CascadeProjects\fincept-terminal.
- Build paper-only target portfolio support.
- Do not place orders.
- Do not create OrderIntent, Fill, broker, Alpaca, or OMS calls.
- Fail closed if a scenario is not approved or if a request implies live execution.
- Start with core tests in libs/fincept-core/tests/test_target_portfolio.py.
- Then add API tests in services/api/tests/test_target_portfolios.py.
- Then add dashboard client/UI tests in apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts.
- Update docs/portfoliooptimizer.md and docs/SYSTEM_OVERVIEW.md with the exact paper-only boundary.
```

---

## Cross-Track Safety Rules

- Never execute live orders from the portfolio builder.
- Never call OMS order routes from Track 1 or Track 2.
- Track 3 may create paper target portfolios, but it still must not place orders.
- Treat all imported market data as untrusted: validate type, length, finite numbers, ticker charset, and timestamps before using it.
- Never print API keys or env values in diagnostics.
- If live data fails, fall back to explicitly labeled demo or unavailable states.
- Any API route that mutates saved targets must authenticate first and fail closed on auth errors.
- Every exported scenario should include enough metadata to reconstruct method, constraints, data mode, and warnings.

## Suggested Commit Split

- `feat(portfolio): add optimizer input and constraint engine`
- `feat(portfolio): add frontier and risk optimizer methods`
- `feat(portfolio): add scenario workbench`
- `feat(portfolio): add stress and benchmark panels`
- `feat(portfolio): add paper target portfolio schema`
- `feat(api): add paper target portfolio routes`
- `feat(dashboard): add paper handoff panel`

## Verification Matrix

Run these as each track lands:

- Track 1 dashboard math: `pnpm --filter @fincept/dashboard test:portfolio-builder`
- Track 2 dashboard workbench: `pnpm --filter @fincept/dashboard test:portfolio-builder`
- Track 3 core schema: `uv run pytest libs/fincept-core/tests/test_target_portfolio.py -q`
- Track 3 API: `uv run pytest services/api/tests/test_target_portfolios.py -q`
- Broader route safety when API routes change: `uv run pytest services/api/tests -q`
- Broader dashboard safety when UI changes: `pnpm --filter @fincept/dashboard typecheck`

## Self-Review Notes

- The three tracks are intentionally independent enough to hand to separate implementation sessions.
- Track 1 can land without Track 2 or Track 3.
- Track 2 can use Track 1 diagnostics when available and still render fallback states without them.
- Track 3 should wait until Track 2 has approval state and scenario export metadata.
- The first implementation pass should prefer deterministic algorithms over external numerical solver dependencies.
- A dedicated solver dependency can be considered after the deterministic MVP proves the contracts and tests.
