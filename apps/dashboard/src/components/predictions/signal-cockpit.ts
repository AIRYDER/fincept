import type { Prediction, PromotionStateResponse, ServicesResponse } from "@/lib/types";

export type SignalCockpitState = "ready" | "review" | "blocked";
export type SignalCockpitSeverity = "pass" | "watch" | "fail";

export interface SignalCockpitCheck {
  id: string;
  label: string;
  severity: SignalCockpitSeverity;
  detail: string;
}

export interface SignalCockpitSymbolRow {
  symbol: string;
  count: number;
  avgDirection: number;
  avgConfidence: number;
  latestTs: number;
  longCount: number;
  shortCount: number;
  state: "candidate" | "watch" | "quiet";
}

export interface SignalCockpitSummary {
  state: SignalCockpitState;
  score: number;
  headline: string;
  checks: SignalCockpitCheck[];
  actions: string[];
  stats: {
    predictionCount: number;
    agentCount: number;
    symbolCount: number;
    avgConfidence: number;
    highConfidenceCount: number;
    staleCount: number;
    latestAgeSeconds: number | null;
  };
  symbols: SignalCockpitSymbolRow[];
}

export function buildSignalCockpit({
  predictions,
  services,
  promotion,
  nowMs = Date.now(),
}: {
  predictions: Prediction[];
  services?: ServicesResponse | null;
  promotion?: PromotionStateResponse | null;
  nowMs?: number;
}): SignalCockpitSummary {
  const stats = buildStats(predictions, nowMs);
  const symbols = buildSymbols(predictions);
  const checks = buildChecks({ predictions, services: services ?? null, promotion: promotion ?? null, stats, symbols });
  const failed = checks.filter((check) => check.severity === "fail").length;
  const watches = checks.filter((check) => check.severity === "watch").length;
  const state: SignalCockpitState = failed > 0 ? "blocked" : watches > 0 ? "review" : "ready";

  return {
    state,
    score: clamp(100 - failed * 26 - watches * 8, 0, 100),
    headline: headlineFor(state, failed, watches),
    checks,
    actions: buildActions(state, checks),
    stats,
    symbols,
  };
}

function buildChecks({
  predictions,
  services,
  promotion,
  stats,
  symbols,
}: {
  predictions: Prediction[];
  services: ServicesResponse | null;
  promotion: PromotionStateResponse | null;
  stats: SignalCockpitSummary["stats"];
  symbols: SignalCockpitSymbolRow[];
}): SignalCockpitCheck[] {
  const predictorServices = (services?.services ?? []).filter((service) =>
    ["gbm_predictor", "news_alpha_predictor"].includes(service.name),
  );
  const downPredictors = predictorServices.filter((service) => service.expected && service.status === "down");
  const stalePredictors = predictorServices.filter((service) => service.expected && service.status === "stale");
  const candidateCount = symbols.filter((symbol) => symbol.state === "candidate").length;

  return [
    {
      id: "signal-feed",
      label: "Signal feed",
      severity: predictions.length > 0 ? "pass" : "watch",
      detail: predictions.length > 0 ? `${predictions.length} latest prediction tile(s) in memory.` : "No live prediction frames have arrived in this browser session.",
    },
    {
      id: "freshness",
      label: "Freshness",
      severity: freshnessSeverity(stats.latestAgeSeconds),
      detail: stats.latestAgeSeconds == null ? "No latest prediction timestamp available." : `Latest signal age is ${formatAge(stats.latestAgeSeconds)}.`,
    },
    {
      id: "confidence",
      label: "Confidence mix",
      severity: predictions.length === 0 ? "watch" : stats.avgConfidence >= 0.7 ? "pass" : stats.avgConfidence >= 0.5 ? "watch" : "fail",
      detail: predictions.length === 0 ? "Confidence unavailable until predictions arrive." : `Average confidence ${(stats.avgConfidence * 100).toFixed(0)}%; ${stats.highConfidenceCount} high-confidence tile(s).`,
    },
    {
      id: "consensus",
      label: "Symbol consensus",
      severity: candidateCount > 0 ? "pass" : symbols.length > 0 ? "watch" : "watch",
      detail: candidateCount > 0 ? `${candidateCount} symbol candidate(s) meet confidence and direction thresholds.` : "No symbol has strong consensus yet.",
    },
    {
      id: "predictor-services",
      label: "Predictor services",
      severity: downPredictors.length > 0 ? "fail" : stalePredictors.length > 0 ? "watch" : predictorServices.length > 0 ? "pass" : "watch",
      detail: services ? `${predictorServices.length} predictor service row(s); ${downPredictors.length} down, ${stalePredictors.length} stale.` : "Service heartbeat summary unavailable.",
    },
    {
      id: "model-binding",
      label: "Model binding",
      severity: promotion?.active ? "pass" : "watch",
      detail: promotion?.active ? `Active model binding is ${promotion.active.model_name}.` : "No active model binding reported for the predictor agent.",
    },
    {
      id: "execution-boundary",
      label: "Execution boundary",
      severity: "pass",
      detail: "Cockpit is read-only: no order, OMS, broker, or lifecycle controls are exposed here.",
    },
  ];
}

function buildStats(predictions: Prediction[], nowMs: number): SignalCockpitSummary["stats"] {
  if (predictions.length === 0) {
    return {
      predictionCount: 0,
      agentCount: 0,
      symbolCount: 0,
      avgConfidence: 0,
      highConfidenceCount: 0,
      staleCount: 0,
      latestAgeSeconds: null,
    };
  }
  const latestTs = Math.max(...predictions.map((prediction) => prediction.ts_event));
  return {
    predictionCount: predictions.length,
    agentCount: new Set(predictions.map((prediction) => prediction.agent_id)).size,
    symbolCount: new Set(predictions.map((prediction) => prediction.symbol)).size,
    avgConfidence: predictions.reduce((sum, prediction) => sum + prediction.confidence, 0) / predictions.length,
    highConfidenceCount: predictions.filter((prediction) => prediction.confidence >= 0.7).length,
    staleCount: predictions.filter((prediction) => ageSeconds(prediction.ts_event, nowMs) > 300).length,
    latestAgeSeconds: ageSeconds(latestTs, nowMs),
  };
}

function buildSymbols(predictions: Prediction[]): SignalCockpitSymbolRow[] {
  const groups = new Map<string, Prediction[]>();
  for (const prediction of predictions) {
    const rows = groups.get(prediction.symbol) ?? [];
    rows.push(prediction);
    groups.set(prediction.symbol, rows);
  }
  return Array.from(groups.entries())
    .map(([symbol, rows]) => {
      const avgDirection = rows.reduce((sum, row) => sum + row.direction, 0) / rows.length;
      const avgConfidence = rows.reduce((sum, row) => sum + row.confidence, 0) / rows.length;
      const latestTs = Math.max(...rows.map((row) => row.ts_event));
      const longCount = rows.filter((row) => row.direction >= 0).length;
      const shortCount = rows.length - longCount;
      return {
        symbol,
        count: rows.length,
        avgDirection,
        avgConfidence,
        latestTs,
        longCount,
        shortCount,
        state: symbolState(rows.length, avgDirection, avgConfidence),
      };
    })
    .sort((a, b) => symbolRank(b.state) - symbolRank(a.state) || b.avgConfidence - a.avgConfidence);
}

function symbolState(count: number, avgDirection: number, avgConfidence: number): SignalCockpitSymbolRow["state"] {
  if (count >= 2 && avgConfidence >= 0.7 && Math.abs(avgDirection) >= 0.2) return "candidate";
  if (avgConfidence >= 0.5 || Math.abs(avgDirection) >= 0.15) return "watch";
  return "quiet";
}

function symbolRank(state: SignalCockpitSymbolRow["state"]): number {
  if (state === "candidate") return 2;
  if (state === "watch") return 1;
  return 0;
}

function freshnessSeverity(age: number | null): SignalCockpitSeverity {
  if (age == null) return "watch";
  if (age <= 300) return "pass";
  if (age <= 900) return "watch";
  return "fail";
}

function ageSeconds(tsEvent: number, nowMs: number): number {
  const eventMs = tsEvent > 10_000_000_000_000 ? tsEvent / 1_000_000 : tsEvent * 1000;
  return Math.max(0, (nowMs - eventMs) / 1000);
}

function buildActions(state: SignalCockpitState, checks: SignalCockpitCheck[]): string[] {
  if (state === "ready") return ["Signals are fresh and model-bound; continue read-only monitoring before any downstream review."];
  return checks
    .filter((check) => check.severity !== "pass")
    .map((check) => `${check.label}: ${check.detail}`)
    .slice(0, 6);
}

function headlineFor(state: SignalCockpitState, failed: number, watches: number): string {
  if (state === "blocked") return `${failed} signal blocker${failed === 1 ? "" : "s"} before relying on the feed.`;
  if (state === "review") return `${watches} signal watch item${watches === 1 ? "" : "s"}; keep interpretation caveated.`;
  return "Production signal cockpit is ready for read-only review.";
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
