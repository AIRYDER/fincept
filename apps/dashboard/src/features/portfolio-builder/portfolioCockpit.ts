import type { EvidenceRow } from "@/components/evidence/evidence-stack";

import type {
  PortfolioAllocationResult,
  PortfolioReportLLMResponse,
} from "./portfolioBuilder.types";

export type PortfolioReadinessState = "ready" | "review" | "blocked";
export type PortfolioReadinessSeverity = "pass" | "watch" | "fail";

export interface PortfolioReadinessCheck {
  id: string;
  label: string;
  severity: PortfolioReadinessSeverity;
  detail: string;
}

export interface PortfolioBudgetRail {
  label: string;
  valuePct: number;
  limitPct: number;
  status: PortfolioReadinessSeverity;
  detail: string;
}

export interface PortfolioCockpitSummary {
  state: PortfolioReadinessState;
  score: number;
  headline: string;
  checks: PortfolioReadinessCheck[];
  budgetRails: PortfolioBudgetRail[];
  evidenceRows: EvidenceRow[];
  traceRows: EvidenceRow[];
  operatorActions: string[];
  payload: Record<string, unknown>;
}

export interface PortfolioCockpitReceipt {
  schemaVersion: "fincept.portfolio_cockpit_receipt.v1";
  inputHash: string;
  generatedFrom: {
    marketDataTimestamp: string;
    reportGeneratedAt: string | null;
  };
  state: PortfolioReadinessState;
  score: number;
  headline: string;
  checks: PortfolioReadinessCheck[];
  budgetRails: PortfolioBudgetRail[];
  operatorActions: string[];
  evidenceRows: EvidenceRow[];
  traceRows: EvidenceRow[];
  payload: Record<string, unknown>;
}

export function buildPortfolioCockpit(
  allocation: PortfolioAllocationResult,
  report?: PortfolioReportLLMResponse | null,
): PortfolioCockpitSummary {
  const checks = buildReadinessChecks(allocation, report);
  const budgetRails = buildBudgetRails(allocation);
  const failed = checks.filter((check) => check.severity === "fail").length;
  const watches = checks.filter((check) => check.severity === "watch").length;
  const state: PortfolioReadinessState = failed > 0 ? "blocked" : watches > 0 ? "review" : "ready";
  const score = clamp(
    100 - failed * 24 - watches * 8 - budgetRails.filter((rail) => rail.status === "watch").length * 4,
    0,
    100,
  );

  return {
    state,
    score: round(score),
    headline: headlineFor(state, failed, watches),
    checks,
    budgetRails,
    evidenceRows: buildEvidenceRows(allocation, report, checks),
    traceRows: buildTraceRows(allocation, report),
    operatorActions: buildOperatorActions(state, checks, budgetRails),
    payload: {
      input: allocation.input,
      marketData: allocation.marketData,
      summary: allocation.summary,
      optimization: allocation.optimization,
      candidateAudit: allocation.candidateAudit,
      warnings: allocation.warnings,
      report: report
        ? {
            providerLabel: report.providerLabel,
            generatedAt: report.generatedAt,
            fallbackUsed: !!report.fallbackUsed,
            providerDiagnostics: report.providerDiagnostics ?? [],
          }
        : null,
    },
  };
}

export function buildPortfolioCockpitReceipt(
  allocation: PortfolioAllocationResult,
  report?: PortfolioReportLLMResponse | null,
): PortfolioCockpitReceipt {
  const cockpit = buildPortfolioCockpit(allocation, report);
  const hashSource = {
    generatedFrom: {
      marketDataTimestamp: allocation.marketData.timestamp,
      reportGeneratedAt: report?.generatedAt ?? null,
    },
    state: cockpit.state,
    score: cockpit.score,
    checks: cockpit.checks,
    budgetRails: cockpit.budgetRails,
    operatorActions: cockpit.operatorActions,
    payload: cockpit.payload,
  };
  return {
    schemaVersion: "fincept.portfolio_cockpit_receipt.v1",
    inputHash: hashStableValue(hashSource),
    generatedFrom: hashSource.generatedFrom,
    state: cockpit.state,
    score: cockpit.score,
    headline: cockpit.headline,
    checks: cockpit.checks,
    budgetRails: cockpit.budgetRails,
    operatorActions: cockpit.operatorActions,
    evidenceRows: cockpit.evidenceRows,
    traceRows: cockpit.traceRows,
    payload: cockpit.payload,
  };
}

export function portfolioCockpitReceiptToJson(
  allocation: PortfolioAllocationResult,
  report?: PortfolioReportLLMResponse | null,
): string {
  return JSON.stringify(buildPortfolioCockpitReceipt(allocation, report), null, 2);
}

export function buildPortfolioCockpitReceiptFilename(
  allocation: PortfolioAllocationResult,
  report?: PortfolioReportLLMResponse | null,
): string {
  const receipt = buildPortfolioCockpitReceipt(allocation, report);
  const amount = Math.max(0, Math.round(allocation.summary.startingAmount));
  return `fincept-portfolio-cockpit-${amount}-${slugify(allocation.input.riskLevel)}-${slugify(allocation.input.horizon)}-${receipt.state}-${receipt.inputHash.slice(0, 10)}.json`;
}

function buildReadinessChecks(
  allocation: PortfolioAllocationResult,
  report?: PortfolioReportLLMResponse | null,
): PortfolioReadinessCheck[] {
  const optimization = allocation.optimization;
  const summary = allocation.summary;
  const preferences = allocation.input.preferences;
  const warnings = [...allocation.warnings, ...optimization.warnings, ...(report?.providerDiagnostics ?? [])];

  return [
    {
      id: "optimizer-feasible",
      label: "Optimizer feasibility",
      severity: optimization.feasible ? "pass" : "fail",
      detail: optimization.feasible
        ? `${optimization.method} emitted a feasible allocation packet.`
        : `${optimization.method} did not emit a feasible allocation packet.`,
    },
    {
      id: "data-mode",
      label: "Market data mode",
      severity: allocation.marketData.dataMode === "live" ? "pass" : "watch",
      detail:
        allocation.marketData.dataMode === "live"
          ? `Live market data from ${allocation.marketData.source}.`
          : `Demo data from ${allocation.marketData.source}; planning output only.`,
    },
    {
      id: "deployment",
      label: "Capital deployment",
      severity: summary.totalInvested > 0 && summary.totalValue <= summary.startingAmount + 0.01 ? "pass" : "fail",
      detail: `${pct(summary.cashPercent)} cash remains after reserve and share conversion.`,
    },
    {
      id: "single-name-cap",
      label: "Single-name budget",
      severity: summary.largestHoldingPct <= preferences.maxAllocationPerHoldingPct + 0.5 ? "pass" : "fail",
      detail: `${summary.largestHoldingTicker ?? "N/A"} is largest at ${pct(summary.largestHoldingPct)} versus ${pct(preferences.maxAllocationPerHoldingPct)} cap.`,
    },
    {
      id: "sector-cap",
      label: "Sector budget",
      severity: summary.largestSectorExposurePct <= preferences.maxSectorConcentrationPct + 0.75 ? "pass" : "fail",
      detail: `${summary.largestSector ?? "N/A"} is largest at ${pct(summary.largestSectorExposurePct)} versus ${pct(preferences.maxSectorConcentrationPct)} cap.`,
    },
    {
      id: "frontier-depth",
      label: "Frontier diagnostics",
      severity: (optimization.frontier ?? []).some((point) => point.feasible) ? "pass" : "watch",
      detail: `${(optimization.frontier ?? []).filter((point) => point.feasible).length}/${optimization.frontier?.length ?? 0} frontier points are feasible.`,
    },
    {
      id: "report-provider",
      label: "Committee packet",
      severity: !report ? "watch" : report.fallbackUsed ? "watch" : "pass",
      detail: !report
        ? "AI committee packet is not generated yet."
        : report.fallbackUsed
          ? `${report.providerLabel} used a fallback packet.`
          : `${report.providerLabel} generated the committee packet.`,
    },
    {
      id: "warnings",
      label: "Warnings",
      severity: warnings.length > 0 ? "watch" : "pass",
      detail: warnings.length ? `${warnings.length} warning(s) require operator review.` : "No warnings reported.",
    },
  ];
}

function buildBudgetRails(allocation: PortfolioAllocationResult): PortfolioBudgetRail[] {
  const summary = allocation.summary;
  const preferences = allocation.input.preferences;
  const feasiblePoints = (allocation.optimization.frontier ?? []).filter((point) => point.feasible).length;
  const totalPoints = allocation.optimization.frontier?.length ?? 0;
  const frontierPct = totalPoints > 0 ? (feasiblePoints / totalPoints) * 100 : 0;

  return [
    {
      label: "Single-name cap",
      valuePct: summary.largestHoldingPct,
      limitPct: preferences.maxAllocationPerHoldingPct,
      status: summary.largestHoldingPct <= preferences.maxAllocationPerHoldingPct + 0.5 ? "pass" : "fail",
      detail: summary.largestHoldingTicker ?? "No largest holding",
    },
    {
      label: "Sector cap",
      valuePct: summary.largestSectorExposurePct,
      limitPct: preferences.maxSectorConcentrationPct,
      status: summary.largestSectorExposurePct <= preferences.maxSectorConcentrationPct + 0.75 ? "pass" : "fail",
      detail: summary.largestSector ?? "No sector exposure",
    },
    {
      label: "Cash reserve",
      valuePct: summary.cashPercent,
      limitPct: Math.max(preferences.cashReservePct, 1),
      status: summary.cashPercent >= preferences.cashReservePct - 0.25 ? "pass" : "watch",
      detail: `${pct(preferences.cashReservePct)} intentional reserve target`,
    },
    {
      label: "Frontier feasibility",
      valuePct: frontierPct,
      limitPct: 100,
      status: frontierPct >= 50 ? "pass" : frontierPct > 0 ? "watch" : "fail",
      detail: `${feasiblePoints}/${totalPoints} feasible points`,
    },
  ];
}

function buildEvidenceRows(
  allocation: PortfolioAllocationResult,
  report: PortfolioReportLLMResponse | null | undefined,
  checks: PortfolioReadinessCheck[],
): EvidenceRow[] {
  const passCount = checks.filter((check) => check.severity === "pass").length;
  const failCount = checks.filter((check) => check.severity === "fail").length;
  return [
    { label: "readiness checks", value: `${passCount}/${checks.length} pass`, tone: failCount ? "critical" : "verified" },
    { label: "optimizer method", value: allocation.optimization.method, tone: allocation.optimization.feasible ? "verified" : "critical" },
    { label: "candidate universe", value: `${allocation.candidateAudit.eligibleCount}/${allocation.candidateAudit.universeCount} eligible`, tone: allocation.candidateAudit.eligibleCount ? "verified" : "critical" },
    { label: "selected holdings", value: `${allocation.candidateAudit.selectedCount}`, tone: allocation.candidateAudit.selectedCount ? "verified" : "critical" },
    { label: "data mode", value: allocation.marketData.dataMode, tone: allocation.marketData.dataMode === "live" ? "verified" : "caveat" },
    { label: "committee provider", value: report?.providerLabel ?? "not generated", tone: report?.fallbackUsed ? "caveat" : report ? "model" : "muted" },
  ];
}

function buildTraceRows(
  allocation: PortfolioAllocationResult,
  report?: PortfolioReportLLMResponse | null,
): EvidenceRow[] {
  return [
    ...allocation.candidateAudit.constraintNotes.slice(0, 6).map((note) => ({
      label: "constraint",
      value: note,
      tone: "verified" as const,
    })),
    ...allocation.warnings.slice(0, 4).map((warning) => ({
      label: "allocation warning",
      value: warning,
      tone: "caveat" as const,
    })),
    ...(report?.providerDiagnostics ?? []).slice(0, 4).map((diagnostic) => ({
      label: "provider diagnostic",
      value: diagnostic,
      tone: "caveat" as const,
    })),
  ];
}

function buildOperatorActions(
  state: PortfolioReadinessState,
  checks: PortfolioReadinessCheck[],
  budgetRails: PortfolioBudgetRail[],
): string[] {
  const actions: string[] = [];
  if (state === "ready") {
    actions.push("Export the packet and review assumptions before translating ideas into explicit strategy config changes.");
  }
  for (const check of checks.filter((item) => item.severity !== "pass")) {
    actions.push(`${check.label}: ${check.detail}`);
  }
  for (const rail of budgetRails.filter((item) => item.status === "fail")) {
    actions.push(`${rail.label} exceeds budget; tighten constraints or regenerate before committee review.`);
  }
  if (!actions.length) actions.push("No immediate operator actions generated.");
  return actions.slice(0, 6);
}

function headlineFor(state: PortfolioReadinessState, failed: number, watches: number): string {
  if (state === "blocked") return `${failed} hard blocker${failed === 1 ? "" : "s"} before portfolio review.`;
  if (state === "review") return `${watches} watch item${watches === 1 ? "" : "s"}; export only after operator review.`;
  return "Optimizer packet is ready for investment committee review.";
}

function pct(value: number): string {
  return `${round(value).toFixed(1)}%`;
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function hashStableValue(value: unknown): string {
  const input = stableStringify(value);
  let hash = 2166136261;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function stableStringify(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`)
    .join(",")}}`;
}

function slugify(value: string): string {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "portfolio";
}
