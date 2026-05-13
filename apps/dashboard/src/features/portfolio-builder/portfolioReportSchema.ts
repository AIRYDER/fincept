import type {
  PortfolioAllocationResult,
  PortfolioReportLLMResponse,
} from "./portfolioBuilder.types";

export function fallbackReport(
  allocation: PortfolioAllocationResult,
  providerLabel = "Deterministic local report",
  providerDiagnostics: string[] = [],
): PortfolioReportLLMResponse {
  const risk = allocation.input.riskLevel.replace(/_/g, " ");
  const horizon = allocation.input.horizon.replace(/_/g, " ");
  const intentionalCashPct = allocation.summary.startingAmount > 0
    ? (allocation.summary.cashReserve / allocation.summary.startingAmount) * 100
    : 0;
  return {
    executiveSummary: `A ${risk} portfolio was built for a ${horizon} horizon using deterministic allocation constraints. The plan invests ${allocation.summary.numberOfPositions} positions, uses an intentional cash reserve of ${intentionalCashPct.toFixed(1)}%, leaves ${allocation.summary.cashPercent.toFixed(1)}% total uninvested cash after whole-share rounding, and caps the largest holding at ${allocation.summary.largestHoldingPct.toFixed(1)}%.`,
    portfolioReasoning:
      "The engine prioritized selected themes, diversified ETF exposure, risk controls, and the user-selected intentional cash reserve before converting allocations into shares. Residual uninvested cash can still appear when fractional shares are disabled; that is rounding cash, not a discretionary reserve. The language layer audits the full scored universe but does not change weights or share counts after the deterministic optimizer locks the packet.",
    optimalityReview: buildFallbackOptimalityReview(allocation),
    universeReview: buildFallbackUniverseReview(allocation),
    holdingRationales: Object.fromEntries(
      allocation.holdings.map((holding) => [
        holding.ticker,
        `${holding.role}: ${holding.reason}`,
      ]),
    ),
    timeHorizonExplanation:
      allocation.input.horizon === "3m" || allocation.input.horizon === "6m"
        ? "The short horizon raised the weight of ETFs, treasuries, diversification, and lower volatility holdings while reducing single-stock concentration."
        : "The longer horizon allows more equity and growth exposure while still applying caps, sector limits, and rebalancing discipline.",
    riskAnalysis: Object.values(allocation.riskAnalysis).join(" "),
    rebalancingPlan: `Review ${allocation.summary.suggestedRebalanceFrequency}. Rebalance when a holding breaches its cap, a sector exceeds its limit, or the investment thesis changes.`,
    researchMandate: [
      "Compare selected holdings against the full eligible sector/theme universe.",
      "Call out omitted candidates that were close but lost to concentration, volatility, share-price, or whole-share rounding constraints.",
      "State live-data gaps plainly instead of inventing current market facts.",
    ],
    agentDebate: [
      {
        agent: "Universe Scout",
        finding: `${allocation.candidateAudit.eligibleCount} eligible candidates were scored before selecting ${allocation.candidateAudit.selectedCount}.`,
      },
      {
        agent: "Risk Officer",
        finding: `Largest holding is ${allocation.summary.largestHoldingTicker ?? "N/A"} at ${allocation.summary.largestHoldingPct.toFixed(1)}%; largest sector is ${allocation.summary.largestSector ?? "N/A"} at ${allocation.summary.largestSectorExposurePct.toFixed(1)}%.`,
      },
    ],
    assumptionsAndLimitations: allocation.assumptions,
    providerLabel,
    generatedAt: new Date().toISOString(),
    fallbackUsed: true,
    providerDiagnostics,
  };
}

export function sanitizeReportResponse(
  value: unknown,
  allocation: PortfolioAllocationResult,
  providerLabel: string,
): PortfolioReportLLMResponse {
  if (!value || typeof value !== "object") {
    return fallbackReport(allocation, providerLabel);
  }
  const record = value as Record<string, unknown>;
  const holdingRationales =
    record.holdingRationales && typeof record.holdingRationales === "object"
      ? sanitizeHoldingRationales(record.holdingRationales as Record<string, unknown>, allocation)
      : fallbackReport(allocation, providerLabel).holdingRationales;

  return {
    executiveSummary: cleanText(record.executiveSummary, fallbackReport(allocation, providerLabel).executiveSummary),
    portfolioReasoning: cleanText(record.portfolioReasoning, fallbackReport(allocation, providerLabel).portfolioReasoning),
    optimalityReview: cleanText(record.optimalityReview, fallbackReport(allocation, providerLabel).optimalityReview),
    universeReview: cleanText(record.universeReview, fallbackReport(allocation, providerLabel).universeReview),
    holdingRationales,
    timeHorizonExplanation: cleanText(record.timeHorizonExplanation, fallbackReport(allocation, providerLabel).timeHorizonExplanation),
    riskAnalysis: cleanText(record.riskAnalysis, fallbackReport(allocation, providerLabel).riskAnalysis),
    rebalancingPlan: cleanText(record.rebalancingPlan, fallbackReport(allocation, providerLabel).rebalancingPlan),
    researchMandate: Array.isArray(record.researchMandate)
      ? record.researchMandate.map((item) => cleanText(item, "")).filter(Boolean).slice(0, 8)
      : fallbackReport(allocation, providerLabel).researchMandate,
    agentDebate: sanitizeAgentDebate(record.agentDebate, fallbackReport(allocation, providerLabel).agentDebate),
    assumptionsAndLimitations: Array.isArray(record.assumptionsAndLimitations)
      ? record.assumptionsAndLimitations.map((item) => cleanText(item, "")).filter(Boolean).slice(0, 8)
      : allocation.assumptions,
    providerLabel,
    generatedAt: cleanText(record.generatedAt, new Date().toISOString()),
    fallbackUsed: false,
    providerDiagnostics: Array.isArray(record.providerDiagnostics)
      ? record.providerDiagnostics.map((item) => cleanText(item, "")).filter(Boolean).slice(0, 6)
      : undefined,
  };
}

function buildFallbackOptimalityReview(allocation: PortfolioAllocationResult): string {
  const rejected = allocation.candidateAudit.topRejected
    .slice(0, 4)
    .map((row) => `${row.ticker} (${row.reason})`)
    .join("; ");
  return rejected
    ? `The optimizer scored the selected names against the eligible universe and retained the highest-fit holdings after intentional cash-reserve, sector, holding-size, and share-rounding constraints. Closest omitted candidates: ${rejected}.`
    : "The optimizer did not surface close omitted candidates after applying the selected universe, holding count, intentional cash-reserve, sector caps, and share-rounding constraints.";
}

function buildFallbackUniverseReview(allocation: PortfolioAllocationResult): string {
  const sectors = allocation.candidateAudit.selectedSectors.join(", ");
  return `Universe scan: ${allocation.candidateAudit.universeCount} candidates available, ${allocation.candidateAudit.eligibleCount} eligible after filters, ${allocation.candidateAudit.selectedCount} selected. Selected sector/theme filters: ${sectors || "broad default"}.`;
}

function sanitizeAgentDebate(
  value: unknown,
  fallback: PortfolioReportLLMResponse["agentDebate"],
): PortfolioReportLLMResponse["agentDebate"] {
  if (!Array.isArray(value)) return fallback;
  return value
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const record = item as Record<string, unknown>;
      const agent = cleanText(record.agent, "");
      const finding = cleanText(record.finding, "");
      return agent && finding ? { agent, finding } : null;
    })
    .filter((item): item is { agent: string; finding: string } => Boolean(item))
    .slice(0, 6);
}

function sanitizeHoldingRationales(
  raw: Record<string, unknown>,
  allocation: PortfolioAllocationResult,
): Record<string, string> {
  const allowed = new Set(allocation.holdings.map((h) => h.ticker));
  const output: Record<string, string> = {};
  for (const holding of allocation.holdings) {
    output[holding.ticker] = cleanText(raw[holding.ticker], `${holding.role}: ${holding.reason}`);
  }
  for (const key of Object.keys(output)) {
    if (!allowed.has(key)) delete output[key];
  }
  return output;
}

function cleanText(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const trimmed = value.replace(/\s+/g, " ").trim();
  if (!trimmed || trimmed.length > 4000) return fallback;
  return trimmed;
}
