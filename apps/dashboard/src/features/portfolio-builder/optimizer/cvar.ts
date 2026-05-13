import type { OptimizationCandidatePortfolio, ReturnInput } from "./optimizerTypes";

export function estimateCvarProxy(
  weightsPct: number[],
  inputs: ReturnInput[],
): Pick<OptimizationCandidatePortfolio, "maxDrawdownProxyPct" | "cvarProxyPct"> & { warnings: string[] } {
  const weightedVol = weightsPct.reduce(
    (sum, weight, index) => sum + (weight / 100) * (inputs[index]?.annualVolatilityPct ?? 0),
    0,
  );
  return {
    maxDrawdownProxyPct: round(weightedVol * 2.05),
    cvarProxyPct: round(weightedVol * 1.42),
    warnings: ["CVaR uses a volatility proxy until historical return scenarios are connected."],
  };
}

function round(value: number): number {
  return Math.round((Number.isFinite(value) ? value : 0) * 10000) / 10000;
}
