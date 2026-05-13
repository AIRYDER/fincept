/**
 * design-tokens — semantic color contract for the Fincept Terminal.
 *
 * Enforces consistent meaning across all UI surfaces:
 *
 *   Cyan   = verified / system truth / live data
 *   Amber  = degraded / experimental / caveat / stale
 *   Red    = critical / blocking / risk / rejected
 *   Purple = AI / model-generated / predicted
 *   Green  = profitable / healthy / long / passed
 *   Gray   = inactive / stale / unknown / flat
 *
 * Every component that renders status, health, or evidence should use
 * these tokens instead of hard-coding Tailwind classes.  This ensures
 * colors mean the same thing everywhere and AI output is visually
 * distinct from verified system state.
 */

// ---------------------------------------------------------------------------
// Semantic intent
// ---------------------------------------------------------------------------

export type SemanticIntent =
  | "verified"    // cyan — system truth, live, confirmed
  | "degraded"    // amber — experimental, stale, partial, caveat
  | "critical"    // red — blocking, risk, rejected, kill
  | "ai"          // purple — model-generated, predicted, shadow
  | "healthy"     // green — profitable, long, passed, ok
  | "inactive";   // gray — flat, unknown, disabled, stale

// ---------------------------------------------------------------------------
// Tailwind class maps
// ---------------------------------------------------------------------------

export const INTENT_TEXT: Record<SemanticIntent, string> = {
  verified:  "text-cyan",
  degraded:  "text-amber",
  critical:  "text-short",
  ai:        "text-purple-400",
  healthy:   "text-long",
  inactive:  "text-muted-foreground",
};

export const INTENT_BG: Record<SemanticIntent, string> = {
  verified:  "bg-cyan/10",
  degraded:  "bg-amber/10",
  critical:  "bg-short/10",
  ai:        "bg-purple-400/10",
  healthy:   "bg-long/10",
  inactive:  "bg-muted/10",
};

export const INTENT_BORDER: Record<SemanticIntent, string> = {
  verified:  "border-cyan/40",
  degraded:  "border-amber/40",
  critical:  "border-short/40",
  ai:        "border-purple-400/40",
  healthy:   "border-long/40",
  inactive:  "border-muted/40",
};

export const INTENT_DOT: Record<SemanticIntent, string> = {
  verified:  "bg-cyan",
  degraded:  "bg-amber",
  critical:  "bg-short",
  ai:        "bg-purple-400",
  healthy:   "bg-long",
  inactive:  "bg-muted-foreground",
};

// ---------------------------------------------------------------------------
// Badge variant mapping
// ---------------------------------------------------------------------------

import type { BadgeProps } from "@/components/ui/badge";

export const INTENT_BADGE_VARIANT: Record<SemanticIntent, NonNullable<BadgeProps["variant"]>> = {
  verified:  "secondary",
  degraded:  "warn",
  critical:  "destructive",
  ai:        "outline",
  healthy:   "long",
  inactive:  "muted",
};

// ---------------------------------------------------------------------------
// Intent inference helpers
// ---------------------------------------------------------------------------

/** Map a boolean health/ok state to an intent. */
export function healthIntent(ok: boolean, stale?: boolean): SemanticIntent {
  if (ok && !stale) return "verified";
  if (stale) return "degraded";
  return "critical";
}

/** Map a PnL or return sign to an intent. */
export function pnlIntent(value: number): SemanticIntent {
  if (value > 0) return "healthy";
  if (value < 0) return "critical";
  return "inactive";
}

/** Map a model/AI source flag to an intent. */
export function sourceIntent(source: "system" | "model" | "human" | "unknown"): SemanticIntent {
  if (source === "system") return "verified";
  if (source === "model") return "ai";
  if (source === "human") return "healthy";
  return "inactive";
}

/** Map a freshness age in seconds to an intent. */
export function freshnessIntent(ageSec: number, staleAfterSec = 300, deadAfterSec = 3600): SemanticIntent {
  if (ageSec < staleAfterSec) return "verified";
  if (ageSec < deadAfterSec) return "degraded";
  return "critical";
}

/** Map a generic severity string to an intent. */
export function severityIntent(severity: "info" | "warning" | "critical" | "ok"): SemanticIntent {
  if (severity === "ok") return "healthy";
  if (severity === "info") return "verified";
  if (severity === "warning") return "degraded";
  return "critical";
}
