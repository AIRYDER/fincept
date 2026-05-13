"use client";

import { Clock } from "lucide-react";

import {
  freshnessIntent,
  INTENT_TEXT,
  type SemanticIntent,
} from "@/lib/design-tokens";
import { cn } from "@/lib/utils";

/**
 * FreshnessBadge — shows data freshness with semantic color.
 *
 *   < 5 min → verified (cyan)
 *   < 1 hr  → degraded (amber)
 *   > 1 hr  → critical (red)
 */

export interface FreshnessBadgeProps {
  /** Age of the data in seconds */
  ageSec: number | null;
  /** Stale threshold in seconds (default 300 = 5 min) */
  staleAfterSec?: number;
  /** Dead threshold in seconds (default 3600 = 1 hr) */
  deadAfterSec?: number;
  /** Label prefix (default "Last") */
  prefix?: string;
  /** Compact mode */
  compact?: boolean;
  className?: string;
}

export function FreshnessBadge({
  ageSec,
  staleAfterSec = 300,
  deadAfterSec = 3600,
  prefix = "Last",
  compact = false,
  className,
}: FreshnessBadgeProps) {
  if (ageSec === null || ageSec === undefined) {
    return (
      <span className={cn("inline-flex items-center gap-1 text-muted-foreground", compact ? "text-[10px]" : "text-xs", className)}>
        <Clock className={compact ? "h-2.5 w-2.5" : "h-3 w-3"} />
        No data
      </span>
    );
  }

  const intent = freshnessIntent(ageSec, staleAfterSec, deadAfterSec);

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1",
        compact ? "text-[10px]" : "text-xs",
        INTENT_TEXT[intent],
        className,
      )}
    >
      <Clock className={compact ? "h-2.5 w-2.5" : "h-3 w-3"} />
      {prefix} {formatAge(ageSec)}
    </span>
  );
}

/** Get the intent for a given age without rendering. */
export function getFreshnessIntent(
  ageSec: number | null,
  staleAfterSec = 300,
  deadAfterSec = 3600,
): SemanticIntent {
  if (ageSec === null || ageSec === undefined) return "inactive";
  return freshnessIntent(ageSec, staleAfterSec, deadAfterSec);
}

// ---------------------------------------------------------------------------
// Age formatter
// ---------------------------------------------------------------------------

function formatAge(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}
