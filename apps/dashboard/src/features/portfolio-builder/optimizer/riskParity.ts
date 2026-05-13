import { portfolioVolatilityPct } from "./covariance";
import { solveConstrainedWeights } from "./constraintSolver";
import type {
  CovarianceMatrix,
  OptimizationCandidatePortfolio,
  OptimizationConstraintSet,
  ReturnInput,
} from "./optimizerTypes";

export function optimizeInverseVolatility(
  inputs: ReturnInput[],
  constraints: OptimizationConstraintSet,
): OptimizationCandidatePortfolio {
  const proposed = inputs.map((input) => 1 / Math.max(1, input.annualVolatilityPct));
  return buildPortfolio("inverse_volatility", proposed, inputs, constraints);
}

export function optimizeRiskParity(
  inputs: ReturnInput[],
  covariance: CovarianceMatrix,
  constraints: OptimizationConstraintSet,
): OptimizationCandidatePortfolio {
  const proposed = inputs.map((input, index) => {
    const variance = covariance.values[index]?.[index] ?? 0;
    const sectorDiversifier = 1 + inputs.filter((candidate) => candidate.sector === input.sector).length * 0.08;
    return 1 / Math.max(0.0001, Math.sqrt(variance) * sectorDiversifier);
  });
  return buildPortfolio("risk_parity", proposed, inputs, constraints, covariance);
}

function buildPortfolio(
  method: "inverse_volatility" | "risk_parity",
  proposed: number[],
  inputs: ReturnInput[],
  constraints: OptimizationConstraintSet,
  covariance?: CovarianceMatrix,
): OptimizationCandidatePortfolio {
  const solved = solveConstrainedWeights(proposed, inputs, constraints);
  const annualVolatilityPct = covariance
    ? portfolioVolatilityPct(solved.weights, covariance)
    : weightedAverage(solved.weights, inputs.map((input) => input.annualVolatilityPct));
  const expectedReturnPct = weightedAverage(solved.weights, inputs.map((input) => input.expectedReturnPct));
  const sharpeLikeScore = annualVolatilityPct > 0 ? expectedReturnPct / annualVolatilityPct : 0;
  return {
    method,
    weights: solved.weights,
    expectedReturnPct: round(expectedReturnPct),
    annualVolatilityPct: round(annualVolatilityPct),
    sharpeLikeScore: round(sharpeLikeScore),
    maxDrawdownProxyPct: round(annualVolatilityPct * 1.85),
    cvarProxyPct: round(annualVolatilityPct * 1.25),
    diagnostics: {
      ...solved.diagnostics,
      objectiveScore: round(sharpeLikeScore * 100),
    },
  };
}

function weightedAverage(weightsPct: number[], values: number[]): number {
  return weightsPct.reduce((sum, weight, index) => sum + (weight / 100) * (values[index] ?? 0), 0);
}

function round(value: number): number {
  return Math.round((Number.isFinite(value) ? value : 0) * 10000) / 10000;
}
