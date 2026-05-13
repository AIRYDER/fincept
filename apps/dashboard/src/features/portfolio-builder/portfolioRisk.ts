import type {
  PortfolioAllocationResult,
  PortfolioHolding,
  PortfolioRiskAnalysis,
  PortfolioRiskLevel,
  PortfolioTimeHorizon,
  RebalanceFrequency,
} from "./portfolioBuilder.types";

export function riskRating(score: number): PortfolioHolding["riskRating"] {
  if (score < 1.5) return "Low";
  if (score < 3) return "Medium";
  if (score < 4.3) return "High";
  return "Speculative";
}

export function suggestedRebalanceFrequency(
  riskLevel: PortfolioRiskLevel,
  horizon: PortfolioTimeHorizon,
): RebalanceFrequency {
  if (riskLevel === "speculative" || riskLevel === "aggressive_growth") return "monthly";
  if (horizon === "3m" || horizon === "6m") return "monthly";
  if (riskLevel === "conservative") return "semiannual";
  return "quarterly";
}

export function buildRiskAnalysis(result: Pick<PortfolioAllocationResult, "holdings" | "summary" | "input">): PortfolioRiskAnalysis {
  const largest = result.summary.largestHoldingPct;
  const sector = result.summary.largestSectorExposurePct;
  const avgRisk = result.summary.averageRiskScore;
  const horizon = result.input.horizon;
  const risk = result.input.riskLevel;
  const hasIntentionalCashReserve = result.summary.cashReserve > 0.01;

  return {
    concentrationRisk:
      largest > 15
        ? `Largest holding is ${largest.toFixed(1)}%, so single-position drawdowns can visibly move the account.`
        : "Position sizing is distributed enough that no single holding dominates the plan.",
    sectorRisk:
      sector > 35
        ? `Largest sector exposure is ${sector.toFixed(1)}%, which should be monitored against the stated sector cap.`
        : "Sector exposure is diversified relative to the selected themes and cap.",
    volatilityRisk:
      avgRisk >= 3.5
        ? "The portfolio carries elevated volatility from growth, thematic, or single-name exposure."
        : "Expected volatility is moderated by ETF, treasury, cash, or defensive exposures.",
    drawdownRisk:
      risk === "speculative" || risk === "aggressive_growth"
        ? "Stress drawdowns can be large; rebalance discipline and sizing limits matter more than headline upside."
        : hasIntentionalCashReserve
          ? "Drawdown risk is controlled through diversification, the requested intentional cash reserve, and position caps."
          : "Drawdown risk is controlled through diversification and position caps; any residual cash is only whole-share rounding.",
    liquidityRisk: "Selected instruments are primarily liquid US listed stocks and ETFs in this demo universe.",
    singleNameRisk:
      result.holdings.filter((h) => h.assetType === "stock").length > result.holdings.length / 2
        ? "Single-name exposure is meaningful; earnings gaps and company-specific news can drive returns."
        : "ETF exposure reduces company-specific event risk.",
    macroSensitivity:
      horizon === "3m" || horizon === "6m"
        ? "Short horizons are more sensitive to rates, liquidity, and market timing than long-term fundamentals."
        : "Longer horizon allows more tolerance for equity-cycle volatility and growth-factor dispersion.",
    aiDataUncertainty:
      "The AI layer explains the deterministic output; it does not set weights, prices, or share counts.",
    timeHorizonMismatchRisk:
      (horizon === "3m" || horizon === "6m") && (risk === "aggressive_growth" || risk === "speculative")
        ? "Aggressive risk on a short horizon can conflict with capital preservation; review before acting."
        : "Risk level and horizon are broadly consistent with the allocation constraints used.",
  };
}

export function bucketRisk(holdings: PortfolioHolding[]) {
  const totals = new Map<string, number>();
  for (const holding of holdings) {
    totals.set(holding.riskRating, (totals.get(holding.riskRating) ?? 0) + holding.dollarAllocation);
  }
  const total = holdings.reduce((sum, h) => sum + h.dollarAllocation, 0);
  return Array.from(totals.entries()).map(([label, value]) => ({
    label,
    value: roundMoney(value),
    percent: total > 0 ? roundPct((value / total) * 100) : 0,
  }));
}

export function roundMoney(value: number): number {
  return Math.round(safeNumber(value) * 100) / 100;
}

export function roundPct(value: number): number {
  return Math.round(safeNumber(value) * 100) / 100;
}

export function safeNumber(value: number): number {
  return Number.isFinite(value) ? value : 0;
}
