import type {
  OptimizationMethod,
  PortfolioSector,
} from "../portfolioBuilder.types";

export interface ReturnInput {
  ticker: string;
  sector: PortfolioSector;
  expectedReturnPct: number;
  annualVolatilityPct: number;
  beta: number;
  riskScore: number;
  confidence: number;
  source: string;
  warnings: string[];
}

export interface CovarianceMatrix {
  tickers: string[];
  values: number[][];
}

export interface OptimizationConstraintSet {
  maxAllocationPerHoldingPct: number;
  minAllocationPerHoldingPct: number;
  maxSectorConcentrationPct: number;
  cashReservePct: number;
}

export interface WeightSolverDiagnostics {
  feasible: boolean;
  iterations: number;
  bindingConstraints: string[];
  warnings: string[];
}

export interface WeightSolverResult {
  weights: number[];
  totalWeightPct: number;
  feasible: boolean;
  diagnostics: WeightSolverDiagnostics;
}

export interface OptimizationCandidatePortfolio {
  method: OptimizationMethod;
  weights: number[];
  expectedReturnPct: number;
  annualVolatilityPct: number;
  sharpeLikeScore: number;
  maxDrawdownProxyPct: number;
  cvarProxyPct: number;
  diagnostics: WeightSolverDiagnostics & {
    objectiveScore: number;
  };
}

export interface EfficientFrontierPoint {
  targetReturnPct: number;
  expectedReturnPct: number;
  annualVolatilityPct: number;
  weights: number[];
  feasible: boolean;
}
