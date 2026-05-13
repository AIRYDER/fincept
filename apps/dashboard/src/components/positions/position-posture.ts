import type { SemanticIntent } from "@/lib/design-tokens";
import type { Position } from "@/lib/types";

export type PositionSide = "long" | "short" | "flat";

export interface PositionPosture {
  side: PositionSide;
  sideLabel: "LONG" | "SHORT" | "FLAT";
  sideIntent: SemanticIntent;
  pnlIntent: SemanticIntent;
  freshnessAgeSec: number | null;
  exposure: number;
  markSource: "live" | "implied" | "cost";
}

function asNum(value: string | number | null | undefined): number {
  if (value === null || value === undefined) return 0;
  const n = typeof value === "string" ? Number(value) : value;
  return Number.isFinite(n) ? n : 0;
}

export function buildPositionPosture(
  position: Position,
  nowMs: number = Date.now(),
): PositionPosture {
  const qty = asNum(position.quantity);
  const avgCost = asNum(position.avg_cost);
  const unrealized = asNum(position.unrealized_pnl);
  const realized = asNum(position.realized_pnl);
  const side: PositionSide = qty > 0 ? "long" : qty < 0 ? "short" : "flat";
  const updatedAt = asNum(position.updated_at);
  const freshnessAgeSec = updatedAt > 0 ? Math.max(0, nowMs / 1000 - updatedAt) : null;

  return {
    side,
    sideLabel: side === "long" ? "LONG" : side === "short" ? "SHORT" : "FLAT",
    sideIntent: side === "long" ? "healthy" : side === "short" ? "critical" : "inactive",
    pnlIntent: unrealized + realized > 0 ? "healthy" : unrealized + realized < 0 ? "critical" : "inactive",
    freshnessAgeSec,
    exposure: Math.abs(qty) * avgCost,
    markSource: position.mark_px ? "live" : qty !== 0 ? "implied" : "cost",
  };
}
