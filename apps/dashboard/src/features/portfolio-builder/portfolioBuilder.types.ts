export type PortfolioTimeHorizon =
  | "3m"
  | "6m"
  | "1y"
  | "3y"
  | "5y"
  | "10y_plus"
  | "custom";

export type PortfolioRiskLevel =
  | "conservative"
  | "balanced"
  | "growth"
  | "aggressive_growth"
  | "speculative";

export type PortfolioSector =
  | "semiconductors"
  | "ai_infrastructure"
  | "cloud_computing"
  | "cybersecurity"
  | "energy"
  | "nuclear_energy"
  | "uranium"
  | "oil_gas"
  | "renewables"
  | "defense"
  | "aerospace"
  | "healthcare"
  | "biotech"
  | "financials"
  | "consumer"
  | "industrials"
  | "utilities"
  | "broad_etfs"
  | "cash_treasuries";

export type PortfolioModelProvider = "auto" | "openai" | "anthropic";
export type PortfolioAssetType = "stock" | "etf" | "cash" | "treasury";
export type DividendPreference = "neutral" | "income" | "total_return";
export type VolatilityTolerance = "low" | "medium" | "high";
export type RebalanceFrequency = "monthly" | "quarterly" | "semiannual" | "annual";
export type MarketDataMode = "live" | "demo";
export type OptimizationMethod =
  | "heuristic"
  | "inverse_volatility"
  | "risk_parity"
  | "mean_variance"
  | "black_litterman"
  | "cvar_min_drawdown";

export interface PortfolioBuilderPreferences {
  targetHoldings: number;
  maxAllocationPerHoldingPct: number;
  minAllocationPerHoldingPct: number;
  maxSectorConcentrationPct: number;
  includeEtfs: boolean;
  includeStocks: boolean;
  allowFractionalShares: boolean;
  cashReservePct: number;
  preferredTickers: string[];
  excludedTickers: string[];
  dividendPreference: DividendPreference;
  volatilityTolerance: VolatilityTolerance;
  rebalanceFrequency: RebalanceFrequency;
}

export interface PortfolioBuilderInput {
  amount: number;
  horizon: PortfolioTimeHorizon;
  customHorizonLabel?: string;
  riskLevel: PortfolioRiskLevel;
  sectors: PortfolioSector[];
  researchInstructions?: string;
  preferences: PortfolioBuilderPreferences;
  modelProvider: PortfolioModelProvider;
}

export interface PortfolioHoldingCandidate {
  ticker: string;
  name: string;
  sector: PortfolioSector;
  theme: string;
  assetType: PortfolioAssetType;
  price: number | null;
  riskScore: number;
  beta: number;
  dividendScore: number;
  liquidityScore: number;
  volatility: number;
  role: string;
  reason: string;
  keyRisk: string;
}

export interface MarketDataSnapshot {
  dataMode: MarketDataMode;
  timestamp: string;
  source: string;
  candidates: PortfolioHoldingCandidate[];
  warnings: string[];
}

export interface PortfolioHolding {
  ticker: string;
  name: string;
  sector: PortfolioSector;
  theme: string;
  assetType: PortfolioAssetType;
  price: number;
  dollarAllocation: number;
  percentAllocation: number;
  targetWeightPct: number;
  shares: number;
  fractional: boolean;
  riskRating: "Low" | "Medium" | "High" | "Speculative";
  riskScore: number;
  role: string;
  reason: string;
  keyRisk: string;
}

export interface PortfolioRiskAnalysis {
  concentrationRisk: string;
  sectorRisk: string;
  volatilityRisk: string;
  drawdownRisk: string;
  liquidityRisk: string;
  singleNameRisk: string;
  macroSensitivity: string;
  aiDataUncertainty: string;
  timeHorizonMismatchRisk: string;
}

export interface PortfolioSummaryMetrics {
  startingAmount: number;
  totalInvested: number;
  cashReserve: number;
  roundingCash: number;
  totalCash: number;
  totalValue: number;
  cashPercent: number;
  numberOfPositions: number;
  largestHoldingTicker: string | null;
  largestHoldingPct: number;
  largestSector: PortfolioSector | null;
  largestSectorExposurePct: number;
  etfPct: number;
  stockPct: number;
  cashPct: number;
  treasuryPct: number;
  averageRiskScore: number;
  concentrationScore: number;
  diversificationScore: number;
  suggestedRebalanceFrequency: RebalanceFrequency;
  confidenceScore: number;
}

export interface AllocationBucket {
  label: string;
  value: number;
  percent: number;
}

export interface PortfolioCandidateAuditRow {
  ticker: string;
  name: string;
  sector: PortfolioSector;
  theme: string;
  assetType: PortfolioAssetType;
  score: number;
  riskScore: number;
  volatility: number;
  dividendScore: number;
  selected: boolean;
  reason: string;
}

export interface PortfolioCandidateAudit {
  universeCount: number;
  eligibleCount: number;
  selectedCount: number;
  selectedSectors: PortfolioSector[];
  topSelected: PortfolioCandidateAuditRow[];
  topRejected: PortfolioCandidateAuditRow[];
  constraintNotes: string[];
}

export interface PortfolioAllocationResult {
  input: PortfolioBuilderInput;
  marketData: Omit<MarketDataSnapshot, "candidates">;
  candidateDiagnostics: PortfolioHoldingCandidate[];
  candidateAudit: PortfolioCandidateAudit;
  holdings: PortfolioHolding[];
  summary: PortfolioSummaryMetrics;
  sectorAllocations: AllocationBucket[];
  assetTypeAllocations: AllocationBucket[];
  riskBuckets: AllocationBucket[];
  riskAnalysis: PortfolioRiskAnalysis;
  optimization: PortfolioOptimizationDiagnostics;
  assumptions: string[];
  constraintsUsed: string[];
  warnings: string[];
}

export interface PortfolioOptimizationDiagnostics {
  method: OptimizationMethod;
  feasible: boolean;
  iterations: number;
  objectiveScore: number;
  expectedReturnPct: number;
  annualVolatilityPct: number;
  sharpeLikeScore: number;
  maxDrawdownProxyPct: number;
  cvarProxyPct: number;
  bindingConstraints: string[];
  warnings: string[];
  frontier?: Array<{
    targetReturnPct: number;
    expectedReturnPct: number;
    annualVolatilityPct: number;
    feasible: boolean;
  }>;
}

export interface PortfolioReportLLMResponse {
  executiveSummary: string;
  portfolioReasoning: string;
  optimalityReview: string;
  universeReview: string;
  holdingRationales: Record<string, string>;
  timeHorizonExplanation: string;
  riskAnalysis: string;
  rebalancingPlan: string;
  researchMandate: string[];
  agentDebate: Array<{
    agent: string;
    finding: string;
  }>;
  assumptionsAndLimitations: string[];
  providerLabel: string;
  generatedAt: string;
  fallbackUsed?: boolean;
  providerDiagnostics?: string[];
}
