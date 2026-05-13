import assert from "assert";

import {
  buildPortfolioAllocation,
  validatePortfolioBuilderInput,
} from "./portfolioOptimizer";
import {
  buildPortfolioCockpit,
  buildPortfolioCockpitReceipt,
  buildPortfolioCockpitReceiptFilename,
  portfolioCockpitReceiptToJson,
} from "./portfolioCockpit";
import {
  buildCovarianceMatrix,
  buildEfficientFrontier,
  buildReturnInputs,
  applyBlackLittermanViews,
  estimateCvarProxy,
  optimizeInverseVolatility,
  optimizeRiskParity,
  solveConstrainedWeights,
} from "./optimizer";
import {
  getDemoMarketDataSnapshot,
  getUnavailableLiveMarketDataSnapshot,
} from "./marketDataService";
import { buildPortfolioPdfFilename } from "./portfolioExport";
import {
  STRESS_REGIMES,
  buildUnavailableStrategyStressSubject,
  buildWarRoomReceipt,
  groupStressRegimesByPolarity,
  getStressRegime,
  runPortfolioStress,
} from "./war-room";
import type { PortfolioBuilderInput } from "./portfolioBuilder.types";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const baseInput: PortfolioBuilderInput = {
  amount: 25000,
  horizon: "1y",
  riskLevel: "balanced",
  sectors: ["broad_etfs", "semiconductors", "ai_infrastructure"],
  modelProvider: "auto",
  preferences: {
    targetHoldings: 8,
    maxAllocationPerHoldingPct: 18,
    minAllocationPerHoldingPct: 3,
    maxSectorConcentrationPct: 35,
    includeEtfs: true,
    includeStocks: true,
    allowFractionalShares: false,
    cashReservePct: 5,
    preferredTickers: [],
    excludedTickers: [],
    dividendPreference: "neutral",
    volatilityTolerance: "medium",
    rebalanceFrequency: "quarterly",
  },
};

type InputOverrides = Omit<Partial<PortfolioBuilderInput>, "preferences"> & {
  preferences?: Partial<PortfolioBuilderInput["preferences"]>;
};

function makeInput(overrides: InputOverrides = {}) {
  return {
    ...baseInput,
    ...overrides,
    preferences: {
      ...baseInput.preferences,
      ...(overrides.preferences ?? {}),
    },
  };
}

function assertFiniteDeep(value: unknown, path = "root") {
  if (typeof value === "number") {
    assert(Number.isFinite(value), `${path} is not finite`);
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, nested] of Object.entries(value)) {
    assertFiniteDeep(nested, `${path}.${key}`);
  }
}

test("validates positive finite investment amounts", () => {
  assert.doesNotThrow(() => validatePortfolioBuilderInput(makeInput()));
  for (const amount of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => validatePortfolioBuilderInput(makeInput({ amount })));
  }
});

test("never allocates more than the starting amount", () => {
  const result = buildPortfolioAllocation(makeInput());
  assert(result.summary.totalValue <= baseInput.amount + 0.01);
  assert(result.summary.totalInvested <= baseInput.amount + 0.01);
  assert(result.summary.totalCash >= 0);
});

test("respects cash reserve and recalculates leftover cash after whole-share rounding", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      amount: 12000,
      preferences: { cashReservePct: 12, allowFractionalShares: false },
    }),
  );
  assert(result.summary.cashReserve >= 1440 - 0.01);
  assert(result.summary.roundingCash >= 0);
  assert.equal(result.holdings.every((h) => Number.isInteger(h.shares)), true);
});

test("preserves an explicit zero cash reserve instead of applying the risk default", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      preferences: { cashReservePct: 0, allowFractionalShares: false },
    }),
  );
  assert.equal(result.input.preferences.cashReservePct, 0);
  assert.equal(result.summary.cashReserve, 0);
  assert(result.summary.cashPercent < 1, `unexpected residual cash ${result.summary.cashPercent}%`);
  assert(result.summary.roundingCash < 100, `unexpected rounding cash ${result.summary.roundingCash}`);
  assert(!result.constraintsUsed.some((constraint) => constraint === "Cash reserve: 5.0%."));
  assert(result.constraintsUsed.some((constraint) => constraint === "Intentional cash reserve: 0.0%."));
  assert(result.assumptions.some((assumption) => assumption.includes("No intentional cash reserve")));
  assert(result.warnings.some((warning) => warning.includes("overflow candidate")));
  assert(!result.riskAnalysis.drawdownRisk.includes("cash reserve"));
});

test("uses exact fractional shares when enabled", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      amount: 10000,
      preferences: { allowFractionalShares: true, cashReservePct: 0 },
    }),
  );
  assert(result.holdings.some((h) => h.shares % 1 !== 0));
  for (const holding of result.holdings) {
    assert(Math.abs(holding.shares * holding.price - holding.dollarAllocation) < 0.05);
  }
});

test("applies max holding caps", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      amount: 100000,
      preferences: { maxAllocationPerHoldingPct: 10, targetHoldings: 12 },
    }),
  );
  assert(
    result.holdings.every((h) => h.percentAllocation <= 10.25),
    "holding exceeded max cap tolerance",
  );
});

test("drops holdings below min allocation when the portfolio is too small", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      amount: 750,
      preferences: {
        targetHoldings: 12,
        minAllocationPerHoldingPct: 7,
        allowFractionalShares: false,
        cashReservePct: 5,
      },
    }),
  );
  assert(result.holdings.length < 12);
  assert(result.summary.totalValue <= 750.01);
});

test("enforces sector concentration caps", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      amount: 50000,
      sectors: ["semiconductors"],
      preferences: { maxSectorConcentrationPct: 30, targetHoldings: 10 },
    }),
  );
  assert(
    result.summary.largestSectorExposurePct <= 30.75,
    `sector cap exceeded: ${result.summary.largestSectorExposurePct}`,
  );
});

test("falls back to broad diversified defaults when no sectors are selected", () => {
  const result = buildPortfolioAllocation(makeInput({ sectors: [] }));
  assert(result.holdings.length > 0);
  assert(result.holdings.some((h) => h.assetType === "etf"));
});

test("audits selected holdings against the scored candidate universe", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      sectors: ["semiconductors", "ai_infrastructure", "cloud_computing"],
      preferences: { targetHoldings: 6 },
    }),
  );
  assert(result.candidateAudit.universeCount >= result.candidateAudit.eligibleCount);
  assert.equal(result.candidateAudit.selectedCount, result.holdings.length);
  assert(result.candidateAudit.topSelected.length > 0);
  assert(result.candidateAudit.topRejected.length > 0);
  assert(result.candidateAudit.topRejected.every((row) => !row.selected));
  assert(result.candidateAudit.constraintNotes.some((note) => note.includes("Max sector")));
});

test("builds an optimizer cockpit readiness packet from allocation evidence", () => {
  const result = buildPortfolioAllocation(makeInput());
  const cockpit = buildPortfolioCockpit(result, null);
  assert(["ready", "review", "blocked"].includes(cockpit.state));
  assert(Number.isFinite(cockpit.score));
  assert(cockpit.score >= 0 && cockpit.score <= 100);
  assert(cockpit.checks.some((check) => check.id === "optimizer-feasible"));
  assert(cockpit.budgetRails.some((rail) => rail.label === "Single-name cap"));
  assert(cockpit.evidenceRows.length > 0);
  assert(cockpit.payload.optimization);
});

test("blocks the optimizer cockpit when feasibility is false", () => {
  const result = buildPortfolioAllocation(makeInput());
  const cockpit = buildPortfolioCockpit({
    ...result,
    optimization: {
      ...result.optimization,
      feasible: false,
    },
  });
  assert.equal(cockpit.state, "blocked");
  assert(cockpit.checks.some((check) => check.id === "optimizer-feasible" && check.severity === "fail"));
});

test("exports a deterministic optimizer cockpit receipt", () => {
  const result = buildPortfolioAllocation(makeInput());
  const receipt = buildPortfolioCockpitReceipt(result, null);
  const secondReceipt = buildPortfolioCockpitReceipt(result, null);
  const json = portfolioCockpitReceiptToJson(result, null);
  const filename = buildPortfolioCockpitReceiptFilename(result, null);

  assert.equal(receipt.schemaVersion, "fincept.portfolio_cockpit_receipt.v1");
  assert.equal(receipt.inputHash, secondReceipt.inputHash);
  assert.equal(JSON.parse(json).inputHash, receipt.inputHash);
  assert(filename.startsWith("fincept-portfolio-cockpit-25000-balanced-1y-"));
  assert(filename.endsWith(".json"));
});

test("builds deterministic PDF print filenames from portfolio context", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      amount: 25000,
      horizon: "10y_plus",
      riskLevel: "aggressive_growth",
    }),
  );
  const filename = buildPortfolioPdfFilename(result);
  assert.equal(filename, "fincept-portfolio-25000-aggressive-growth-10y-plus.pdf");
});

test("excludes tickers and prioritizes valid preferred tickers", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      sectors: ["semiconductors", "ai_infrastructure", "broad_etfs"],
      preferences: {
        preferredTickers: ["NVDA", "MSFT"],
        excludedTickers: ["AMD"],
        targetHoldings: 8,
      },
    }),
  );
  const tickers = result.holdings.map((h) => h.ticker);
  assert(!tickers.includes("AMD"));
  assert(tickers.includes("NVDA") || tickers.includes("MSFT"));
});

test("skips candidates with missing or invalid quotes", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      amount: 10000,
      preferences: { preferredTickers: ["BROKEN"], targetHoldings: 6 },
    }),
  );
  assert(!result.holdings.some((h) => h.ticker === "BROKEN"));
  assert(result.warnings.some((warning) => warning.includes("BROKEN")));
});

test("does not return NaN or Infinity anywhere in the result", () => {
  assertFiniteDeep(buildPortfolioAllocation(makeInput()));
});

test("percent totals are within rounding tolerance", () => {
  const result = buildPortfolioAllocation(makeInput());
  const holdingsPct = result.holdings.reduce((sum, h) => sum + h.percentAllocation, 0);
  const totalPct = holdingsPct + result.summary.cashPercent;
  assert(Math.abs(totalPct - 100) <= 0.25, `percent total was ${totalPct}`);
});

test("handles large portfolios without exceeding caps", () => {
  const result = buildPortfolioAllocation(
    makeInput({
      amount: 5_000_000,
      horizon: "10y_plus",
      riskLevel: "aggressive_growth",
      sectors: ["ai_infrastructure", "semiconductors", "cloud_computing", "cybersecurity"],
      preferences: {
        targetHoldings: 18,
        maxAllocationPerHoldingPct: 12,
        maxSectorConcentrationPct: 35,
        cashReservePct: 2,
      },
    }),
  );
  assert(result.holdings.length >= 10);
  assert(result.summary.totalValue <= 5_000_000.01);
});

test("builds finite return inputs from eligible market candidates", () => {
  const result = buildPortfolioAllocation(makeInput());
  const inputs = buildReturnInputs(result.candidateDiagnostics, result.input.riskLevel, result.input.horizon);
  assert(inputs.length > 0);
  assert.deepEqual(
    inputs.map((input) => input.ticker),
    [...inputs].map((input) => input.ticker).sort(),
  );
  for (const input of inputs) {
    assert(Number.isFinite(input.expectedReturnPct), `${input.ticker} expected return is not finite`);
    assert(Number.isFinite(input.annualVolatilityPct), `${input.ticker} volatility is not finite`);
    assert(input.confidence >= 0 && input.confidence <= 1, `${input.ticker} confidence was out of range`);
  }
});

test("constraint solver normalizes weights inside holding and sector caps", () => {
  const result = buildPortfolioAllocation(makeInput());
  const inputs = buildReturnInputs(result.candidateDiagnostics, result.input.riskLevel, result.input.horizon).slice(0, 8);
  const proposed = inputs.map(() => 100 / inputs.length);
  const solved = solveConstrainedWeights(proposed, inputs, {
    maxAllocationPerHoldingPct: 8,
    minAllocationPerHoldingPct: 2,
    maxSectorConcentrationPct: 30,
    cashReservePct: 5,
  });
  assert(solved.feasible);
  assert(solved.totalWeightPct <= 95.01);
  assert(solved.weights.every((weight) => weight <= 8.01));
  assert(solved.diagnostics.bindingConstraints.length > 0);
});

test("inverse volatility and risk parity optimizers return different finite portfolios", () => {
  const result = buildPortfolioAllocation(makeInput());
  const inputs = buildReturnInputs(result.candidateDiagnostics, result.input.riskLevel, result.input.horizon).slice(0, 10);
  const covariance = buildCovarianceMatrix(inputs);
  const constraints = {
    maxAllocationPerHoldingPct: 18,
    minAllocationPerHoldingPct: 1,
    maxSectorConcentrationPct: 40,
    cashReservePct: 5,
  };
  const inverseVol = optimizeInverseVolatility(inputs, constraints);
  const riskParity = optimizeRiskParity(inputs, covariance, constraints);
  assert(inverseVol.diagnostics.feasible);
  assert(riskParity.diagnostics.feasible);
  assert.notDeepEqual(inverseVol.weights.map((weight) => weight.toFixed(4)), riskParity.weights.map((weight) => weight.toFixed(4)));
  assertFiniteDeep(inverseVol);
  assertFiniteDeep(riskParity);
});

test("efficient frontier returns sorted feasible points", () => {
  const result = buildPortfolioAllocation(makeInput());
  const inputs = buildReturnInputs(result.candidateDiagnostics, result.input.riskLevel, result.input.horizon).slice(0, 8);
  const covariance = buildCovarianceMatrix(inputs);
  const frontier = buildEfficientFrontier(inputs, covariance, {
    maxAllocationPerHoldingPct: 20,
    minAllocationPerHoldingPct: 1,
    maxSectorConcentrationPct: 45,
    cashReservePct: 5,
  });
  assert(frontier.length >= 3);
  for (let index = 1; index < frontier.length; index += 1) {
    assert(frontier[index].expectedReturnPct >= frontier[index - 1].expectedReturnPct);
  }
  assert(frontier.some((point) => point.feasible));
  assertFiniteDeep(frontier);
});

test("Black-Litterman views blend returns by confidence and reject unknown tickers", () => {
  const snapshot = getDemoMarketDataSnapshot();
  const inputs = buildReturnInputs(snapshot.candidates, "balanced", "1y");
  const nvda = inputs.find((input) => input.ticker === "NVDA");
  assert(nvda);
  const blended = applyBlackLittermanViews(inputs, [
    { ticker: "NVDA", expectedReturnDeltaPct: 4, confidence: 0.25 },
    { ticker: "NOTREAL", expectedReturnDeltaPct: 10, confidence: 1 },
  ]);
  const blendedNvda = blended.inputs.find((input) => input.ticker === "NVDA");
  assert(blendedNvda);
  assert.equal(blendedNvda.expectedReturnPct, Math.round((nvda.expectedReturnPct + 1) * 100) / 100);
  assert(blended.warnings.some((warning) => warning.includes("NOTREAL")));
});

test("CVaR proxy is finite and labels scenario limitations", () => {
  const snapshot = getDemoMarketDataSnapshot();
  const inputs = buildReturnInputs(snapshot.candidates, "growth", "3y").slice(0, 6);
  const cvar = estimateCvarProxy(inputs.map(() => 100 / inputs.length), inputs);
  assert(cvar.cvarProxyPct > 0);
  assert(cvar.maxDrawdownProxyPct > cvar.cvarProxyPct);
  assert(cvar.warnings.some((warning) => warning.includes("proxy")));
  assertFiniteDeep(cvar);
});

test("live market data boundary returns explicit unavailable diagnostics", async () => {
  const snapshot = await getUnavailableLiveMarketDataSnapshot();
  assert.equal(snapshot.dataMode, "live");
  assert.equal(snapshot.candidates.length, 0);
  assert(snapshot.warnings.some((warning) => warning.includes("not wired")));
});

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
    "soft_landing",
    "fed_pivot",
    "ai_capex_acceleration",
    "productivity_boom",
    "earnings_recession",
    "stagflation",
    "dollar_spike",
    "geopolitical_supply_shock",
  ]);
  for (const regime of STRESS_REGIMES) {
    assert(regime.label.length > 0);
    assert(regime.description.length > 0);
    assert(Number.isFinite(regime.betaShockMultiplier));
    assert(Number.isFinite(regime.volatilityShockMultiplier));
  }
});

test("scenario war room includes both positive and negative expanded regimes", () => {
  const allocation = buildPortfolioAllocation(makeInput());
  const softLanding = runPortfolioStress(allocation, {
    regimeId: "soft_landing",
    severity: "base",
  });
  const stagflation = runPortfolioStress(allocation, {
    regimeId: "stagflation",
    severity: "base",
  });
  assert(softLanding.pnlDelta > 0);
  assert(stagflation.pnlDelta < 0);
  assert(softLanding.bestContributors.length > 0);
  assert(stagflation.worstContributors.length > 0);
});

test("scenario war room regimes carry UI grouping metadata", () => {
  const grouped = groupStressRegimesByPolarity(STRESS_REGIMES);
  assert(grouped.upside.some((regime) => regime.id === "soft_landing"));
  assert(grouped.downside.some((regime) => regime.id === "stagflation"));
  assert(grouped.mixed.some((regime) => regime.id === "energy_spike"));
  for (const regime of STRESS_REGIMES) {
    assert(["upside", "downside", "mixed"].includes(regime.polarity));
    assert(regime.category.length > 0);
  }
});

test("scenario war room resolves regimes by id", () => {
  assert.equal(getStressRegime("recession").label, "Recession");
  assert.throws(() => getStressRegime("unknown" as never), /Unknown stress regime/);
});

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

test("scenario war room strategy adapter is explicitly unavailable until exposure contracts exist", () => {
  const subject = buildUnavailableStrategyStressSubject("strategy-demo");
  assert.equal(subject.subjectType, "strategy");
  assert.equal(subject.available, false);
  assert(subject.warnings.some((warning) => warning.includes("read-only strategy exposure")));
});

async function run() {
  let passed = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      passed += 1;
      console.log(`ok - ${name}`);
    } catch (error) {
      console.error(`not ok - ${name}`);
      console.error(error);
      process.exitCode = 1;
      return;
    }
  }
  console.log(`${passed} portfolio builder tests passed`);
}

void run();
