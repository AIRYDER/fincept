import type { ReturnInput } from "./optimizerTypes";

export interface BlackLittermanView {
  ticker: string;
  expectedReturnDeltaPct: number;
  confidence: number;
}

export function applyBlackLittermanViews(
  inputs: ReturnInput[],
  views: BlackLittermanView[],
): { inputs: ReturnInput[]; warnings: string[] } {
  const warnings: string[] = [];
  const byTicker = new Map(views.map((view) => [view.ticker.toUpperCase(), view]));
  const known = new Set(inputs.map((input) => input.ticker));
  for (const view of views) {
    if (!known.has(view.ticker.toUpperCase())) warnings.push(`Ignored Black-Litterman view for unknown ticker ${view.ticker}.`);
  }
  return {
    warnings,
    inputs: inputs.map((input) => {
      const view = byTicker.get(input.ticker);
      if (!view) return input;
      const confidence = Math.max(0, Math.min(1, Number.isFinite(view.confidence) ? view.confidence : 0));
      return {
        ...input,
        expectedReturnPct: round(input.expectedReturnPct + view.expectedReturnDeltaPct * confidence),
        confidence: Math.max(input.confidence * 0.75, confidence),
        source: "black_litterman_view_blend",
      };
    }),
  };
}

function round(value: number): number {
  return Math.round((Number.isFinite(value) ? value : 0) * 100) / 100;
}
