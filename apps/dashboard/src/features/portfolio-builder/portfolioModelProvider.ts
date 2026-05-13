import { fallbackReport, sanitizeReportResponse } from "./portfolioReportSchema";
import type {
  PortfolioAllocationResult,
  PortfolioModelProvider,
  PortfolioReportLLMResponse,
} from "./portfolioBuilder.types";

export async function generatePortfolioReport(
  allocation: PortfolioAllocationResult,
  provider: PortfolioModelProvider,
): Promise<PortfolioReportLLMResponse> {
  try {
    const response = await fetch("/api/portfolio-report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allocation, provider }),
    });
    if (!response.ok) {
      return fallbackReport(allocation, "Local fallback report", [
        `Portfolio report route returned HTTP ${response.status}.`,
      ]);
    }
    const body = await response.json();
    const report = sanitizeReportResponse(
      body.report,
      allocation,
      typeof body.providerLabel === "string" ? body.providerLabel : "Portfolio report provider",
    );
    if (Array.isArray(body.providerDiagnostics)) {
      report.providerDiagnostics = body.providerDiagnostics
        .filter((item: unknown) => typeof item === "string")
        .slice(0, 6);
    }
    return report;
  } catch {
    return fallbackReport(allocation, "Local fallback report", [
      "Browser could not reach /api/portfolio-report.",
    ]);
  }
}
