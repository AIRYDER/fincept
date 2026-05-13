/**
 * recon-checklist — pure read-only analyzer for the Daily Reconciliation Checklist.
 *
 * Classifies reconciliation issues with severity and owner, builds a daily
 * checklist, and produces an exportable receipt. No mutations, no side effects.
 */

import type {
  DataCoverageRow,
  OrderRecord,
  Position,
  ServicesResponse,
  StrategyConfigRow,
  StrategyRow,
  UniverseRow,
} from "@/lib/types";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ReconSeverity = "critical" | "warning" | "info";
export type ReconOwner = "operator" | "data" | "strategy" | "risk" | "broker";

export interface ReconIssue {
  id: string;
  label: string;
  severity: ReconSeverity;
  owner: ReconOwner;
  detail: string;
  repairAction: string | null;
}

export interface ReconChecklistSummary {
  state: "clean" | "attention" | "critical";
  score: number;
  headline: string;
  issues: ReconIssue[];
  stats: {
    openPositions: number;
    strategyGroups: number;
    pendingOrders: number;
    rejectedOrders: number;
    missingConfigs: number;
    missingRuntimes: number;
    missingUniverse: number;
    coverageGaps: number;
    servicesDown: number;
    criticalCount: number;
    warningCount: number;
  };
}

export interface ReconReceipt {
  schema_version: string;
  exported_at: number;
  state: ReconChecklistSummary["state"];
  score: number;
  stats: ReconChecklistSummary["stats"];
  issues: Array<{
    id: string;
    label: string;
    severity: ReconSeverity;
    owner: ReconOwner;
    detail: string;
  }>;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function asNum(value: string | number | null | undefined): number {
  if (value === null || value === undefined) return 0;
  const n = typeof value === "string" ? Number(value) : value;
  return Number.isFinite(n) ? n : 0;
}

// ---------------------------------------------------------------------------
// Main builder
// ---------------------------------------------------------------------------

export function buildReconChecklist({
  positions,
  strategies,
  configs,
  universe,
  coverage,
  orders,
  services,
}: {
  positions: Position[];
  strategies: StrategyRow[];
  configs: StrategyConfigRow[];
  universe: UniverseRow[];
  coverage: DataCoverageRow[];
  orders: OrderRecord[];
  services?: ServicesResponse | null;
}): ReconChecklistSummary {
  const openPositions = positions.filter((p) => asNum(p.quantity) !== 0);
  const configsByStrategy = new Map(configs.map((c) => [c.strategy_id, c]));
  const runtimeByStrategy = new Map(strategies.map((s) => [s.strategy_id, s]));
  const universeSymbols = new Set(universe.map((u) => u.symbol));
  const coverageBySymbol = new Map(coverage.map((c) => [c.symbol, c]));

  const issues: ReconIssue[] = [];

  // Group positions by strategy
  const grouped = new Map<string, Position[]>();
  for (const pos of openPositions) {
    const rows = grouped.get(pos.strategy_id) ?? [];
    rows.push(pos);
    grouped.set(pos.strategy_id, rows);
  }

  let missingConfigs = 0;
  let missingRuntimes = 0;
  let missingUniverse = 0;
  let coverageGaps = 0;

  // Per-strategy checks
  for (const [strategyId, rows] of grouped) {
    const config = configsByStrategy.get(strategyId);
    const runtime = runtimeByStrategy.get(strategyId);

    if (!config) {
      missingConfigs += 1;
      issues.push({
        id: `missing-config:${strategyId}`,
        label: "Missing config",
        severity: "critical",
        owner: "strategy",
        detail: `Strategy ${strategyId} has ${rows.length} open positions but no config row`,
        repairAction: `Adopt tracker config for ${strategyId}`,
      });
    } else if (!config.enabled) {
      issues.push({
        id: `config-disabled:${strategyId}`,
        label: "Config disabled",
        severity: "warning",
        owner: "strategy",
        detail: `Strategy ${strategyId} config is disabled`,
        repairAction: `Enable config for ${strategyId}`,
      });
    }

    if (!runtime) {
      missingRuntimes += 1;
      issues.push({
        id: `no-runtime:${strategyId}`,
        label: "No runtime row",
        severity: "warning",
        owner: "strategy",
        detail: `Strategy ${strategyId} has no runtime row — agent may not be running`,
        repairAction: `Check agent status for ${strategyId}`,
      });
    }

    for (const row of rows) {
      if (!universeSymbols.has(row.symbol)) {
        missingUniverse += 1;
        issues.push({
          id: `missing-universe:${strategyId}:${row.symbol}`,
          label: "Missing universe",
          severity: "warning",
          owner: "data",
          detail: `${row.symbol} (strategy ${strategyId}) is not in the universe`,
          repairAction: `Seed universe from positions`,
        });
        // Only report once per symbol per strategy
        break;
      }
    }

    for (const row of rows) {
      const cov = coverageBySymbol.get(row.symbol);
      if (!cov || cov.status !== "ok") {
        coverageGaps += 1;
        issues.push({
          id: `coverage-gap:${strategyId}:${row.symbol}`,
          label: "Coverage gap",
          severity: "warning",
          owner: "data",
          detail: `${row.symbol} (strategy ${strategyId}) has ${cov ? cov.status : "no"} market data coverage`,
          repairAction: `Start market data or check data source for ${row.symbol}`,
        });
        break;
      }
    }
  }

  // Pending orders
  const pendingOrders = orders.filter((o) =>
    ["pending_new", "new", "partially_filled"].includes(o.status),
  );
  if (pendingOrders.length > 0) {
    issues.push({
      id: "pending-orders",
      label: "Pending orders",
      severity: pendingOrders.length > 5 ? "critical" : "warning",
      owner: "broker",
      detail: `${pendingOrders.length} orders pending execution`,
      repairAction: "Review pending orders for stale state",
    });
  }

  // Rejected orders
  const rejectedOrders = orders.filter((o) => o.status === "rejected");
  if (rejectedOrders.length > 0) {
    issues.push({
      id: "rejected-orders",
      label: "Rejected orders",
      severity: "critical",
      owner: "risk",
      detail: `${rejectedOrders.length} orders were rejected by risk gate`,
      repairAction: "Review rejected orders and risk settings",
    });
  }

  // Service heartbeat
  let servicesDown = 0;
  if (services) {
    for (const svc of services.services) {
      if (svc.expected && svc.status !== "up") {
        servicesDown += 1;
        issues.push({
          id: `service-down:${svc.name}`,
          label: "Service down",
          severity: "critical",
          owner: "operator",
          detail: `${svc.name} is ${svc.status}`,
          repairAction: `Restart or check ${svc.name}`,
        });
      }
    }
  }

  // Data freshness (if coverage has stale entries)
  const staleCount = coverage.filter((c) => c.status === "stale").length;
  if (staleCount > 0) {
    issues.push({
      id: "stale-coverage",
      label: "Stale coverage",
      severity: "warning",
      owner: "data",
      detail: `${staleCount} symbols have stale market data`,
      repairAction: "Check market data scheduler and data source connectivity",
    });
  }

  // Classify state
  const criticalCount = issues.filter((i) => i.severity === "critical").length;
  const warningCount = issues.filter((i) => i.severity === "warning").length;
  const state: ReconChecklistSummary["state"] =
    criticalCount > 0 ? "critical" : warningCount > 0 ? "attention" : "clean";
  const score = Math.max(0, 100 - criticalCount * 25 - warningCount * 5);

  const headline =
    state === "clean"
      ? "All checks passed"
      : state === "attention"
        ? `${warningCount} items need attention`
        : `${criticalCount} critical issues`;

  return {
    state,
    score,
    headline,
    issues,
    stats: {
      openPositions: openPositions.length,
      strategyGroups: grouped.size,
      pendingOrders: pendingOrders.length,
      rejectedOrders: rejectedOrders.length,
      missingConfigs,
      missingRuntimes,
      missingUniverse,
      coverageGaps,
      servicesDown,
      criticalCount,
      warningCount,
    },
  };
}

// ---------------------------------------------------------------------------
// Receipt builder
// ---------------------------------------------------------------------------

export function buildReconReceipt(
  summary: ReconChecklistSummary,
): ReconReceipt {
  return {
    schema_version: "recon-checklist-receipt.v1",
    exported_at: Date.now(),
    state: summary.state,
    score: summary.score,
    stats: summary.stats,
    issues: summary.issues.map(({ id, label, severity, owner, detail }) => ({
      id,
      label,
      severity,
      owner,
      detail,
    })),
  };
}

export function reconReceiptFilename(receipt: ReconReceipt): string {
  const date = new Date(receipt.exported_at).toISOString().slice(0, 10);
  return `recon-checklist-${date}-${receipt.state}.json`;
}
