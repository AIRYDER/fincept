import type {
  PortfolioHoldingCandidate,
  PortfolioRiskLevel,
  PortfolioTimeHorizon,
} from "../portfolioBuilder.types";
import type { ReturnInput } from "./optimizerTypes";

const RISK_RETURN_PREMIUM: Record<PortfolioRiskLevel, number> = {
  conservative: 0.7,
  balanced: 1,
  growth: 1.2,
  aggressive_growth: 1.4,
  speculative: 1.6,
};

const HORIZON_RETURN_MULTIPLIER: Record<PortfolioTimeHorizon, number> = {
  "3m": 0.72,
  "6m": 0.82,
  "1y": 1,
  "3y": 1.08,
  "5y": 1.16,
  "10y_plus": 1.24,
  custom: 1,
};

export function buildReturnInputs(
  candidates: PortfolioHoldingCandidate[],
  riskLevel: PortfolioRiskLevel,
  horizon: PortfolioTimeHorizon,
): ReturnInput[] {
  return candidates
    .filter((candidate) => candidate.assetType !== "cash")
    .filter((candidate) => Number.isFinite(candidate.price) && (candidate.price ?? 0) > 0)
    .map((candidate) => {
      const warnings: string[] = [];
      const annualVolatilityPct = clamp(candidate.volatility || candidate.riskScore * 10, 2, 85);
      const expectedReturnPct = estimateExpectedReturn(candidate, riskLevel, horizon);
      if (candidate.assetType === "treasury" && expectedReturnPct > 6) {
        warnings.push("Treasury expected return was capped by conservative estimator.");
      }
      return {
        ticker: candidate.ticker,
        sector: candidate.sector,
        expectedReturnPct,
        annualVolatilityPct,
        beta: finite(candidate.beta, 1),
        riskScore: finite(candidate.riskScore, 3),
        confidence: clamp((candidate.liquidityScore / 5) * (candidate.assetType === "etf" ? 0.95 : 0.82), 0, 1),
        source: "demo_candidate_estimator",
        warnings,
      };
    })
    .sort((a, b) => a.ticker.localeCompare(b.ticker));
}

function estimateExpectedReturn(
  candidate: PortfolioHoldingCandidate,
  riskLevel: PortfolioRiskLevel,
  horizon: PortfolioTimeHorizon,
): number {
  const baseByAsset = candidate.assetType === "treasury" ? 4.2 : candidate.assetType === "etf" ? 7.2 : 8.4;
  const riskPremium = candidate.riskScore * RISK_RETURN_PREMIUM[riskLevel];
  const dividendPremium = candidate.dividendScore * 0.25;
  const betaAdjustment = (candidate.beta - 1) * 1.1;
  const estimate = (baseByAsset + riskPremium + dividendPremium + betaAdjustment) * HORIZON_RETURN_MULTIPLIER[horizon];
  return Math.round(clamp(estimate, 0.5, candidate.assetType === "treasury" ? 6 : 28) * 100) / 100;
}

function finite(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, finite(value, min)));
}
