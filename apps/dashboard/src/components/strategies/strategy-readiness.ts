import type { Position, StrategyConfigRow } from "@/lib/types";

export type StrategyReadinessState = "ready" | "review" | "blocked";
export type StrategyReadinessSeverity = "pass" | "watch" | "fail";

export interface StrategyReadinessCheck {
  id: string;
  label: string;
  severity: StrategyReadinessSeverity;
  detail: string;
}

export interface StrategyReadinessSummary {
  state: StrategyReadinessState;
  score: number;
  headline: string;
  checks: StrategyReadinessCheck[];
  actions: string[];
}

export function buildStrategyReadiness(
  config: StrategyConfigRow,
  positions: Position[] = [],
): StrategyReadinessSummary {
  const checks = buildChecks(config, positions);
  const failed = checks.filter((check) => check.severity === "fail").length;
  const watches = checks.filter((check) => check.severity === "watch").length;
  const state: StrategyReadinessState = failed > 0 ? "blocked" : watches > 0 ? "review" : "ready";
  const score = clamp(100 - failed * 28 - watches * 10, 0, 100);

  return {
    state,
    score,
    headline: headlineFor(state, failed, watches),
    checks,
    actions: buildActions(state, checks),
  };
}

function buildChecks(config: StrategyConfigRow, positions: Position[]): StrategyReadinessCheck[] {
  const openPositions = positions.filter((position) => asNumber(position.quantity) !== 0);
  return [
    {
      id: "symbols",
      label: "Symbols",
      severity: config.symbols.length > 0 ? "pass" : "fail",
      detail: config.symbols.length > 0 ? `${config.symbols.length} symbol(s) configured.` : "No symbols configured; on_bar will not receive a tradable basket.",
    },
    {
      id: "class-name",
      label: "Strategy class",
      severity: config.class_name.trim() ? "pass" : "fail",
      detail: config.class_name.trim() ? `${config.class_name} is configured.` : "Strategy class is blank.",
    },
    {
      id: "lifecycle-mode",
      label: "Lifecycle mode",
      severity: config.enabled ? "watch" : "pass",
      detail: config.enabled ? "Strategy is enabled; review readiness before relying on next reconcile tick." : "Strategy is stopped, so edits remain planning-safe until explicitly started.",
    },
    {
      id: "model-binding",
      label: "Model binding",
      severity: modelBindingSeverity(config),
      detail: modelBindingDetail(config),
    },
    {
      id: "params",
      label: "Parameter sanity",
      severity: paramsSeverity(config),
      detail: paramsDetail(config),
    },
    {
      id: "position-drift",
      label: "Position drift",
      severity: openPositions.length > 0 ? "watch" : "pass",
      detail: openPositions.length > 0 ? `${openPositions.length} open position(s) exist; stop/edit decisions may leave residual exposure.` : "No open position drift detected for this strategy.",
    },
  ];
}

function modelBindingSeverity(config: StrategyConfigRow): StrategyReadinessSeverity {
  if (config.class_name === "gbm" && !config.model_binding) return "fail";
  if (config.model_binding) return "pass";
  return "watch";
}

function modelBindingDetail(config: StrategyConfigRow): string {
  if (config.class_name === "gbm" && !config.model_binding) return "GBM strategy requires an explicit model binding before review.";
  if (config.model_binding) return `Bound to ${config.model_binding}.`;
  return "No model binding configured; acceptable only for model-free strategies.";
}

function paramsSeverity(config: StrategyConfigRow): StrategyReadinessSeverity {
  if (config.class_name === "ma_crossover" && !validMovingAverageParams(config.params)) return "fail";
  if (config.class_name === "gbm" && !validGbmParams(config.params)) return "fail";
  if (Object.keys(config.params).length === 0) return "watch";
  return "pass";
}

function paramsDetail(config: StrategyConfigRow): string {
  if (config.class_name === "ma_crossover") {
    const fast = asNumber(config.params.fast);
    const slow = asNumber(config.params.slow);
    return validMovingAverageParams(config.params)
      ? `MA windows are ordered: fast ${fast}, slow ${slow}.`
      : "MA crossover requires finite positive fast/slow windows with fast < slow.";
  }
  if (config.class_name === "gbm") {
    return validGbmParams(config.params)
      ? "GBM thresholds and notional params are finite."
      : "GBM requires finite entry_threshold, exit_threshold, and positive per_symbol_notional.";
  }
  return Object.keys(config.params).length > 0
    ? `${Object.keys(config.params).length} parameter(s) configured.`
    : "No params configured; strategy host defaults will apply.";
}

function validMovingAverageParams(params: Record<string, unknown>): boolean {
  const fast = asNumber(params.fast);
  const slow = asNumber(params.slow);
  return Number.isFinite(fast) && Number.isFinite(slow) && fast > 0 && slow > 0 && fast < slow;
}

function validGbmParams(params: Record<string, unknown>): boolean {
  const entry = asNumber(params.entry_threshold);
  const exit = asNumber(params.exit_threshold);
  const notional = asNumber(params.per_symbol_notional);
  return Number.isFinite(entry) && Number.isFinite(exit) && Number.isFinite(notional) && notional > 0;
}

function buildActions(state: StrategyReadinessState, checks: StrategyReadinessCheck[]): string[] {
  if (state === "ready") return ["Review assumptions, then use the lifecycle control only if paper-mode operation is intended."];
  return checks
    .filter((check) => check.severity !== "pass")
    .map((check) => `${check.label}: ${check.detail}`)
    .slice(0, 5);
}

function headlineFor(state: StrategyReadinessState, failed: number, watches: number): string {
  if (state === "blocked") return `${failed} hard blocker${failed === 1 ? "" : "s"} before strategy review.`;
  if (state === "review") return `${watches} watch item${watches === 1 ? "" : "s"}; review before lifecycle changes.`;
  return "Strategy config is ready for paper-mode operator review.";
}

function asNumber(value: unknown): number {
  if (typeof value === "number") return Number.isFinite(value) ? value : 0;
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
