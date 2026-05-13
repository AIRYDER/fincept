import type {
  PortfolioAllocationResult,
  PortfolioHoldingCandidate,
} from "../portfolioBuilder.types";
import { getStressRegime } from "./regimeCatalog";
import type {
  RunPortfolioStressOptions,
  StressHoldingResult,
  StressResult,
  StressSeverity,
} from "./warRoomTypes";
import { evaluateStressGuardrails } from "./guardrails";

const SEVERITY_MULTIPLIER: Record<StressSeverity, number> = {
  mild: 0.5,
  base: 1,
  severe: 1.5,
};

export function runPortfolioStress(
  allocation: PortfolioAllocationResult,
  options: RunPortfolioStressOptions,
): StressResult {
  const regime = getStressRegime(options.regimeId);
  const severity = options.severity ?? regime.defaultSeverity;
  const warnings = [...regime.warnings];
  const diagnostics = new Map(
    allocation.candidateDiagnostics.map((candidate) => [candidate.ticker, candidate]),
  );

  const holdings = allocation.holdings.map((holding): StressHoldingResult => {
    const diagnostic = diagnostics.get(holding.ticker);
    if (!diagnostic) {
      warnings.push(`${holding.ticker} is missing candidate diagnostics; beta and volatility adjustments used defaults.`);
    }
    const shockPct = calculateShockPct(
      holding.sector,
      holding.assetType,
      diagnostic,
      regime,
      severity,
    );
    const stressedValue = roundMoney(holding.dollarAllocation * (1 + shockPct / 100));
    const pnlDelta = roundMoney(stressedValue - holding.dollarAllocation);
    return {
      ticker: holding.ticker,
      name: holding.name,
      sector: holding.sector,
      assetType: holding.assetType,
      startingValue: roundMoney(holding.dollarAllocation),
      stressedValue,
      pnlDelta,
      pnlDeltaPct: holding.dollarAllocation > 0 ? roundPct((pnlDelta / holding.dollarAllocation) * 100) : 0,
      contributionPct: 0,
      appliedShockPct: shockPct,
      explanation: explainShock(shockPct, diagnostic),
    };
  });

  const startingValue = roundMoney(holdings.reduce((sum, holding) => sum + holding.startingValue, 0) + allocation.summary.totalCash);
  const stressedHoldingsValue = holdings.reduce((sum, holding) => sum + holding.stressedValue, 0);
  const stressedValue = roundMoney(stressedHoldingsValue + allocation.summary.totalCash);
  const pnlDelta = roundMoney(stressedValue - startingValue);
  const pnlDeltaPct = startingValue > 0 ? roundPct((pnlDelta / startingValue) * 100) : 0;
  const totalAbsDelta = holdings.reduce((sum, holding) => sum + Math.abs(holding.pnlDelta), 0);
  const holdingsWithContribution = holdings.map((holding) => ({
    ...holding,
    contributionPct: totalAbsDelta > 0 ? roundPct((Math.abs(holding.pnlDelta) / totalAbsDelta) * 100) : 0,
  }));

  const resultWithoutGuardrails: Omit<StressResult, "guardrailBreaches"> = {
    regimeId: regime.id,
    severity,
    startingValue,
    stressedValue,
    pnlDelta,
    pnlDeltaPct,
    worstContributors: [...holdingsWithContribution]
      .sort((a, b) => a.pnlDelta - b.pnlDelta)
      .slice(0, 5),
    bestContributors: [...holdingsWithContribution]
      .sort((a, b) => b.pnlDelta - a.pnlDelta)
      .slice(0, 5),
    holdings: holdingsWithContribution,
    warnings: unique(warnings),
  };
  return {
    ...resultWithoutGuardrails,
    guardrailBreaches: evaluateStressGuardrails(allocation, resultWithoutGuardrails, regime),
  };
}

function calculateShockPct(
  sector: StressHoldingResult["sector"],
  assetType: StressHoldingResult["assetType"],
  diagnostic: PortfolioHoldingCandidate | undefined,
  regime: ReturnType<typeof getStressRegime>,
  severity: StressSeverity,
): number {
  const sectorShock = regime.sectorShockPct[sector] ?? 0;
  const assetShock = regime.assetTypeShockPct[assetType] ?? 0;
  const beta = diagnostic?.beta ?? 1;
  const volatility = diagnostic?.volatility ?? 20;
  const betaShock = (beta - 1) * regime.betaShockMultiplier;
  const severeVolShock = severity === "severe" ? volatility * regime.volatilityShockMultiplier : 0;
  const liquidityShock = assetType === "stock" ? -regime.liquidityHaircutPct : 0;
  const rawShock = (sectorShock + assetShock + betaShock + severeVolShock + liquidityShock) * SEVERITY_MULTIPLIER[severity];
  return roundPct(clamp(rawShock, -95, 150));
}

function explainShock(shockPct: number, diagnostic: PortfolioHoldingCandidate | undefined): string {
  const direction = shockPct > 0 ? "positive" : shockPct < 0 ? "negative" : "flat";
  const beta = diagnostic?.beta ?? 1;
  return `${direction} stress from regime, sector, asset-type, beta ${beta.toFixed(2)}, and volatility assumptions.`;
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values));
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, Number.isFinite(value) ? value : 0));
}

function roundMoney(value: number): number {
  return Math.round((Number.isFinite(value) ? value : 0) * 100) / 100;
}

function roundPct(value: number): number {
  return Math.round((Number.isFinite(value) ? value : 0) * 100) / 100;
}
