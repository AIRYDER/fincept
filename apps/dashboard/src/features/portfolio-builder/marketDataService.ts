import type {
  MarketDataSnapshot,
  PortfolioHoldingCandidate,
} from "./portfolioBuilder.types";
import { UnavailableLiveMarketDataProvider } from "./liveMarketDataClient";

export const DEMO_MARKET_DATA_SOURCE =
  "Demo market data snapshot. Prices are static placeholders until a live quote adapter is connected.";

export const DEMO_CANDIDATES: PortfolioHoldingCandidate[] = [
  asset("VTI", "Vanguard Total Stock Market ETF", "broad_etfs", "Core broad equity ETF", "etf", 258.42, 2.2, 1.0, 2, "Core market beta", "Broad low-cost US equity exposure.", "Equity market drawdown risk."),
  asset("SPY", "SPDR S&P 500 ETF Trust", "broad_etfs", "Large-cap index ETF", "etf", 512.88, 2.3, 1.0, 2, "Large-cap core", "Liquid S&P 500 exposure.", "Market-cap concentration in mega-cap technology."),
  asset("QQQ", "Invesco QQQ Trust", "broad_etfs", "Nasdaq 100 growth ETF", "etf", 438.12, 3.4, 1.25, 1, "Growth index sleeve", "Diversified growth and technology exposure.", "Higher valuation and duration sensitivity."),
  asset("SCHD", "Schwab US Dividend Equity ETF", "broad_etfs", "Dividend ETF", "etf", 78.36, 2.1, 0.9, 5, "Dividend quality sleeve", "Quality dividend exposure with lower single-name risk.", "Value factor can lag growth-led rallies."),
  asset("BND", "Vanguard Total Bond Market ETF", "cash_treasuries", "Bond ETF", "etf", 72.28, 1.1, 0.25, 3, "Portfolio ballast", "Investment-grade bond diversification.", "Rate sensitivity."),
  asset("SGOV", "iShares 0-3 Month Treasury Bond ETF", "cash_treasuries", "Short-term treasury ETF", "treasury", 100.42, 0.4, 0.05, 4, "Cash-like reserve", "T-bill exposure for dry powder and lower volatility.", "Yield can fall as policy rates decline."),
  asset("BIL", "SPDR Bloomberg 1-3 Month T-Bill ETF", "cash_treasuries", "T-bill ETF", "treasury", 91.68, 0.4, 0.05, 4, "Cash-like reserve", "Treasury bill exposure with high liquidity.", "Low upside in risk-on markets."),

  asset("NVDA", "NVIDIA", "ai_infrastructure", "AI accelerators", "stock", 199.53, 4.8, 1.7, 0, "AI compute leader", "Direct exposure to accelerated computing and AI infrastructure.", "Valuation, supply chain, and competitive cycle risk."),
  asset("MSFT", "Microsoft", "cloud_computing", "Cloud platform", "stock", 407.35, 3.0, 0.95, 2, "Cloud and AI platform", "Enterprise cloud, productivity, and AI platform exposure.", "Large-cap growth multiple compression."),
  asset("AMZN", "Amazon", "cloud_computing", "Cloud and consumer platform", "stock", 264.6, 3.5, 1.15, 0, "Cloud plus consumer growth", "AWS and retail operating leverage.", "Margin cyclicality and regulatory risk."),
  asset("GOOGL", "Alphabet", "ai_infrastructure", "AI and advertising platform", "stock", 385.77, 3.4, 1.08, 0, "AI data and ads", "Search, cloud, and AI model infrastructure.", "Advertising cycle and AI disruption risk."),
  asset("AVGO", "Broadcom", "semiconductors", "AI networking silicon", "stock", 1360.25, 3.8, 1.2, 3, "AI silicon infrastructure", "Networking and custom silicon exposure.", "Customer concentration and integration risk."),
  asset("AMD", "Advanced Micro Devices", "semiconductors", "CPU/GPU semiconductors", "stock", 153.57, 4.2, 1.6, 0, "AI and CPU challenger", "GPU and data-center semiconductor growth.", "Execution risk versus incumbents."),
  asset("TSM", "Taiwan Semiconductor", "semiconductors", "Semiconductor foundry", "stock", 146.2, 3.6, 1.1, 2, "Foundry backbone", "Critical global semiconductor manufacturing exposure.", "Geopolitical and cyclicality risk."),
  asset("SMH", "VanEck Semiconductor ETF", "semiconductors", "Semiconductor ETF", "etf", 242.8, 3.5, 1.25, 1, "Diversified semiconductor sleeve", "ETF exposure to the semiconductor value chain.", "Sector cyclicality and valuation risk."),
  asset("SOXX", "iShares Semiconductor ETF", "semiconductors", "Semiconductor ETF", "etf", 226.11, 3.4, 1.2, 1, "Semiconductor basket", "Diversified chip industry exposure.", "Drawdowns during inventory corrections."),
  asset("CRWD", "CrowdStrike", "cybersecurity", "Endpoint security", "stock", 339.44, 4.1, 1.4, 0, "Cybersecurity growth", "Cloud-native endpoint security exposure.", "Execution and valuation risk."),
  asset("PANW", "Palo Alto Networks", "cybersecurity", "Cybersecurity platform", "stock", 299.7, 3.7, 1.2, 0, "Security platform", "Broad enterprise cybersecurity platform exposure.", "Platform transition and competition risk."),
  asset("CIBR", "First Trust Nasdaq Cybersecurity ETF", "cybersecurity", "Cybersecurity ETF", "etf", 58.91, 3.0, 1.05, 1, "Cybersecurity basket", "Diversified exposure to security software and infrastructure.", "High software valuation sensitivity."),

  asset("XLE", "Energy Select Sector SPDR Fund", "energy", "Energy ETF", "etf", 91.33, 2.8, 0.8, 4, "Energy sector sleeve", "Diversified US energy exposure.", "Commodity price risk."),
  asset("XOM", "Exxon Mobil", "oil_gas", "Integrated oil and gas", "stock", 118.2, 2.7, 0.85, 4, "Oil and gas major", "Integrated energy cash-flow exposure.", "Oil price and transition risk."),
  asset("CVX", "Chevron", "oil_gas", "Integrated oil and gas", "stock", 162.4, 2.6, 0.85, 4, "Oil and gas major", "Dividend-oriented energy exposure.", "Commodity and reserve replacement risk."),
  asset("CCJ", "Cameco", "uranium", "Uranium producer", "stock", 52.18, 4.2, 1.35, 1, "Uranium beta", "Producer exposure to uranium demand.", "Commodity and permitting volatility."),
  asset("URA", "Global X Uranium ETF", "uranium", "Uranium ETF", "etf", 31.72, 4.0, 1.3, 1, "Uranium basket", "Diversified uranium and nuclear supply-chain exposure.", "Sharp commodity-cycle drawdowns."),
  asset("NLR", "VanEck Uranium and Nuclear ETF", "nuclear_energy", "Nuclear energy ETF", "etf", 86.25, 3.4, 1.0, 2, "Nuclear theme", "Nuclear infrastructure and uranium exposure.", "Policy and project timing risk."),
  asset("ICLN", "iShares Global Clean Energy ETF", "renewables", "Renewables ETF", "etf", 14.4, 3.5, 1.25, 1, "Renewables basket", "Diversified clean energy exposure.", "Rate sensitivity and policy risk."),
  asset("FSLR", "First Solar", "renewables", "Solar manufacturing", "stock", 205.1, 4.0, 1.45, 0, "Solar manufacturing", "US solar manufacturing and utility-scale demand.", "Policy, margin, and competition risk."),

  asset("LMT", "Lockheed Martin", "defense", "Defense prime", "stock", 469.4, 2.2, 0.65, 4, "Defense prime", "Defense budget and missile systems exposure.", "Program and budget risk."),
  asset("RTX", "RTX", "aerospace", "Aerospace and defense", "stock", 104.9, 2.4, 0.8, 3, "Aerospace defense", "Commercial aerospace and defense diversification.", "Execution and supply-chain risk."),
  asset("NOC", "Northrop Grumman", "defense", "Defense prime", "stock", 473.6, 2.3, 0.65, 3, "Defense systems", "Aerospace, defense, and space systems exposure.", "Contract and budget risk."),
  asset("ITA", "iShares US Aerospace & Defense ETF", "aerospace", "Aerospace and defense ETF", "etf", 132.2, 2.6, 0.8, 2, "Defense ETF", "Diversified aerospace and defense basket.", "Budget-cycle concentration risk."),

  asset("XLV", "Health Care Select Sector SPDR Fund", "healthcare", "Healthcare ETF", "etf", 146.1, 1.9, 0.65, 2, "Defensive sector ETF", "Healthcare diversification with defensive characteristics.", "Policy and reimbursement risk."),
  asset("UNH", "UnitedHealth Group", "healthcare", "Managed care", "stock", 490.55, 2.2, 0.75, 2, "Healthcare quality", "Managed care and healthcare services exposure.", "Regulatory and medical-cost risk."),
  asset("JNJ", "Johnson & Johnson", "healthcare", "Healthcare conglomerate", "stock", 151.8, 1.8, 0.55, 4, "Defensive healthcare", "Diversified healthcare and dividend stability.", "Litigation and pipeline risk."),
  asset("IBB", "iShares Biotechnology ETF", "biotech", "Biotech ETF", "etf", 134.2, 3.5, 1.15, 0, "Biotech basket", "Diversified biotech exposure.", "Clinical trial and rate sensitivity."),
  asset("XBI", "SPDR S&P Biotech ETF", "biotech", "Equal-weight biotech ETF", "etf", 92.6, 4.1, 1.45, 0, "Speculative biotech basket", "Broad high-beta biotech exposure.", "Financing and trial-result risk."),

  asset("XLF", "Financial Select Sector SPDR Fund", "financials", "Financials ETF", "etf", 42.88, 2.6, 1.0, 2, "Financials sector sleeve", "Diversified bank, insurer, and capital markets exposure.", "Credit-cycle risk."),
  asset("JPM", "JPMorgan Chase", "financials", "Money-center bank", "stock", 201.7, 2.5, 1.0, 3, "Quality bank", "Scale banking and capital markets exposure.", "Credit and regulatory risk."),
  asset("V", "Visa", "financials", "Payments network", "stock", 284.2, 2.4, 0.9, 1, "Payments compounder", "Global payments network with high margins.", "Consumer spending and regulation risk."),
  asset("XLY", "Consumer Discretionary Select Sector SPDR Fund", "consumer", "Consumer discretionary ETF", "etf", 181.1, 3.0, 1.2, 1, "Consumer growth sleeve", "Diversified consumer discretionary exposure.", "Consumer-cycle risk."),
  asset("COST", "Costco Wholesale", "consumer", "Retail compounder", "stock", 816.0, 2.7, 0.75, 1, "Quality consumer", "Membership retail model and defensive growth.", "Valuation and margin risk."),
  asset("PG", "Procter & Gamble", "consumer", "Consumer staples", "stock", 162.0, 1.6, 0.45, 4, "Defensive consumer", "Consumer staples stability and dividends.", "Input cost and FX risk."),
  asset("XLI", "Industrial Select Sector SPDR Fund", "industrials", "Industrials ETF", "etf", 121.7, 2.5, 0.9, 2, "Industrials sector sleeve", "Diversified cyclical industrial exposure.", "Economic cycle risk."),
  asset("CAT", "Caterpillar", "industrials", "Heavy machinery", "stock", 342.6, 3.0, 1.1, 3, "Industrial cycle leader", "Infrastructure and commodities equipment exposure.", "Cyclical demand risk."),
  asset("XLU", "Utilities Select Sector SPDR Fund", "utilities", "Utilities ETF", "etf", 69.34, 1.4, 0.35, 4, "Defensive utilities ETF", "Rate-sensitive defensive income exposure.", "Interest-rate sensitivity."),
  asset("NEE", "NextEra Energy", "utilities", "Utility and renewables", "stock", 71.5, 2.2, 0.7, 3, "Utility growth", "Regulated utility plus renewable development exposure.", "Rate and project execution risk."),
  asset("CASH", "Cash reserve", "cash_treasuries", "Cash", "cash", 1, 0.1, 0, 0, "Explicit cash", "Uninvested reserve for optionality and risk control.", "Purchasing-power risk."),
  asset("BROKEN", "Unavailable preferred ticker", "broad_etfs", "Unavailable quote", "stock", null, 5, 2, 0, "Unavailable", "This row exists only to test missing quote handling.", "No valid market price."),
];

export function getDemoMarketDataSnapshot(): MarketDataSnapshot {
  return {
    dataMode: "demo",
    timestamp: new Date().toISOString(),
    source: DEMO_MARKET_DATA_SOURCE,
    candidates: DEMO_CANDIDATES,
    warnings: ["Demo market data is active; live quote integration is not used for this allocation."],
  };
}

export async function getUnavailableLiveMarketDataSnapshot(): Promise<MarketDataSnapshot> {
  return new UnavailableLiveMarketDataProvider().getSnapshot();
}

function asset(
  ticker: string,
  name: string,
  sector: PortfolioHoldingCandidate["sector"],
  theme: string,
  assetType: PortfolioHoldingCandidate["assetType"],
  price: number | null,
  riskScore: number,
  beta: number,
  dividendScore: number,
  role: string,
  reason: string,
  keyRisk: string,
): PortfolioHoldingCandidate {
  return {
    ticker,
    name,
    sector,
    theme,
    assetType,
    price,
    riskScore,
    beta,
    dividendScore,
    liquidityScore: assetType === "etf" || assetType === "treasury" ? 5 : 4,
    volatility: Math.max(2, riskScore * 8 + beta * 6),
    role,
    reason,
    keyRisk,
  };
}
