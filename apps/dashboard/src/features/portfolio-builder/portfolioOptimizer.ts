import {
  getDemoMarketDataSnapshot,
} from "./marketDataService";
import {
  buildCovarianceMatrix,
  buildEfficientFrontier,
  buildReturnInputs,
} from "./optimizer";
import {
  bucketRisk,
  buildRiskAnalysis,
  riskRating,
  roundMoney,
  roundPct,
  safeNumber,
  suggestedRebalanceFrequency,
} from "./portfolioRisk";
import type {
  AllocationBucket,
  PortfolioAllocationResult,
  PortfolioAssetType,
  PortfolioBuilderInput,
  PortfolioBuilderPreferences,
  PortfolioCandidateAudit,
  PortfolioCandidateAuditRow,
  PortfolioHolding,
  PortfolioHoldingCandidate,
  PortfolioOptimizationDiagnostics,
  PortfolioRiskLevel,
  PortfolioSector,
  PortfolioTimeHorizon,
} from "./portfolioBuilder.types";

const DEFAULT_SECTORS: PortfolioSector[] = [
  "broad_etfs",
  "cash_treasuries",
  "healthcare",
  "financials",
  "industrials",
  "semiconductors",
];

const RISK_DEFAULTS: Record<
  PortfolioRiskLevel,
  Pick<
    PortfolioBuilderPreferences,
    | "targetHoldings"
    | "maxAllocationPerHoldingPct"
    | "minAllocationPerHoldingPct"
    | "maxSectorConcentrationPct"
    | "cashReservePct"
  >
> = {
  conservative: {
    targetHoldings: 12,
    maxAllocationPerHoldingPct: 8,
    minAllocationPerHoldingPct: 3,
    maxSectorConcentrationPct: 25,
    cashReservePct: 12,
  },
  balanced: {
    targetHoldings: 10,
    maxAllocationPerHoldingPct: 14,
    minAllocationPerHoldingPct: 3,
    maxSectorConcentrationPct: 32,
    cashReservePct: 5,
  },
  growth: {
    targetHoldings: 10,
    maxAllocationPerHoldingPct: 16,
    minAllocationPerHoldingPct: 2,
    maxSectorConcentrationPct: 38,
    cashReservePct: 3,
  },
  aggressive_growth: {
    targetHoldings: 8,
    maxAllocationPerHoldingPct: 20,
    minAllocationPerHoldingPct: 2,
    maxSectorConcentrationPct: 45,
    cashReservePct: 2,
  },
  speculative: {
    targetHoldings: 6,
    maxAllocationPerHoldingPct: 24,
    minAllocationPerHoldingPct: 1,
    maxSectorConcentrationPct: 50,
    cashReservePct: 2,
  },
};

const HORIZON_ADJUSTMENTS: Record<
  PortfolioTimeHorizon,
  { etfBoost: number; stockBoost: number; lowRiskBoost: number; concentrationMultiplier: number }
> = {
  "3m": { etfBoost: 1.45, stockBoost: 0.72, lowRiskBoost: 1.5, concentrationMultiplier: 0.65 },
  "6m": { etfBoost: 1.35, stockBoost: 0.8, lowRiskBoost: 1.35, concentrationMultiplier: 0.75 },
  "1y": { etfBoost: 1.15, stockBoost: 0.95, lowRiskBoost: 1.1, concentrationMultiplier: 0.9 },
  "3y": { etfBoost: 1, stockBoost: 1.05, lowRiskBoost: 1, concentrationMultiplier: 1 },
  "5y": { etfBoost: 0.95, stockBoost: 1.15, lowRiskBoost: 0.95, concentrationMultiplier: 1.08 },
  "10y_plus": { etfBoost: 0.9, stockBoost: 1.25, lowRiskBoost: 0.9, concentrationMultiplier: 1.15 },
  custom: { etfBoost: 1.05, stockBoost: 1, lowRiskBoost: 1, concentrationMultiplier: 1 },
};

interface ScoredCandidate {
  candidate: PortfolioHoldingCandidate;
  score: number;
}

interface CandidateSelection {
  candidates: ScoredCandidate[];
  estimatedCapacityPct: number;
  expandedBeyondTarget: boolean;
  expandedBeyondSelectedSectors: boolean;
}

export function defaultPortfolioPreferences(
  riskLevel: PortfolioRiskLevel = "balanced",
): PortfolioBuilderPreferences {
  return {
    ...RISK_DEFAULTS[riskLevel],
    includeEtfs: true,
    includeStocks: true,
    allowFractionalShares: false,
    preferredTickers: [],
    excludedTickers: [],
    dividendPreference: "neutral",
    volatilityTolerance: "medium",
    rebalanceFrequency: suggestedRebalanceFrequency(riskLevel, "1y"),
  };
}

export function validatePortfolioBuilderInput(input: PortfolioBuilderInput): void {
  if (!Number.isFinite(input.amount) || input.amount <= 0) {
    throw new Error("Investment amount must be a positive finite number.");
  }
  const p = input.preferences;
  if (!Number.isFinite(p.targetHoldings) || p.targetHoldings <= 0) {
    throw new Error("Target holdings must be a positive number.");
  }
  for (const [label, value] of [
    ["max allocation", p.maxAllocationPerHoldingPct],
    ["min allocation", p.minAllocationPerHoldingPct],
    ["max sector concentration", p.maxSectorConcentrationPct],
    ["cash reserve", p.cashReservePct],
  ] as const) {
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      throw new Error(`${label} must be a percentage between 0 and 100.`);
    }
  }
  if (p.minAllocationPerHoldingPct > p.maxAllocationPerHoldingPct) {
    throw new Error("Min allocation cannot be greater than max allocation.");
  }
  if (!p.includeEtfs && !p.includeStocks) {
    throw new Error("At least one of ETFs or individual stocks must be enabled.");
  }
}

export function buildPortfolioAllocation(input: PortfolioBuilderInput): PortfolioAllocationResult {
  validatePortfolioBuilderInput(input);

  const marketData = getDemoMarketDataSnapshot();
  const warnings = [...marketData.warnings];
  const preferences = normalizePreferences(input.preferences, input.riskLevel, input.horizon);
  const selectedSectors = input.sectors.length > 0 ? input.sectors : DEFAULT_SECTORS;
  const excluded = normalizeTickers(preferences.excludedTickers);
  const preferred = normalizeTickers(preferences.preferredTickers);

  const scored = marketData.candidates
    .filter((candidate) => candidate.assetType !== "cash")
    .filter((candidate) => preferences.includeEtfs || candidate.assetType !== "etf")
    .filter((candidate) => preferences.includeStocks || candidate.assetType !== "stock")
    .filter((candidate) => !excluded.has(candidate.ticker))
    .map((candidate) => {
      if (!candidate.price || candidate.price <= 0 || !Number.isFinite(candidate.price)) {
        if (preferred.has(candidate.ticker)) {
          warnings.push(`${candidate.ticker} was preferred but skipped because no valid quote is available.`);
        }
        return null;
      }
      return {
        candidate,
        score: scoreCandidate(candidate, selectedSectors, preferred, input.riskLevel, input.horizon, preferences),
      };
    })
    .filter((candidate): candidate is ScoredCandidate => Boolean(candidate))
    .filter((candidate) => candidate.score > 0)
    .sort((a, b) => b.score - a.score || a.candidate.ticker.localeCompare(b.candidate.ticker));

  if (!scored.length) {
    throw new Error("No eligible holdings were available after applying filters.");
  }

  const targetCount = Math.max(1, Math.min(Math.round(preferences.targetHoldings), scored.length));
  const selection = selectCandidatesForDeployment(
    scored,
    preferred,
    targetCount,
    preferences,
    selectedSectors,
  );
  const selectedCandidates = selection.candidates;
  if (selection.expandedBeyondTarget) {
    warnings.push(
      `Optimizer added ${selectedCandidates.length - targetCount} overflow candidate(s) because the target count, concentration caps, and reserve setting would otherwise strand investable cash; estimated deployment capacity is ${selection.estimatedCapacityPct.toFixed(1)}%.`,
    );
  }
  if (selection.expandedBeyondSelectedSectors) {
    warnings.push(
      "Optimizer expanded into the next-best eligible sectors because the selected themes and caps could not absorb the full investable amount.",
    );
  }
  const cashReserve = roundMoney(input.amount * (preferences.cashReservePct / 100));
  const investable = Math.max(0, input.amount - cashReserve);
  const weights = allocateWeights(selectedCandidates, preferences, selectedSectors);
  const holdings = deployResidualCash(
    buildHoldings(selectedCandidates, weights, input.amount, investable, preferences),
    selectedCandidates,
    investable,
    preferences,
    selectedSectors,
  );

  let totalInvested = roundMoney(holdings.reduce((sum, h) => sum + h.dollarAllocation, 0));
  let roundingCash = roundMoney(Math.max(0, investable - totalInvested));
  const totalCash = roundMoney(cashReserve + roundingCash);
  const totalValue = roundMoney(totalInvested + totalCash);

  for (const holding of holdings) {
    holding.percentAllocation = totalValue > 0 ? roundPct((holding.dollarAllocation / totalValue) * 100) : 0;
  }

  totalInvested = roundMoney(holdings.reduce((sum, h) => sum + h.dollarAllocation, 0));
  roundingCash = roundMoney(Math.max(0, input.amount - cashReserve - totalInvested));

  const sectorAllocations = buckets(holdings, (h) => h.sector, totalValue);
  const assetTypeAllocations = buckets(holdings, (h) => h.assetType, totalValue, totalCash, "cash");
  const riskBuckets = bucketRisk(holdings);
  const candidateAudit = buildCandidateAudit(
    marketData.candidates,
    scored,
    holdings,
    preferences,
    selectedSectors,
  );
  const largestHolding = holdings.reduce<PortfolioHolding | null>(
    (largest, h) => (!largest || h.percentAllocation > largest.percentAllocation ? h : largest),
    null,
  );
  const largestSector = sectorAllocations.reduce<AllocationBucket | null>(
    (largest, bucket) => (!largest || bucket.percent > largest.percent ? bucket : largest),
    null,
  );
  const averageRiskScore =
    totalInvested > 0
      ? holdings.reduce((sum, h) => sum + h.riskScore * h.dollarAllocation, 0) / totalInvested
      : 0;
  const etfValue = holdings
    .filter((h) => h.assetType === "etf")
    .reduce((sum, h) => sum + h.dollarAllocation, 0);
  const stockValue = holdings
    .filter((h) => h.assetType === "stock")
    .reduce((sum, h) => sum + h.dollarAllocation, 0);
  const treasuryValue = holdings
    .filter((h) => h.assetType === "treasury")
    .reduce((sum, h) => sum + h.dollarAllocation, 0);

  const summary = {
    startingAmount: roundMoney(input.amount),
    totalInvested,
    cashReserve,
    roundingCash,
    totalCash: roundMoney(cashReserve + roundingCash),
    totalValue: roundMoney(totalInvested + cashReserve + roundingCash),
    cashPercent: roundPct(((cashReserve + roundingCash) / input.amount) * 100),
    numberOfPositions: holdings.length,
    largestHoldingTicker: largestHolding?.ticker ?? null,
    largestHoldingPct: largestHolding?.percentAllocation ?? 0,
    largestSector: (largestSector?.label as PortfolioSector | undefined) ?? null,
    largestSectorExposurePct: largestSector?.percent ?? 0,
    etfPct: roundPct((etfValue / input.amount) * 100),
    stockPct: roundPct((stockValue / input.amount) * 100),
    cashPct: roundPct(((cashReserve + roundingCash) / input.amount) * 100),
    treasuryPct: roundPct((treasuryValue / input.amount) * 100),
    averageRiskScore: roundPct(averageRiskScore),
    concentrationScore: roundPct(Math.min(100, (largestHolding?.percentAllocation ?? 0) * 4 + (largestSector?.percent ?? 0))),
    diversificationScore: roundPct(Math.max(0, Math.min(100, holdings.length * 7 - (largestHolding?.percentAllocation ?? 0) * 1.5))),
    suggestedRebalanceFrequency: preferences.rebalanceFrequency,
    confidenceScore: roundPct(Math.max(45, Math.min(95, 100 - averageRiskScore * 7 - (largestHolding?.percentAllocation ?? 0) * 0.5))),
  };

  const result: PortfolioAllocationResult = {
    input: { ...input, preferences },
    marketData: {
      dataMode: marketData.dataMode,
      timestamp: marketData.timestamp,
      source: marketData.source,
      warnings: marketData.warnings,
    },
    candidateDiagnostics: marketData.candidates,
    candidateAudit,
    holdings,
    summary,
    sectorAllocations,
    assetTypeAllocations,
    riskBuckets,
    riskAnalysis: {
      concentrationRisk: "",
      sectorRisk: "",
      volatilityRisk: "",
      drawdownRisk: "",
      liquidityRisk: "",
      singleNameRisk: "",
      macroSensitivity: "",
      aiDataUncertainty: "",
      timeHorizonMismatchRisk: "",
    },
    optimization: buildHeuristicOptimizationDiagnostics(
      marketData.candidates,
      holdings,
      preferences,
      input.riskLevel,
      input.horizon,
    ),
    assumptions: buildAssumptions(input, preferences, selectedSectors),
    constraintsUsed: buildConstraints(preferences, input.horizon, input.riskLevel),
    warnings,
  };
  result.riskAnalysis = buildRiskAnalysis(result);
  assertFiniteResult(result);
  return result;
}

function buildHeuristicOptimizationDiagnostics(
  candidates: PortfolioHoldingCandidate[],
  holdings: PortfolioHolding[],
  preferences: PortfolioBuilderPreferences,
  riskLevel: PortfolioRiskLevel,
  horizon: PortfolioTimeHorizon,
): PortfolioOptimizationDiagnostics {
  const inputs = buildReturnInputs(candidates, riskLevel, horizon);
  const covariance = buildCovarianceMatrix(inputs);
  const constraints = {
    maxAllocationPerHoldingPct: preferences.maxAllocationPerHoldingPct,
    minAllocationPerHoldingPct: preferences.minAllocationPerHoldingPct,
    maxSectorConcentrationPct: preferences.maxSectorConcentrationPct,
    cashReservePct: preferences.cashReservePct,
  };
  const frontier = buildEfficientFrontier(inputs, covariance, constraints);
  const byTicker = new Map(inputs.map((input) => [input.ticker, input]));
  const weights = inputs.map((input) => holdings.find((holding) => holding.ticker === input.ticker)?.percentAllocation ?? 0);
  const expectedReturnPct = weights.reduce(
    (sum, weight, index) => sum + (weight / 100) * (inputs[index]?.expectedReturnPct ?? 0),
    0,
  );
  const annualVolatilityPct = weights.reduce(
    (sum, weight, index) => sum + (weight / 100) * (inputs[index]?.annualVolatilityPct ?? 0),
    0,
  );
  const warnings = holdings
    .filter((holding) => !byTicker.has(holding.ticker))
    .map((holding) => `${holding.ticker} did not have optimizer return diagnostics.`);
  const sharpeLikeScore = annualVolatilityPct > 0 ? expectedReturnPct / annualVolatilityPct : 0;
  return {
    method: "heuristic",
    feasible: holdings.length > 0,
    iterations: holdings.length,
    objectiveScore: roundPct(sharpeLikeScore * 100),
    expectedReturnPct: roundPct(expectedReturnPct),
    annualVolatilityPct: roundPct(annualVolatilityPct),
    sharpeLikeScore: roundPct(sharpeLikeScore),
    maxDrawdownProxyPct: roundPct(annualVolatilityPct * 1.85),
    cvarProxyPct: roundPct(annualVolatilityPct * 1.25),
    bindingConstraints: buildBindingConstraints(holdings, preferences),
    warnings,
    frontier: frontier.map((point) => ({
      targetReturnPct: point.targetReturnPct,
      expectedReturnPct: point.expectedReturnPct,
      annualVolatilityPct: point.annualVolatilityPct,
      feasible: point.feasible,
    })),
  };
}

function buildBindingConstraints(
  holdings: PortfolioHolding[],
  preferences: PortfolioBuilderPreferences,
): string[] {
  const constraints = new Set<string>();
  if (holdings.some((holding) => holding.targetWeightPct >= preferences.maxAllocationPerHoldingPct - 0.01)) {
    constraints.add("max_holding");
  }
  if (holdings.some((holding) => holding.targetWeightPct <= preferences.minAllocationPerHoldingPct + 0.01)) {
    constraints.add("min_holding");
  }
  const sectorTotals = new Map<PortfolioSector, number>();
  for (const holding of holdings) {
    sectorTotals.set(holding.sector, (sectorTotals.get(holding.sector) ?? 0) + holding.targetWeightPct);
  }
  if (Array.from(sectorTotals.values()).some((value) => value >= preferences.maxSectorConcentrationPct - 0.01)) {
    constraints.add("max_sector");
  }
  if (preferences.cashReservePct > 0) constraints.add("cash_reserve");
  return Array.from(constraints).sort();
}

function buildCandidateAudit(
  universe: PortfolioHoldingCandidate[],
  scored: ScoredCandidate[],
  holdings: PortfolioHolding[],
  preferences: PortfolioBuilderPreferences,
  selectedSectors: PortfolioSector[],
): PortfolioCandidateAudit {
  const selectedTickers = new Set(holdings.map((holding) => holding.ticker));
  const holdingSectors = new Map<PortfolioSector, number>();
  for (const holding of holdings) {
    holdingSectors.set(
      holding.sector,
      (holdingSectors.get(holding.sector) ?? 0) + holding.percentAllocation,
    );
  }

  const rows = scored.map((item): PortfolioCandidateAuditRow => {
    const selected = selectedTickers.has(item.candidate.ticker);
    return {
      ticker: item.candidate.ticker,
      name: item.candidate.name,
      sector: item.candidate.sector,
      theme: item.candidate.theme,
      assetType: item.candidate.assetType,
      score: roundPct(item.score),
      riskScore: item.candidate.riskScore,
      volatility: roundPct(item.candidate.volatility),
      dividendScore: item.candidate.dividendScore,
      selected,
      reason: selected
        ? "Selected after ranking, risk controls, sector caps, and share conversion."
        : rejectionReason(item.candidate, preferences, holdingSectors, selectedSectors),
    };
  });

  const eligibleCount = scored.length;
  return {
    universeCount: universe.length,
    eligibleCount,
    selectedCount: holdings.length,
    selectedSectors,
    topSelected: rows.filter((row) => row.selected).slice(0, 12),
    topRejected: rows.filter((row) => !row.selected).slice(0, 12),
    constraintNotes: [
      `Target holdings: ${preferences.targetHoldings}.`,
      `Max holding: ${preferences.maxAllocationPerHoldingPct.toFixed(1)}%.`,
      `Max sector: ${preferences.maxSectorConcentrationPct.toFixed(1)}%.`,
      `Intentional cash reserve: ${preferences.cashReservePct.toFixed(1)}%.`,
      "Any extra uninvested cash is whole-share rounding cash, not a model-selected reserve.",
      `Universe filter: ${selectedSectors.join(", ") || "broad default"}.`,
    ],
  };
}

function rejectionReason(
  candidate: PortfolioHoldingCandidate,
  preferences: PortfolioBuilderPreferences,
  holdingSectors: Map<PortfolioSector, number>,
  selectedSectors: PortfolioSector[],
): string {
  const sectorExposure = holdingSectors.get(candidate.sector) ?? 0;
  if (sectorExposure >= preferences.maxSectorConcentrationPct - 0.5) {
    return "Sector exposure was already near the cap.";
  }
  if (!selectedSectors.includes(candidate.sector) && candidate.sector !== "broad_etfs") {
    return "Lower fit versus the selected sector/theme universe.";
  }
  if (candidate.assetType === "stock" && !preferences.includeStocks) {
    return "Individual stocks were disabled.";
  }
  if (candidate.assetType === "etf" && !preferences.includeEtfs) {
    return "ETFs were disabled.";
  }
  if (candidate.riskScore > 4 && preferences.volatilityTolerance !== "high") {
    return "Higher risk than the selected volatility tolerance.";
  }
  return "Scored below the selected holdings after constraints and target count.";
}

function normalizePreferences(
  preferences: PortfolioBuilderPreferences,
  riskLevel: PortfolioRiskLevel,
  horizon: PortfolioTimeHorizon,
): PortfolioBuilderPreferences {
  const defaults = RISK_DEFAULTS[riskLevel];
  const horizonCaps = HORIZON_ADJUSTMENTS[horizon];
  return {
    ...preferences,
    targetHoldings: clamp(Math.round(preferences.targetHoldings || defaults.targetHoldings), 1, 30),
    maxAllocationPerHoldingPct: clamp(
      Math.min(preferences.maxAllocationPerHoldingPct || defaults.maxAllocationPerHoldingPct, defaults.maxAllocationPerHoldingPct * horizonCaps.concentrationMultiplier),
      1,
      60,
    ),
    minAllocationPerHoldingPct: clamp(preferences.minAllocationPerHoldingPct || defaults.minAllocationPerHoldingPct, 0, 25),
    maxSectorConcentrationPct: clamp(
      Math.min(preferences.maxSectorConcentrationPct || defaults.maxSectorConcentrationPct, defaults.maxSectorConcentrationPct * horizonCaps.concentrationMultiplier),
      5,
      80,
    ),
    cashReservePct: clamp(
      Number.isFinite(preferences.cashReservePct)
        ? preferences.cashReservePct
        : defaults.cashReservePct,
      0,
      80,
    ),
    rebalanceFrequency: preferences.rebalanceFrequency || suggestedRebalanceFrequency(riskLevel, horizon),
  };
}

function scoreCandidate(
  candidate: PortfolioHoldingCandidate,
  selectedSectors: PortfolioSector[],
  preferred: Set<string>,
  riskLevel: PortfolioRiskLevel,
  horizon: PortfolioTimeHorizon,
  preferences: PortfolioBuilderPreferences,
): number {
  const horizonAdj = HORIZON_ADJUSTMENTS[horizon];
  const sectorMatch = selectedSectors.includes(candidate.sector);
  let score = sectorMatch ? 100 : candidate.assetType === "etf" && candidate.sector === "broad_etfs" ? 72 : 12;

  if (preferred.has(candidate.ticker)) score += 70;
  if (candidate.assetType === "etf") score *= horizonAdj.etfBoost;
  if (candidate.assetType === "stock") score *= horizonAdj.stockBoost;
  if (candidate.assetType === "treasury") score *= horizonAdj.lowRiskBoost;

  if (riskLevel === "conservative") score *= Math.max(0.35, 1.55 - candidate.riskScore * 0.2);
  if (riskLevel === "balanced") score *= Math.max(0.55, 1.25 - candidate.riskScore * 0.08);
  if (riskLevel === "growth") score *= candidate.riskScore >= 2 ? 1.1 : 0.85;
  if (riskLevel === "aggressive_growth") score *= candidate.riskScore >= 3 ? 1.25 : 0.8;
  if (riskLevel === "speculative") score *= candidate.riskScore >= 3.5 ? 1.35 : 0.75;

  if (preferences.dividendPreference === "income") score *= 1 + candidate.dividendScore * 0.08;
  if (preferences.dividendPreference === "total_return" && candidate.dividendScore <= 1) score *= 1.08;
  if (preferences.volatilityTolerance === "low") score *= Math.max(0.45, 1.35 - candidate.volatility / 45);
  if (preferences.volatilityTolerance === "high") score *= 0.9 + candidate.volatility / 120;
  return score;
}

function ensurePreferred(scored: ScoredCandidate[], preferred: Set<string>, targetCount: number): ScoredCandidate[] {
  const selected = scored.slice(0, targetCount);
  for (const candidate of scored) {
    if (!preferred.has(candidate.candidate.ticker)) continue;
    if (selected.some((s) => s.candidate.ticker === candidate.candidate.ticker)) continue;
    const replaceIndex = selected.length - 1;
    if (replaceIndex >= 0) selected[replaceIndex] = candidate;
  }
  return selected;
}

function selectCandidatesForDeployment(
  scored: ScoredCandidate[],
  preferred: Set<string>,
  targetCount: number,
  preferences: PortfolioBuilderPreferences,
  selectedSectors: PortfolioSector[],
): CandidateSelection {
  const candidates = ensurePreferred(scored, preferred, targetCount);
  const selectedTickers = new Set(candidates.map((item) => item.candidate.ticker));
  let estimatedCapacityPct = estimateDeploymentCapacity(candidates, preferences, selectedSectors);

  for (const candidate of scored) {
    if (estimatedCapacityPct >= 99.5) break;
    if (selectedTickers.has(candidate.candidate.ticker)) continue;
    candidates.push(candidate);
    selectedTickers.add(candidate.candidate.ticker);
    estimatedCapacityPct = estimateDeploymentCapacity(candidates, preferences, selectedSectors);
  }

  const overflow = candidates.slice(targetCount);
  return {
    candidates,
    estimatedCapacityPct,
    expandedBeyondTarget: candidates.length > targetCount,
    expandedBeyondSelectedSectors: overflow.some((item) => !selectedSectors.includes(item.candidate.sector)),
  };
}

function estimateDeploymentCapacity(
  candidates: ScoredCandidate[],
  preferences: PortfolioBuilderPreferences,
  selectedSectors: PortfolioSector[],
): number {
  const sectorTotals = new Map<PortfolioSector, number>();
  let capacity = 0;
  for (const item of candidates) {
    const sector = item.candidate.sector;
    const sectorCap = selectedSectors.includes(sector)
      ? preferences.maxSectorConcentrationPct
      : Math.min(preferences.maxSectorConcentrationPct, 20);
    const used = sectorTotals.get(sector) ?? 0;
    const add = Math.min(
      preferences.maxAllocationPerHoldingPct,
      Math.max(0, sectorCap - used),
    );
    if (add <= 0) continue;
    sectorTotals.set(sector, used + add);
    capacity += add;
    if (capacity >= 100) return 100;
  }
  return roundPct(capacity);
}

function allocateWeights(
  selected: ScoredCandidate[],
  preferences: PortfolioBuilderPreferences,
  selectedSectors: PortfolioSector[],
): number[] {
  const sectorTotals = new Map<PortfolioSector, number>();
  const raw = selected.map((item) => Math.max(1, item.score));
  const rawTotal = raw.reduce((sum, v) => sum + v, 0);
  let weights = raw.map((v) => (v / rawTotal) * 100);
  weights = weights.map((w) => Math.min(w, preferences.maxAllocationPerHoldingPct));

  const sectorCap = preferences.maxSectorConcentrationPct;
  weights = weights.map((weight, index) => {
    const sector = selected[index].candidate.sector;
    const current = sectorTotals.get(sector) ?? 0;
    const remainingSector = selectedSectors.includes(sector) ? Math.max(0, sectorCap - current) : Math.max(0, Math.min(sectorCap, 20) - current);
    const capped = Math.min(weight, remainingSector);
    sectorTotals.set(sector, current + capped);
    return capped;
  });

  weights = redistribute(weights, selected, preferences);
  return weights.map((w) => (w < preferences.minAllocationPerHoldingPct ? 0 : w));
}

function redistribute(
  weights: number[],
  selected: ScoredCandidate[],
  preferences: PortfolioBuilderPreferences,
): number[] {
  let next = [...weights];
  for (let pass = 0; pass < 6; pass += 1) {
    const total = next.reduce((sum, w) => sum + w, 0);
    const shortfall = 100 - total;
    if (shortfall <= 0.01) break;
    const eligible = next
      .map((weight, index) => ({ index, weight, sector: selected[index].candidate.sector }))
      .filter(({ weight }) => weight > 0 && weight < preferences.maxAllocationPerHoldingPct - 0.01);
    if (!eligible.length) break;
    const sectorTotals = new Map<PortfolioSector, number>();
    for (let i = 0; i < next.length; i += 1) {
      sectorTotals.set(selected[i].candidate.sector, (sectorTotals.get(selected[i].candidate.sector) ?? 0) + next[i]);
    }
    const increment = shortfall / eligible.length;
    for (const item of eligible) {
      const sectorCurrent = sectorTotals.get(item.sector) ?? 0;
      const sectorRoom = Math.max(0, preferences.maxSectorConcentrationPct - sectorCurrent);
      const holdingRoom = Math.max(0, preferences.maxAllocationPerHoldingPct - next[item.index]);
      const add = Math.min(increment, sectorRoom, holdingRoom);
      next[item.index] += add;
      sectorTotals.set(item.sector, sectorCurrent + add);
    }
  }
  return next;
}

function buildHoldings(
  selected: ScoredCandidate[],
  weights: number[],
  startingAmount: number,
  investable: number,
  preferences: PortfolioBuilderPreferences,
): PortfolioHolding[] {
  return selected
    .map((item, index) => {
      const candidate = item.candidate;
      const targetDollars = investable * (weights[index] / 100);
      if (!candidate.price || targetDollars <= 0) return null;
      const shares = preferences.allowFractionalShares
        ? Math.floor((targetDollars / candidate.price) * 10000) / 10000
        : Math.floor(targetDollars / candidate.price);
      if (shares <= 0) return null;
      const dollarAllocation = roundMoney(shares * candidate.price);
      if (dollarAllocation <= 0) return null;
      return {
        ticker: candidate.ticker,
        name: candidate.name,
        sector: candidate.sector,
        theme: candidate.theme,
        assetType: candidate.assetType,
        price: candidate.price,
        dollarAllocation,
        percentAllocation: roundPct((dollarAllocation / startingAmount) * 100),
        targetWeightPct: roundPct(weights[index]),
        shares,
        fractional: preferences.allowFractionalShares,
        riskRating: riskRating(candidate.riskScore),
        riskScore: candidate.riskScore,
        role: candidate.role,
        reason: candidate.reason,
        keyRisk: candidate.keyRisk,
      };
    })
    .filter((holding): holding is PortfolioHolding => Boolean(holding));
}

function deployResidualCash(
  holdings: PortfolioHolding[],
  selected: ScoredCandidate[],
  investable: number,
  preferences: PortfolioBuilderPreferences,
  selectedSectors: PortfolioSector[],
): PortfolioHolding[] {
  if (preferences.allowFractionalShares || investable <= 0 || !holdings.length) return holdings;

  const next = holdings.map((holding) => ({ ...holding }));
  const byTicker = new Map(next.map((holding) => [holding.ticker, holding]));
  const maxHoldingDollars = investable * (preferences.maxAllocationPerHoldingPct / 100);
  const sectorCapDollars = (sector: PortfolioSector) =>
    investable *
    ((selectedSectors.includes(sector)
      ? preferences.maxSectorConcentrationPct
      : Math.min(preferences.maxSectorConcentrationPct, 20)) /
      100);

  for (let pass = 0; pass < 500; pass += 1) {
    const invested = roundMoney(next.reduce((sum, holding) => sum + holding.dollarAllocation, 0));
    const residual = roundMoney(Math.max(0, investable - invested));
    if (residual <= 0.01) break;

    const sectorDollars = new Map<PortfolioSector, number>();
    for (const holding of next) {
      sectorDollars.set(holding.sector, (sectorDollars.get(holding.sector) ?? 0) + holding.dollarAllocation);
    }

    let purchased = false;
    for (const item of selected) {
      const holding = byTicker.get(item.candidate.ticker);
      if (!holding || holding.price > residual + 0.01) continue;

      const holdingRoom = maxHoldingDollars - holding.dollarAllocation;
      const sectorRoom = sectorCapDollars(holding.sector) - (sectorDollars.get(holding.sector) ?? 0);
      if (holding.price > Math.min(holdingRoom, sectorRoom, residual) + 0.01) continue;

      holding.shares += 1;
      holding.dollarAllocation = roundMoney(holding.shares * holding.price);
      holding.targetWeightPct = roundPct((holding.dollarAllocation / investable) * 100);
      purchased = true;
      break;
    }

    if (!purchased) break;
  }

  return next;
}

function buckets<T extends string>(
  holdings: PortfolioHolding[],
  selector: (holding: PortfolioHolding) => T,
  totalValue: number,
  cashValue = 0,
  cashBucketLabel?: string,
): AllocationBucket[] {
  const totals = new Map<string, number>();
  for (const holding of holdings) {
    totals.set(selector(holding), (totals.get(selector(holding)) ?? 0) + holding.dollarAllocation);
  }
  if (cashValue > 0 && cashBucketLabel) {
    totals.set(cashBucketLabel, (totals.get(cashBucketLabel) ?? 0) + cashValue);
  }
  return Array.from(totals.entries())
    .map(([label, value]) => ({
      label,
      value: roundMoney(value),
      percent: totalValue > 0 ? roundPct((value / totalValue) * 100) : 0,
    }))
    .sort((a, b) => b.value - a.value);
}

function normalizeTickers(tickers: string[]): Set<string> {
  return new Set(
    tickers
      .map((t) => t.trim().toUpperCase())
      .filter(Boolean),
  );
}

function buildAssumptions(
  input: PortfolioBuilderInput,
  preferences: PortfolioBuilderPreferences,
  selectedSectors: PortfolioSector[],
): string[] {
  return [
    "This tool generates a proposed allocation only and does not execute trades.",
    "Prices are sourced from the configured market-data abstraction; demo mode is clearly labeled when active.",
    `Time horizon ${input.horizon} adjusted ETF, stock, volatility, and concentration preferences.`,
    `Selected sectors/themes: ${selectedSectors.join(", ")}.`,
    preferences.cashReservePct > 0
      ? `Intentional cash reserve is ${preferences.cashReservePct.toFixed(1)}% before share rounding.`
      : "No intentional cash reserve was requested; residual cash, if any, is only whole-share rounding cash.",
  ];
}

function buildConstraints(
  preferences: PortfolioBuilderPreferences,
  horizon: PortfolioTimeHorizon,
  risk: PortfolioRiskLevel,
): string[] {
  return [
    `Risk profile: ${risk}.`,
    `Time horizon: ${horizon}.`,
    `Target holdings: ${preferences.targetHoldings}.`,
    `Max holding allocation: ${preferences.maxAllocationPerHoldingPct.toFixed(1)}%.`,
    `Min holding allocation: ${preferences.minAllocationPerHoldingPct.toFixed(1)}%.`,
    `Max sector concentration: ${preferences.maxSectorConcentrationPct.toFixed(1)}%.`,
    `Intentional cash reserve: ${preferences.cashReservePct.toFixed(1)}%.`,
    `Fractional shares: ${preferences.allowFractionalShares ? "allowed" : "disabled"}.`,
    `Rebalance frequency: ${preferences.rebalanceFrequency}.`,
  ];
}

function assertFiniteResult(result: PortfolioAllocationResult): void {
  walk(result, "result");
}

function walk(value: unknown, path: string): void {
  if (typeof value === "number" && !Number.isFinite(value)) {
    throw new Error(`${path} is not finite.`);
  }
  if (!value || typeof value !== "object") return;
  for (const [key, nested] of Object.entries(value)) walk(nested, `${path}.${key}`);
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, safeNumber(value)));
}
