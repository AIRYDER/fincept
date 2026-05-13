/**
 * operator-briefing — pure read-only aggregator for the Overview page.
 *
 * Surfaces the most important operator-grade signals at a glance:
 *   - Safety state (kill switch + critical services)
 *   - Top unresolved reconciliation issues
 *   - Open strategy readiness rollup
 *   - Service heartbeat strip (expected up vs total)
 *   - Last proof receipt status
 *
 * No mutations, no side effects. Reuses existing analyzers:
 *   - buildReconChecklist for recon issues
 *   - buildStrategyReadiness for per-strategy readiness rollup
 *   - buildProofReceiptCenter for receipt status
 */

import { buildProofReceiptCenter } from "@/components/receipts/proof-receipts";
import {
  buildReconChecklist,
  type ReconIssue,
} from "@/components/reconciliation/recon-checklist";
import { buildStrategyReadiness } from "@/components/strategies/strategy-readiness";
import type {
  DataCoverageRow,
  KillSwitchState,
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

export type BriefingState = "ready" | "watch" | "alert";

export interface BriefingStripItem {
  id: string;
  label: string;
  state: BriefingState;
  detail: string;
  href?: string;
}

export interface BriefingStrategyRollup {
  total: number;
  ready: number;
  review: number;
  blocked: number;
  /** Strategies in worst state, capped at 5 */
  attention: Array<{ strategy_id: string; state: "ready" | "review" | "blocked"; headline: string }>;
}

export interface OperatorBriefingPacket {
  state: BriefingState;
  headline: string;
  /** Top-level safety strip (kill switch, services, recon, strategies, receipts) */
  strip: BriefingStripItem[];
  /** Top unresolved recon issues, capped at 5 */
  topIssues: ReconIssue[];
  /** Strategy readiness rollup */
  strategies: BriefingStrategyRollup;
  /** Service summary stats */
  services: {
    up: number;
    stale: number;
    down: number;
    expected: number;
    total: number;
  };
  /** Receipt center summary */
  receipts: {
    state: "ready" | "review" | "blocked";
    total: number;
  };
  /** Kill switch state */
  killSwitch: {
    engaged: boolean;
    reason: string | null;
  };
}

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

export interface OperatorBriefingInput {
  positions: Position[];
  strategies: StrategyRow[];
  configs: StrategyConfigRow[];
  universe: UniverseRow[];
  coverage: DataCoverageRow[];
  orders: OrderRecord[];
  services: ServicesResponse | null | undefined;
  killSwitch: KillSwitchState | null | undefined;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function worstState(states: BriefingState[]): BriefingState {
  if (states.includes("alert")) return "alert";
  if (states.includes("watch")) return "watch";
  return "ready";
}

// ---------------------------------------------------------------------------
// Main builder
// ---------------------------------------------------------------------------

export function buildOperatorBriefing(input: OperatorBriefingInput): OperatorBriefingPacket {
  // --- Recon checklist ----------------------------------------------------
  const recon = buildReconChecklist({
    positions: input.positions,
    strategies: input.strategies,
    configs: input.configs,
    universe: input.universe,
    coverage: input.coverage,
    orders: input.orders,
    services: input.services ?? undefined,
  });
  const criticalIssues = recon.issues.filter((i) => i.severity === "critical");
  const warningIssues = recon.issues.filter((i) => i.severity === "warning");
  const topIssues = [...criticalIssues, ...warningIssues].slice(0, 5);

  // --- Strategy readiness rollup -----------------------------------------
  const configByName = new Map(input.configs.map((c) => [c.strategy_id, c]));
  const strategyRollup: BriefingStrategyRollup = {
    total: 0,
    ready: 0,
    review: 0,
    blocked: 0,
    attention: [],
  };
  const seenStrategyIds = new Set<string>();
  for (const row of input.strategies) {
    if (seenStrategyIds.has(row.strategy_id)) continue;
    seenStrategyIds.add(row.strategy_id);
    const cfg = configByName.get(row.strategy_id);
    if (!cfg) continue;
    strategyRollup.total += 1;
    const summary = buildStrategyReadiness(cfg, input.positions);
    if (summary.state === "ready") strategyRollup.ready += 1;
    else if (summary.state === "review") strategyRollup.review += 1;
    else strategyRollup.blocked += 1;
    if (summary.state !== "ready" && strategyRollup.attention.length < 5) {
      strategyRollup.attention.push({
        strategy_id: row.strategy_id,
        state: summary.state,
        headline: summary.headline,
      });
    }
  }
  // Sort attention: blocked first, then review
  strategyRollup.attention.sort((a, b) => {
    const order = { blocked: 0, review: 1, ready: 2 };
    return order[a.state] - order[b.state];
  });

  // --- Services -----------------------------------------------------------
  const allServices = input.services?.services ?? [];
  const expected = allServices.filter((s) => s.expected);
  const servicesUp = expected.filter((s) => s.status === "up").length;
  const servicesStale = expected.filter((s) => s.status === "stale").length;
  const servicesDown = expected.filter((s) => s.status === "down").length;

  // --- Kill switch --------------------------------------------------------
  const killEngaged = input.killSwitch?.engaged ?? false;

  // --- Receipts -----------------------------------------------------------
  const receiptCenter = buildProofReceiptCenter();

  // --- Build strip --------------------------------------------------------
  const strip: BriefingStripItem[] = [];

  strip.push({
    id: "kill",
    label: "Kill switch",
    state: killEngaged ? "alert" : "ready",
    detail: killEngaged
      ? `ENGAGED. ${input.killSwitch?.reason ?? "No reason provided."}`
      : "Clear",
    href: "/risk",
  });

  strip.push({
    id: "services",
    label: "Services",
    state:
      servicesDown > 0
        ? "alert"
        : servicesStale > 0
          ? "watch"
          : expected.length === 0
            ? "watch"
            : "ready",
    detail:
      expected.length === 0
        ? "No services reported"
        : `${servicesUp}/${expected.length} up${servicesStale ? `, ${servicesStale} stale` : ""}${servicesDown ? `, ${servicesDown} down` : ""}`,
    href: "/system",
  });

  strip.push({
    id: "recon",
    label: "Reconciliation",
    state:
      recon.state === "critical" ? "alert" : recon.state === "attention" ? "watch" : "ready",
    detail:
      criticalIssues.length > 0
        ? `${criticalIssues.length} critical, ${warningIssues.length} warning`
        : warningIssues.length > 0
          ? `${warningIssues.length} warning`
          : "No issues",
    href: "/reconciliation",
  });

  strip.push({
    id: "strategies",
    label: "Strategies",
    state:
      strategyRollup.blocked > 0
        ? "alert"
        : strategyRollup.review > 0
          ? "watch"
          : "ready",
    detail:
      strategyRollup.total === 0
        ? "None active"
        : `${strategyRollup.ready}/${strategyRollup.total} ready${strategyRollup.review ? `, ${strategyRollup.review} review` : ""}${strategyRollup.blocked ? `, ${strategyRollup.blocked} blocked` : ""}`,
    href: "/strategies",
  });

  strip.push({
    id: "receipts",
    label: "Receipts",
    state:
      receiptCenter.state === "blocked"
        ? "alert"
        : receiptCenter.state === "review"
          ? "watch"
          : "ready",
    detail: `${receiptCenter.stats.total} in catalog`,
    href: "/receipts",
  });

  // --- Aggregate state + headline ----------------------------------------
  // Receipts is informational, exclude from overall state (same rule as
  // system-readiness).
  const stateForAggregation = strip.filter((s) => s.id !== "receipts").map((s) => s.state);
  const overall = worstState(stateForAggregation);

  const alertCount = stateForAggregation.filter((s) => s === "alert").length;
  const watchCount = stateForAggregation.filter((s) => s === "watch").length;
  const headline =
    overall === "alert"
      ? `${alertCount} blocker${alertCount === 1 ? "" : "s"} require attention. See safety strip and top issues.`
      : overall === "watch"
        ? `${watchCount} item${watchCount === 1 ? "" : "s"} need a quick look. Otherwise the stack is operational.`
        : "All operator checks pass. Paper trading flow is unblocked.";

  return {
    state: overall,
    headline,
    strip,
    topIssues,
    strategies: strategyRollup,
    services: {
      up: servicesUp,
      stale: servicesStale,
      down: servicesDown,
      expected: expected.length,
      total: allServices.length,
    },
    receipts: {
      state: receiptCenter.state,
      total: receiptCenter.stats.total,
    },
    killSwitch: {
      engaged: killEngaged,
      reason: input.killSwitch?.reason ?? null,
    },
  };
}
