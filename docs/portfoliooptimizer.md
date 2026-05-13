# Portfolio Optimizer

The dashboard includes an Optimizer route at `/portfolio-builder`. This is the operator-facing portfolio construction surface, separate from the live trading loop.

## Purpose

The optimizer helps explore portfolio weights, risk budgets, diversification, and allocation scenarios before those ideas become strategy configs or risk limits.

## Current product boundary

- It is an analysis and planning tool.
- It should not place orders directly.
- Any output that affects trading must flow through explicit operator review, strategy config changes, risk limits, and the paper-trading loop.

## Expected inputs

- Universe symbols from the Fincept universe/data API.
- Historical bars or returns from local bars storage.
- Optional model views from prediction/model endpoints.
- Operator constraints such as gross cap, max symbol weight, cash reserve, and risk budget.

## Expected outputs

- Candidate weights.
- Concentration and exposure summaries.
- Risk/return diagnostics.
- Exportable scenario notes for review.

## Implemented optimizer core

The first institutional optimizer slice now lives under
`apps/dashboard/src/features/portfolio-builder/optimizer/`.

- `returnInputs.ts` converts candidate rows into deterministic expected-return,
  volatility, beta, confidence, and source diagnostics.
- `covariance.ts` builds a finite annualized covariance matrix from the current
  candidate universe.
- `constraintSolver.ts` enforces long-only cash, holding, minimum-allocation,
  and sector caps without returning NaN or Infinity.
- `riskParity.ts` implements inverse-volatility and risk-parity-style
  portfolio candidates.
- `efficientFrontier.ts` builds deterministic mean-variance frontier points.
- `blackLitterman.ts` blends operator return views by confidence and rejects
  unknown tickers with warnings.
- `cvar.ts` exposes a labeled CVaR/drawdown proxy until real historical return
  scenarios are connected.

The default UI path still uses the existing deterministic heuristic allocator,
but `PortfolioAllocationResult.optimization` now carries method, feasibility,
frontier, expected-return, volatility, drawdown proxy, CVaR proxy, binding
constraints, and warnings. `PortfolioAllocationResult.candidateDiagnostics`
exposes the candidate universe used to build optimizer inputs.

## Data mode boundary

Demo market data remains the default and is explicitly labeled. The live market
data adapter boundary exists as `liveMarketDataClient.ts`; it currently returns
a live-mode unavailable snapshot with warnings instead of pretending the `/data`
API is wired into portfolio construction.

## Scenario War Room

The Scenario War Room is implemented as a portfolio-first, deterministic stress
surface inside the investment committee packet. It is read-only analysis and
does not call strategy lifecycle, OMS, broker, Alpaca, or order routes.

Implemented files:

- `war-room/regimeCatalog.ts` defines preset regimes for rates shock, liquidity
  crisis, AI multiple compression, energy spike, crypto crash, recession, and
  melt-up.
- `war-room/portfolioStressEngine.ts` applies sector, asset-type, beta,
  volatility, liquidity, and severity shocks to the current allocation.
- `war-room/guardrails.ts` turns severe stress outcomes into operator-facing
  drawdown, concentration, sector, liquidity, and cash-drag breaches.
- `war-room/warRoomReceipt.ts` exports a reproducible local JSON receipt with
  input hash, regime, severity, constraints, warnings, and stress result.
- `ScenarioWarRoomPanel.tsx` mounts the War Room in the packet after allocation
  charts with regime controls, severity controls, KPI strip, contributor tables,
  guardrail messages, warnings, and JSON export.
- `war-room/strategyStressAdapter.ts` is intentionally unavailable until a
  read-only strategy exposure snapshot contract exists.

Future strategy stress must consume an exposure snapshot, not raw strategy
config, order intents, fills, or lifecycle state. Until that contract exists,
strategy stress returns an explicit unavailable state.

## Optimizer Cockpit

The investment committee packet now includes an optimizer cockpit layer that
turns optimizer diagnostics into operator review state.

- `portfolioCockpit.ts` computes a deterministic readiness packet from the
  allocation result and optional AI committee report.
- `PortfolioOptimizerCockpit.tsx` renders the readiness gate, operator actions,
  risk-budget rails, and L1-L4 optimizer evidence stack.
- The cockpit can export a deterministic JSON receipt with schema version,
  readiness state, readiness score, input hash, evidence rows, trace rows,
  budget rails, operator actions, and raw audit payload.
- The cockpit does not change allocations, place trades, or call broker/order
  routes. It only explains whether the generated packet is ready, needs review,
  or is blocked by hard optimizer/risk constraints.
- Tests cover readiness packet generation, blocked-state behavior when
  feasibility fails, and deterministic receipt export.

## Verification checklist

- Dashboard typecheck passes.
- Portfolio-builder tests pass with `pnpm --filter @fincept/dashboard test:portfolio-builder`.
- Edge cases include empty universe, missing bars, all-zero returns, and over-constrained optimization.
