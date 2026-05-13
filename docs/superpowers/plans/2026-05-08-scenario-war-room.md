# Scenario War Room Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Scenario War Room that stress-tests a portfolio first, then strategy exposures later, against preset market regimes such as rates shock, liquidity crisis, AI multiple compression, energy spike, crypto crash, recession, and melt-up.

**Architecture:** Start as a deterministic dashboard feature inside `/portfolio-builder` using pure TypeScript stress engines and the existing `PortfolioAllocationResult` contract. Keep the first slice browser-local and analysis-only; later slices can add read-only strategy exposure adapters and receipt exports without calling OMS, broker, or strategy lifecycle routes.

**Tech Stack:** Next.js dashboard, TypeScript portfolio-builder modules, existing portfolio-builder test harness, lucide-react icons, existing dashboard card/table styles, `pnpm --filter @fincept/dashboard test:portfolio-builder`, `pnpm --filter @fincept/dashboard typecheck`.

---

## Product Boundary

The Scenario War Room is a stress-analysis surface, not an execution surface.

- It may estimate portfolio or strategy impact under synthetic regimes.
- It may export a JSON receipt for audit and later comparison.
- It must not place orders.
- It must not create `OrderIntent`, `Fill`, broker, Alpaca, or OMS calls.
- It must label stress output as estimated, deterministic, and dependent on the input allocation or exposure snapshot.
- It must degrade clearly when live data, strategy exposure, or benchmark inputs are unavailable.

## Current Integration Points

- Portfolio result type: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.types.ts`
- Portfolio report UI: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`
- Existing optimizer diagnostics: `PortfolioAllocationResult.optimization`
- Existing candidate universe diagnostics: `PortfolioAllocationResult.candidateDiagnostics`
- Current test harness: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`
- Existing plan overlap: `docs/superpowers/plans/2026-05-08-portfolio-optimizer-tracks.md` Track 2 Task 2.4
- User-facing docs: `docs/portfoliooptimizer.md`

## MVP Scope

Build portfolio-first stress testing for these preset regimes:

- `rates_shock`: long-duration growth pressure, treasury stability, equity multiple compression.
- `liquidity_crisis`: broad drawdown, higher penalty for high beta and low liquidity.
- `ai_multiple_compression`: AI infrastructure, semiconductors, cloud, and cybersecurity valuation reset.
- `energy_spike`: energy and oil/gas positive shock, consumer/industrials pressure, inflation-sensitive drag.
- `crypto_crash`: risk appetite shock for speculative/high-beta names; no direct crypto holdings required.
- `recession`: defensive healthcare, utilities, cash/treasuries hold up; cyclicals and high beta weaken.
- `melt_up`: broad risk-on rally with growth/high-beta upside and cash opportunity cost.

## File Map

- Create: `apps/dashboard/src/features/portfolio-builder/war-room/warRoomTypes.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/war-room/regimeCatalog.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/war-room/portfolioStressEngine.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/war-room/guardrails.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/war-room/warRoomReceipt.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/war-room/index.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/ScenarioWarRoomPanel.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`
- Modify: `docs/portfoliooptimizer.md`

## Data Contracts

Use these names exactly unless the implementation discovers an existing equivalent:

```ts
export type StressRegimeId =
  | "rates_shock"
  | "liquidity_crisis"
  | "ai_multiple_compression"
  | "energy_spike"
  | "crypto_crash"
  | "recession"
  | "melt_up";

export type StressSeverity = "mild" | "base" | "severe";

export interface StressRegime {
  id: StressRegimeId;
  label: string;
  description: string;
  defaultSeverity: StressSeverity;
  sectorShockPct: Partial<Record<PortfolioSector, number>>;
  assetTypeShockPct: Partial<Record<PortfolioAssetType, number>>;
  betaShockMultiplier: number;
  volatilityShockMultiplier: number;
  liquidityHaircutPct: number;
  warnings: string[];
}

export interface StressHoldingResult {
  ticker: string;
  name: string;
  sector: PortfolioSector;
  assetType: PortfolioAssetType;
  startingValue: number;
  stressedValue: number;
  pnlDelta: number;
  pnlDeltaPct: number;
  contributionPct: number;
  appliedShockPct: number;
  explanation: string;
}

export interface StressGuardrailBreach {
  id: "drawdown" | "single_name" | "sector" | "liquidity" | "cash_drag";
  severity: "info" | "warn" | "critical";
  message: string;
}

export interface StressResult {
  regimeId: StressRegimeId;
  severity: StressSeverity;
  startingValue: number;
  stressedValue: number;
  pnlDelta: number;
  pnlDeltaPct: number;
  worstContributors: StressHoldingResult[];
  bestContributors: StressHoldingResult[];
  holdings: StressHoldingResult[];
  guardrailBreaches: StressGuardrailBreach[];
  warnings: string[];
}
```

## Task 1: Regime Catalog

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/war-room/warRoomTypes.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/war-room/regimeCatalog.ts`
- Create: `apps/dashboard/src/features/portfolio-builder/war-room/index.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] **Step 1: Write catalog tests**

Add tests that assert:

```ts
import {
  STRESS_REGIMES,
  getStressRegime,
} from "./war-room";

test("scenario war room exposes all required preset regimes", () => {
  const ids = STRESS_REGIMES.map((regime) => regime.id);
  assert.deepEqual(ids, [
    "rates_shock",
    "liquidity_crisis",
    "ai_multiple_compression",
    "energy_spike",
    "crypto_crash",
    "recession",
    "melt_up",
  ]);
  for (const regime of STRESS_REGIMES) {
    assert(regime.label.length > 0);
    assert(regime.description.length > 0);
    assert(Number.isFinite(regime.betaShockMultiplier));
    assert(Number.isFinite(regime.volatilityShockMultiplier));
  }
});

test("scenario war room resolves regimes by id", () => {
  assert.equal(getStressRegime("recession").label, "Recession");
  assert.throws(() => getStressRegime("unknown" as never), /Unknown stress regime/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: FAIL because `./war-room` does not exist.

- [ ] **Step 3: Implement catalog**

Create `warRoomTypes.ts`, `regimeCatalog.ts`, and `index.ts` with the exact contracts above. Each regime must include at least one sector or asset-type shock and finite multipliers.

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: PASS for the two new catalog tests and all existing portfolio-builder tests.

## Task 2: Portfolio Stress Engine

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/war-room/portfolioStressEngine.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/war-room/index.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] **Step 1: Write stress engine tests**

Add tests that assert:

```ts
import {
  runPortfolioStress,
} from "./war-room";

test("scenario war room stress result is finite and value preserving", () => {
  const allocation = buildPortfolioAllocation(makeInput());
  const result = runPortfolioStress(allocation, {
    regimeId: "recession",
    severity: "base",
  });
  assert.equal(result.regimeId, "recession");
  assert(result.startingValue > 0);
  assert(result.stressedValue >= 0);
  assert(result.pnlDelta <= 0);
  assert(result.holdings.length === allocation.holdings.length);
  assert(result.worstContributors.length > 0);
  assertFiniteDeep(result);
});

test("AI multiple compression penalizes semiconductor and AI infrastructure holdings", () => {
  const allocation = buildPortfolioAllocation(
    makeInput({
      sectors: ["semiconductors", "ai_infrastructure", "cloud_computing"],
      riskLevel: "growth",
    }),
  );
  const result = runPortfolioStress(allocation, {
    regimeId: "ai_multiple_compression",
    severity: "severe",
  });
  assert(result.pnlDelta < 0);
  assert(
    result.worstContributors.some((holding) =>
      ["semiconductors", "ai_infrastructure", "cloud_computing"].includes(holding.sector),
    ),
  );
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: FAIL because `runPortfolioStress` does not exist.

- [ ] **Step 3: Implement `runPortfolioStress`**

Rules:

- Start from `allocation.holdings`.
- Calculate a base shock from asset type plus sector shock.
- Add beta-sensitive adjustment: `(holding.beta - 1) * regime.betaShockMultiplier`, using `candidateDiagnostics` to find beta if the holding does not carry one.
- Add volatility-sensitive adjustment for severe shocks using candidate volatility.
- Multiply shock by severity: mild `0.5`, base `1`, severe `1.5`.
- Clamp total shock to `-95..150`.
- Calculate per-holding stressed value and contribution.
- Sort `worstContributors` ascending by `pnlDelta`.
- Sort `bestContributors` descending by `pnlDelta`.
- Push warnings when candidate diagnostics are missing.

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: PASS for stress engine tests.

## Task 3: Guardrails

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/war-room/guardrails.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/war-room/portfolioStressEngine.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/war-room/index.ts`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] **Step 1: Write guardrail tests**

Add tests that assert:

```ts
test("scenario war room emits guardrail breaches for severe drawdowns", () => {
  const allocation = buildPortfolioAllocation(
    makeInput({
      riskLevel: "speculative",
      sectors: ["semiconductors", "ai_infrastructure", "cybersecurity"],
      preferences: { cashReservePct: 0, maxAllocationPerHoldingPct: 24 },
    }),
  );
  const result = runPortfolioStress(allocation, {
    regimeId: "liquidity_crisis",
    severity: "severe",
  });
  assert(result.guardrailBreaches.some((breach) => breach.id === "drawdown"));
  assert(result.guardrailBreaches.some((breach) => breach.severity === "critical"));
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: FAIL because guardrail breaches are empty or missing.

- [ ] **Step 3: Implement guardrails**

Rules:

- Drawdown warning at `pnlDeltaPct <= -8`, critical at `<= -18`.
- Single-name warning if one holding contributes more than `35%` of total loss.
- Sector warning if largest sector exposure is above `40%` and portfolio loss is negative.
- Liquidity warning if regime has a liquidity haircut and stock exposure is above ETF exposure.
- Cash drag info in melt-up if cash percent is above `10`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: PASS for guardrail tests.

## Task 4: Scenario War Room Panel

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/ScenarioWarRoomPanel.tsx`
- Modify: `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx`

- [ ] **Step 1: Add panel component**

Component props:

```ts
import { useMemo, useState } from "react";
import type { PortfolioAllocationResult } from "./portfolioBuilder.types";
import {
  STRESS_REGIMES,
  runPortfolioStress,
  type StressRegimeId,
  type StressSeverity,
} from "./war-room";

export function ScenarioWarRoomPanel({
  allocation,
}: {
  allocation: PortfolioAllocationResult;
}) {
  const [regimeId, setRegimeId] = useState<StressRegimeId>("recession");
  const [severity, setSeverity] = useState<StressSeverity>("base");
  const result = useMemo(
    () => runPortfolioStress(allocation, { regimeId, severity }),
    [allocation, regimeId, severity],
  );

  return null;
}
```

Panel behavior:

- Use icon buttons or compact segmented controls for regime/severity selection.
- Render starting value, stressed value, estimated P&L, estimated P&L percent, and breached guardrails.
- Render worst contributors and best contributors in dense tables.
- Render warnings prominently when demo data or missing diagnostics affect results.
- Use existing `Card`, `CardContent`, `CardHeader`, `CardTitle`, `Button`, and `KpiTile` components.
- Keep text compact; do not add marketing copy.

- [ ] **Step 2: Wire into report view**

Add `<ScenarioWarRoomPanel allocation={allocation} />` after charts and before the text risk panels in `PortfolioReportView.tsx`.

- [ ] **Step 3: Run typecheck**

Run: `pnpm --filter @fincept/dashboard typecheck`

Expected: PASS.

## Task 5: War Room Receipt Export

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/war-room/warRoomReceipt.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/war-room/index.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/ScenarioWarRoomPanel.tsx`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] **Step 1: Write receipt tests**

Add tests that assert:

```ts
import {
  buildWarRoomReceipt,
} from "./war-room";

test("scenario war room receipt carries reproducible audit metadata", () => {
  const allocation = buildPortfolioAllocation(makeInput());
  const result = runPortfolioStress(allocation, {
    regimeId: "rates_shock",
    severity: "base",
  });
  const receipt = buildWarRoomReceipt(allocation, result);
  assert.equal(receipt.kind, "scenario_war_room_receipt");
  assert.equal(receipt.regimeId, "rates_shock");
  assert.equal(receipt.subjectType, "portfolio");
  assert(receipt.inputHash.length >= 12);
  assert(receipt.warnings.length >= result.warnings.length);
  assertFiniteDeep(receipt.result);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: FAIL because `buildWarRoomReceipt` does not exist.

- [ ] **Step 3: Implement receipt builder**

Receipt fields:

- `kind: "scenario_war_room_receipt"`
- `generatedAt`
- `subjectType: "portfolio"`
- `allocationMethod`
- `dataMode`
- `regimeId`
- `severity`
- `inputHash`
- `constraintsUsed`
- `warnings`
- `result`

Use deterministic JSON stringification for the hash input and a small non-crypto hash helper. This is an audit correlation id, not a security primitive.

- [ ] **Step 4: Add export button**

Add a JSON export button to `ScenarioWarRoomPanel` using the existing `downloadTextFile` helper.

- [ ] **Step 5: Run tests**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: PASS.

## Task 6: Strategy Stress Adapter Scaffold

**Files:**

- Create: `apps/dashboard/src/features/portfolio-builder/war-room/strategyStressAdapter.ts`
- Modify: `apps/dashboard/src/features/portfolio-builder/war-room/index.ts`
- Modify: `docs/portfoliooptimizer.md`
- Test: `apps/dashboard/src/features/portfolio-builder/portfolioBuilder.test.ts`

- [ ] **Step 1: Write adapter tests**

Add tests that assert:

```ts
import {
  buildUnavailableStrategyStressSubject,
} from "./war-room";

test("scenario war room strategy adapter is explicitly unavailable until exposure contracts exist", () => {
  const subject = buildUnavailableStrategyStressSubject("strategy-demo");
  assert.equal(subject.subjectType, "strategy");
  assert.equal(subject.available, false);
  assert(subject.warnings.some((warning) => warning.includes("read-only strategy exposure")));
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter @fincept/dashboard test:portfolio-builder`

Expected: FAIL because strategy adapter does not exist.

- [ ] **Step 3: Implement unavailable adapter**

The adapter should return a typed unavailable state only. Do not call strategy routes yet.

- [ ] **Step 4: Document future strategy path**

Update `docs/portfoliooptimizer.md` with:

- portfolio stress is implemented first,
- strategy stress is read-only and future-facing,
- no strategy lifecycle or OMS routes are called,
- the required future input is an exposure snapshot, not raw strategy config.

- [ ] **Step 5: Run verification**

Run:

```powershell
pnpm --filter @fincept/dashboard test:portfolio-builder
pnpm --filter @fincept/dashboard typecheck
```

Expected: both PASS.

## Suggested UI Layout

Place the panel in the investment packet after allocation charts:

- Header: `Scenario War Room`
- Left rail: compact regime selector.
- Top row: severity selector plus JSON export button.
- KPI strip: stressed value, estimated P&L, estimated P&L percent, guardrails.
- Main grid: worst contributors and best contributors.
- Bottom strip: warnings and regime notes.

Use restrained dashboard styling:

- No hero section.
- No decorative background.
- No nested cards.
- Dense tables and compact controls.
- Red/amber/green risk coloring from existing semantic classes.

## Future Strategy Stress Path

The strategy version should wait for a read-only exposure contract:

```ts
export interface StrategyStressExposureSnapshot {
  strategyId: string;
  timestamp: string;
  nav: number;
  exposures: Array<{
    ticker: string;
    sector?: PortfolioSector;
    assetType?: PortfolioAssetType;
    notional: number;
    beta?: number;
    volatility?: number;
  }>;
  warnings: string[];
}
```

The strategy path should consume snapshots from an API route later. It must not infer exposure by reading order intents, and it must not start/stop strategies.

## GPT-5.4 Implementation Prompt

```text
Implement the Scenario War Room from docs/superpowers/plans/2026-05-08-scenario-war-room.md.

Rules:
- Work in C:\Users\nolan\CascadeProjects\fincept-terminal.
- Keep the first implementation portfolio-first inside apps/dashboard/src/features/portfolio-builder.
- Do not call OMS, broker, Alpaca, strategy lifecycle, or order routes.
- Do not add server persistence.
- Add deterministic TypeScript stress modules under apps/dashboard/src/features/portfolio-builder/war-room/.
- Add ScenarioWarRoomPanel and mount it in PortfolioReportView.
- Start each task by adding the specified tests to portfolioBuilder.test.ts.
- Run pnpm --filter @fincept/dashboard test:portfolio-builder after each task.
- Run pnpm --filter @fincept/dashboard typecheck before reporting completion.
- Update docs/portfoliooptimizer.md with exactly what landed.
```

## Verification Matrix

- `pnpm --filter @fincept/dashboard test:portfolio-builder`
- `pnpm --filter @fincept/dashboard typecheck`

## Self-Review Notes

- The MVP is portfolio-first because current portfolio allocations already have holdings, sectors, asset types, risk scores, prices, and candidate diagnostics.
- Strategy stress is intentionally scaffolded as unavailable until a read-only exposure snapshot contract exists.
- The feature remains analysis-only and cannot trigger live or paper orders.
- Receipts are local JSON exports, not server-persisted artifacts in the first slice.
- The preset regimes are deterministic and explainable, so operators can challenge assumptions instead of trusting opaque model output.
