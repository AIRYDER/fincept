"use client";

import { Badge } from "@/components/ui/badge";
import {
  INTENT_BADGE_VARIANT,
  INTENT_BORDER,
  INTENT_TEXT,
  sourceIntent,
  type SemanticIntent,
} from "@/lib/design-tokens";
import { cn } from "@/lib/utils";

/**
 * EntityHeader — entity header with icon, label, badges, and source indicator.
 *
 * Enforces the design token contract:
 *   - System-sourced entities show cyan "SYSTEM" badge
 *   - Model-sourced entities show purple "AI" badge
 *   - Human-sourced entities show green "HUMAN" badge
 */

export interface EntityHeaderProps {
  /** Entity name / symbol */
  label: string;
  /** Entity type label (e.g. "Strategy", "Symbol", "Model") */
  typeLabel?: string;
  /** Source of the entity data */
  source?: "system" | "model" | "human" | "unknown";
  /** Overall health intent */
  intent?: SemanticIntent;
  /** Optional icon */
  icon?: React.ReactNode;
  /** Additional badges */
  badges?: Array<{ label: string; intent: SemanticIntent }>;
  /** Subtitle / description */
  sub?: string;
  className?: string;
}

export function EntityHeader({
  label,
  typeLabel,
  source,
  intent = "verified",
  icon,
  badges,
  sub,
  className,
}: EntityHeaderProps) {
  const resolvedSourceIntent = source ? sourceIntent(source) : null;

  return (
    <div className={cn("flex items-start gap-3", className)}>
      {icon && (
        <div
          className={cn(
            "shrink-0 rounded-md p-1.5",
            INTENT_BORDER[intent],
            INTENT_TEXT[intent],
          )}
        >
          {icon}
        </div>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm font-semibold">{label}</span>
          {typeLabel && (
            <Badge variant="outline" className="text-[9px]">
              {typeLabel}
            </Badge>
          )}
          {source && resolvedSourceIntent && (
            <Badge variant={INTENT_BADGE_VARIANT[resolvedSourceIntent]} className="text-[9px]">
              {source === "model" ? "AI" : source.toUpperCase()}
            </Badge>
          )}
          {badges?.map((b, i) => (
            <Badge key={i} variant={INTENT_BADGE_VARIANT[b.intent]} className="text-[9px]">
              {b.label}
            </Badge>
          ))}
        </div>
        {sub && (
          <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>
        )}
      </div>
    </div>
  );
}
