/**
 * backtest-lab — pure read-only analyzer for the Backtest and Scenario Lab.
 *
 * Surfaces fee impact, per-symbol attribution, readiness checks, and
 * exportable run receipts from existing BacktestReport + BacktestManifest.
 * No mutations, no trading signals, no side effects.
 */

import type { BacktestManifest, BacktestReport } from "@/lib/types";

export type BacktestLabState = "ready" | "review" | "blocked";
export type BacktestLabSeverity = "pass" | "watch" | "fail";

export interface BacktestLabCheck {
  id: string;
  label: string;
  severity: BacktestLabSeverity;
  detail: string;
}

export interface BacktestLabAttribution {
  symbol: string;
  fills: number;
  notionalTraded: number;
  feesPaid: number;
  feeBps: number;
  pctOfTotalNotional: number;
}

export interface BacktestLabReceipt {
  schema_version: string;
  run_id: string;
  exported_at: number;
  manifest: {
    strategy: string;
    strategy_params: Record<string, unknown>;
    starting_cash: number;
    freq: string;
    venue: string;
    asset_class: string;
    symbols: string[];
    n_bars: number;
    n_fills: number;
  };
  metrics: {
    final_equity: number;
    total_return_pct: number;
    sharpe: number | null;
    max_drawdown_pct: number | null;
    fees_paid_total: number;
    fee_impact_pct: number;
    turnover_ratio: number;
  };
  attribution: BacktestLabAttribution[];
  assumptions: string[];
}

export interface BacktestLabSummary {
  state: BacktestLabState;
  score: number;
  headline: string;
  checks: BacktestLabCheck[];
  actions: string[];
  stats: {
    hasReport: boolean;
    nFills: number;
    feesPaidTotal: number;
    feeImpactPct: number;
    turnoverRatio: number;
    grossNotional: number;
    netReturnPct: number;
    symbolsTraded: number;
  };
  attribution: BacktestLabAttribution[];
  assumptions: string[];
}

// ---------------------------------------------------------------------------
// Main builder
// ---------------------------------------------------------------------------

export function buildBacktestLab({
  report,
  manifest,
}: {
  report: BacktestReport | null;
  manifest: BacktestManifest | null;
}): BacktestLabSummary {
  const stats = buildStats(report);
  const attribution = buildAttribution(report);
  const assumptions = buildAssumptions(report, manifest);
  const checks = buildChecks({ report, manifest, stats });
  const failed = checks.filter((c) => c.severity === "fail").length;
  const watches = checks.filter((c) => c.severity === "watch").length;
  const state: BacktestLabState = failed > 0 ? "blocked" : watches > 0 ? "review" : "ready";
  const score = Math.max(0, 100 - failed * 30 - watches * 10);

  const headline = !report
    ? "No backtest run selected"
    : state === "ready"
      ? "Backtest run looks healthy"
      : state === "review"
        ? "Backtest run needs attention"
        : "Backtest run has issues";

  const actions: string[] = [];
  if (!report) actions.push("Select or run a backtest to see lab analysis");
  if (stats.feeImpactPct > 1) actions.push("Fee impact above 1% — consider lower-turnover strategy params");
  if (stats.nFills === 0) actions.push("Zero fills — strategy may not be generating signals");
  if (stats.turnoverRatio > 20) actions.push("Very high turnover — slippage assumptions may be optimistic");

  return { state, score, headline, checks, actions, stats, attribution, assumptions };
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

function buildStats(report: BacktestReport | null): BacktestLabSummary["stats"] {
  if (!report) {
    return {
      hasReport: false,
      nFills: 0,
      feesPaidTotal: 0,
      feeImpactPct: 0,
      turnoverRatio: 0,
      grossNotional: 0,
      netReturnPct: 0,
      symbolsTraded: 0,
    };
  }

  const grossNotional = report.per_symbol.reduce(
    (sum, s) => sum + s.notional_traded,
    0,
  );
  const feeImpactPct =
    report.starting_cash > 0
      ? (report.fees_paid_total / report.starting_cash) * 100
      : 0;
  const turnoverRatio =
    report.starting_cash > 0 ? grossNotional / report.starting_cash : 0;
  const netReturnPct = report.total_return_pct;

  return {
    hasReport: true,
    nFills: report.n_fills,
    feesPaidTotal: report.fees_paid_total,
    feeImpactPct,
    turnoverRatio,
    grossNotional,
    netReturnPct,
    symbolsTraded: report.per_symbol.length,
  };
}

// ---------------------------------------------------------------------------
// Attribution
// ---------------------------------------------------------------------------

function buildAttribution(
  report: BacktestReport | null,
): BacktestLabAttribution[] {
  if (!report) return [];

  const totalNotional = report.per_symbol.reduce(
    (sum, s) => sum + s.notional_traded,
    0,
  );

  return report.per_symbol
    .map((s) => ({
      symbol: s.symbol,
      fills: s.fills,
      notionalTraded: s.notional_traded,
      feesPaid: s.fees_paid,
      feeBps:
        s.notional_traded > 0
          ? (s.fees_paid / s.notional_traded) * 10_000
          : 0,
      pctOfTotalNotional:
        totalNotional > 0
          ? (s.notional_traded / totalNotional) * 100
          : 0,
    }))
    .sort((a, b) => b.notionalTraded - a.notionalTraded);
}

// ---------------------------------------------------------------------------
// Assumptions
// ---------------------------------------------------------------------------

function buildAssumptions(
  report: BacktestReport | null,
  manifest: BacktestManifest | null,
): string[] {
  const assumptions: string[] = [];

  if (manifest) {
    assumptions.push(`Strategy: ${manifest.strategy_name}`);
    assumptions.push(`Starting cash: $${manifest.starting_cash.toLocaleString()}`);
    assumptions.push(`Frequency: ${manifest.freq}`);
    assumptions.push(`Venue: ${manifest.venue}`);
    assumptions.push(`Asset class: ${manifest.asset_class}`);
    assumptions.push(`Bars/year: ${manifest.bars_per_year}`);
    if (manifest.strategy_params && Object.keys(manifest.strategy_params).length > 0) {
      assumptions.push(`Params: ${JSON.stringify(manifest.strategy_params)}`);
    }
  }

  if (report) {
    assumptions.push(`Fees paid: $${report.fees_paid_total.toFixed(2)}`);
    assumptions.push(`Fill count: ${report.n_fills}`);
    if (report.fees_paid_total === 0) {
      assumptions.push("⚠ Zero fees — cost model may be disabled");
    }
  }

  assumptions.push("Slippage: default SimBroker (no explicit slippage model)");
  assumptions.push("Risk gate: not simulated in backtest engine");

  return assumptions;
}

// ---------------------------------------------------------------------------
// Checks
// ---------------------------------------------------------------------------

function buildChecks({
  report,
  manifest,
  stats,
}: {
  report: BacktestReport | null;
  manifest: BacktestManifest | null;
  stats: BacktestLabSummary["stats"];
}): BacktestLabCheck[] {
  const checks: BacktestLabCheck[] = [];

  // 1. Report presence
  if (!report) {
    checks.push({ id: "report", label: "Report", severity: "fail", detail: "No backtest report loaded" });
    return checks;
  }
  checks.push({ id: "report", label: "Report", severity: "pass", detail: "Report loaded" });

  // 2. Fill count
  if (stats.nFills === 0) {
    checks.push({ id: "fills", label: "Fills", severity: "watch", detail: "Zero fills — strategy did not trade" });
  } else {
    checks.push({ id: "fills", label: "Fills", severity: "pass", detail: `${stats.nFills} fills across ${stats.symbolsTraded} symbols` });
  }

  // 3. Fee impact
  if (stats.feeImpactPct > 2) {
    checks.push({ id: "fees", label: "Fee impact", severity: "watch", detail: `Fees consume ${stats.feeImpactPct.toFixed(2)}% of starting capital` });
  } else if (stats.feeImpactPct > 0.5) {
    checks.push({ id: "fees", label: "Fee impact", severity: "watch", detail: `Fees consume ${stats.feeImpactPct.toFixed(2)}% of starting capital` });
  } else {
    checks.push({ id: "fees", label: "Fee impact", severity: "pass", detail: `Fee impact ${stats.feeImpactPct.toFixed(2)}% of starting capital` });
  }

  // 4. Zero-fee warning
  if (report.fees_paid_total === 0 && stats.nFills > 0) {
    checks.push({ id: "zero-fees", label: "Cost model", severity: "watch", detail: "Fills exist but fees are zero — cost model may be disabled" });
  }

  // 5. Turnover
  if (stats.turnoverRatio > 20) {
    checks.push({ id: "turnover", label: "Turnover", severity: "watch", detail: `Turnover ratio ${stats.turnoverRatio.toFixed(1)}x — slippage may be understated` });
  } else {
    checks.push({ id: "turnover", label: "Turnover", severity: "pass", detail: `Turnover ratio ${stats.turnoverRatio.toFixed(1)}x` });
  }

  // 6. Risk gate
  checks.push({
    id: "risk-gate",
    label: "Risk gate",
    severity: "watch",
    detail: "Risk gate not simulated — rejected trades are invisible",
  });

  // 7. Manifest presence
  if (!manifest) {
    checks.push({ id: "manifest", label: "Manifest", severity: "watch", detail: "No manifest — run assumptions unavailable" });
  } else {
    checks.push({ id: "manifest", label: "Manifest", severity: "pass", detail: "Manifest present with run assumptions" });
  }

  return checks;
}

// ---------------------------------------------------------------------------
// Receipt builder
// ---------------------------------------------------------------------------

export function buildBacktestLabReceipt({
  report,
  manifest,
}: {
  report: BacktestReport | null;
  manifest: BacktestManifest | null;
}): BacktestLabReceipt | null {
  if (!report || !manifest) return null;

  const stats = buildStats(report);
  const attribution = buildAttribution(report);
  const assumptions = buildAssumptions(report, manifest);

  return {
    schema_version: "backtest-lab-receipt.v1",
    run_id: manifest.run_id,
    exported_at: Date.now(),
    manifest: {
      strategy: manifest.strategy_name,
      strategy_params: manifest.strategy_params,
      starting_cash: manifest.starting_cash,
      freq: manifest.freq,
      venue: manifest.venue,
      asset_class: manifest.asset_class,
      symbols: manifest.symbols,
      n_bars: manifest.n_bars,
      n_fills: manifest.n_fills,
    },
    metrics: {
      final_equity: report.final_equity,
      total_return_pct: report.total_return_pct,
      sharpe: report.sharpe,
      max_drawdown_pct: report.max_drawdown_pct,
      fees_paid_total: report.fees_paid_total,
      fee_impact_pct: stats.feeImpactPct,
      turnover_ratio: stats.turnoverRatio,
    },
    attribution,
    assumptions,
  };
}

export function backtestLabReceiptFilename(receipt: BacktestLabReceipt): string {
  return `backtest-lab-${receipt.run_id.slice(0, 12)}-${new Date(receipt.exported_at).toISOString().slice(0, 10)}.json`;
}
