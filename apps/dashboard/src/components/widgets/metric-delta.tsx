"use client";

import { Minus, TrendingDown, TrendingUp } from "lucide-react";

import { pnlIntent, INTENT_TEXT, type SemanticIntent } from "@/lib/design-tokens";
import { cn } from "@/lib/utils";

/**
 * MetricDelta — reusable delta display with semantic color.
 *
 * Positive → healthy (green), Negative → critical (red), Zero → inactive (gray).
 * Replaces ad-hoc delta rendering in KpiTile and page-specific code.
 */

export interface MetricDeltaProps {
  /** Numeric delta value */
  value: number | null | undefined;
  /** Show as percentage (default false) */
  pct?: boolean;
  /** Show arrow icon (default true) */
  arrow?: boolean;
  /** Compact mode */
  compact?: boolean;
  /** Override intent (e.g. for non-PnL contexts) */
  intent?: SemanticIntent;
  className?: string;
}

export function MetricDelta({
  value,
  pct = false,
  arrow = true,
  compact = false,
  intent,
  className,
}: MetricDeltaProps) {
  if (value === null || value === undefined) return null;

  const resolvedIntent = intent ?? pnlIntent(value);
  const direction =
    value > 0 ? "up" : value < 0 ? "down" : "flat";
  const Arrow =
    direction === "up"
      ? TrendingUp
      : direction === "down"
        ? TrendingDown
        : Minus;

  const formatted = pct
    ? `${value > 0 ? "+" : ""}${value.toFixed(2)}%`
    : `${value > 0 ? "+" : ""}${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-0.5 font-medium",
        compact ? "text-[10px]" : "text-xs",
        INTENT_TEXT[resolvedIntent],
        className,
      )}
    >
      {arrow && <Arrow className={compact ? "h-2.5 w-2.5" : "h-3 w-3"} />}
      {formatted}
    </span>
  );
}

/** Get the intent for a delta value without rendering. */
export function getDeltaIntent(value: number): SemanticIntent {
  return pnlIntent(value);
}
