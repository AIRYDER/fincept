import type {
  PortfolioAssetType,
  PortfolioSector,
} from "../portfolioBuilder.types";

export type StressRegimeId =
  | "rates_shock"
  | "liquidity_crisis"
  | "ai_multiple_compression"
  | "energy_spike"
  | "crypto_crash"
  | "recession"
  | "melt_up"
  | "soft_landing"
  | "fed_pivot"
  | "ai_capex_acceleration"
  | "productivity_boom"
  | "earnings_recession"
  | "stagflation"
  | "dollar_spike"
  | "geopolitical_supply_shock";

export type StressSeverity = "mild" | "base" | "severe";
export type StressRegimePolarity = "upside" | "downside" | "mixed";

export interface StressRegime {
  id: StressRegimeId;
  label: string;
  description: string;
  defaultSeverity: StressSeverity;
  polarity: StressRegimePolarity;
  category: string;
  sectorShockPct: Partial<Record<PortfolioSector, number>>;
  assetTypeShockPct: Partial<Record<PortfolioAssetType, number>>;
  betaShockMultiplier: number;
  volatilityShockMultiplier: number;
  liquidityHaircutPct: number;
  warnings: string[];
}

export interface StressHoldingResult {
  ticker: string;
  name: string;
  sector: PortfolioSector;
  assetType: PortfolioAssetType;
  startingValue: number;
  stressedValue: number;
  pnlDelta: number;
  pnlDeltaPct: number;
  contributionPct: number;
  appliedShockPct: number;
  explanation: string;
}

export interface StressGuardrailBreach {
  id: "drawdown" | "single_name" | "sector" | "liquidity" | "cash_drag";
  severity: "info" | "warn" | "critical";
  message: string;
}

export interface StressResult {
  regimeId: StressRegimeId;
  severity: StressSeverity;
  startingValue: number;
  stressedValue: number;
  pnlDelta: number;
  pnlDeltaPct: number;
  worstContributors: StressHoldingResult[];
  bestContributors: StressHoldingResult[];
  holdings: StressHoldingResult[];
  guardrailBreaches: StressGuardrailBreach[];
  warnings: string[];
}

export interface RunPortfolioStressOptions {
  regimeId: StressRegimeId;
  severity?: StressSeverity;
}
