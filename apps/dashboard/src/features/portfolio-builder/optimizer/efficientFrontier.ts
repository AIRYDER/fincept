import { portfolioVolatilityPct } from "./covariance";
import { solveConstrainedWeights } from "./constraintSolver";
import type {
  CovarianceMatrix,
  EfficientFrontierPoint,
  OptimizationCandidatePortfolio,
  OptimizationConstraintSet,
  ReturnInput,
} from "./optimizerTypes";

export function optimizeMeanVariance(
  inputs: ReturnInput[],
  covariance: CovarianceMatrix,
  constraints: OptimizationConstraintSet,
): OptimizationCandidatePortfolio {
  const frontier = buildEfficientFrontier(inputs, covariance, constraints);
  const best = frontier
    .filter((point) => point.feasible)
    .sort((a, b) => scorePoint(b) - scorePoint(a))[0];
  const weights = best?.weights ?? inputs.map(() => 0);
  const expectedReturnPct = best?.expectedReturnPct ?? 0;
  const annualVolatilityPct = best?.annualVolatilityPct ?? 0;
  const sharpeLikeScore = annualVolatilityPct > 0 ? expectedReturnPct / annualVolatilityPct : 0;
  return {
    method: "mean_variance",
    weights,
    expectedReturnPct,
    annualVolatilityPct,
    sharpeLikeScore: round(sharpeLikeScore),
    maxDrawdownProxyPct: round(annualVolatilityPct * 1.9),
    cvarProxyPct: round(annualVolatilityPct * 1.3),
    diagnostics: {
      feasible: Boolean(best),
      iterations: frontier.length,
      bindingConstraints: [],
      warnings: best ? [] : ["No feasible frontier point was available."],
      objectiveScore: round(sharpeLikeScore * 100),
    },
  };
}

export function buildEfficientFrontier(
  inputs: ReturnInput[],
  covariance: CovarianceMatrix,
  constraints: OptimizationConstraintSet,
): EfficientFrontierPoint[] {
  if (!inputs.length) return [];
  const sorted = [...inputs].sort((a, b) => a.expectedReturnPct - b.expectedReturnPct);
  const minReturn = sorted[0].expectedReturnPct;
  const maxReturn = sorted[sorted.length - 1].expectedReturnPct;
  const steps = 7;
  const points: EfficientFrontierPoint[] = [];
  for (let step = 0; step < steps; step += 1) {
    const targetReturnPct = minReturn + ((maxReturn - minReturn) * step) / Math.max(1, steps - 1);
    const proposed = inputs.map((input) => {
      const returnFit = 1 / (1 + Math.abs(input.expectedReturnPct - targetReturnPct));
      const riskPenalty = 1 / Math.max(1, input.annualVolatilityPct);
      return returnFit * 0.7 + riskPenalty * 3;
    });
    const solved = solveConstrainedWeights(proposed, inputs, constraints);
    const expectedReturnPct = weightedAverage(solved.weights, inputs.map((input) => input.expectedReturnPct));
    points.push({
      targetReturnPct: round(targetReturnPct),
      expectedReturnPct: round(expectedReturnPct),
      annualVolatilityPct: portfolioVolatilityPct(solved.weights, covariance),
      weights: solved.weights,
      feasible: solved.diagnostics.feasible,
    });
  }
  return points.sort((a, b) => a.expectedReturnPct - b.expectedReturnPct);
}

function scorePoint(point: EfficientFrontierPoint): number {
  return point.annualVolatilityPct > 0 ? point.expectedReturnPct / point.annualVolatilityPct : 0;
}

function weightedAverage(weightsPct: number[], values: number[]): number {
  return weightsPct.reduce((sum, weight, index) => sum + (weight / 100) * (values[index] ?? 0), 0);
}

function round(value: number): number {
  return Math.round((Number.isFinite(value) ? value : 0) * 10000) / 10000;
}
