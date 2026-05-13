"use client";

import { Badge } from "@/components/ui/badge";
import {
  INTENT_BADGE_VARIANT,
  INTENT_DOT,
  INTENT_TEXT,
  type SemanticIntent,
} from "@/lib/design-tokens";
import { cn } from "@/lib/utils";

/**
 * StatusPill — semantic status indicator with dot + label.
 *
 * Uses the design token contract so colors always mean the same thing:
 *   verified=cyan, degraded=amber, critical=red, ai=purple, healthy=green, inactive=gray
 */

export interface StatusPillProps {
  intent: SemanticIntent;
  label: string;
  /** Show a pulsing dot (default true) */
  dot?: boolean;
  /** Compact mode — smaller text */
  compact?: boolean;
  className?: string;
}

export function StatusPill({
  intent,
  label,
  dot = true,
  compact = false,
  className,
}: StatusPillProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5",
        compact ? "text-[10px]" : "text-xs",
        INTENT_TEXT[intent],
        className,
      )}
    >
      {dot && (
        <span
          className={cn(
            "shrink-0 rounded-full",
            compact ? "h-1.5 w-1.5" : "h-2 w-2",
            INTENT_DOT[intent],
            intent === "verified" && "animate-pulse",
          )}
        />
      )}
      <span className="font-medium uppercase tracking-wider">{label}</span>
    </span>
  );
}

/**
 * StatusPillBadge — same as StatusPill but rendered as a Badge.
 */
export function StatusPillBadge({
  intent,
  label,
  className,
}: {
  intent: SemanticIntent;
  label: string;
  className?: string;
}) {
  return (
    <Badge variant={INTENT_BADGE_VARIANT[intent]} className={cn("text-[10px]", className)}>
      {label}
    </Badge>
  );
}
