"use client";

import {
  AlertTriangle,
  Clock,
  Database,
  Eye,
  FileQuestion,
  KeyRound,
  Loader2,
  ShieldAlert,
  Wifi,
} from "lucide-react";

import { StatusPill } from "@/components/widgets/status-pill";
import {
  healthIntent,
  severityIntent,
  INTENT_BORDER,
  INTENT_TEXT,
} from "@/lib/design-tokens";
import { cn } from "@/lib/utils";

import {
  type PageState,
  type PageStateType,
} from "./page-state";

// ---------------------------------------------------------------------------
// Icon + intent map
// ---------------------------------------------------------------------------

const STATE_ICON: Record<PageStateType, React.ComponentType<{ className?: string }>> = {
  empty: FileQuestion,
  loading: Loader2,
  auth: KeyRound,
  provider: Database,
  stale: Clock,
  partial: Wifi,
  fatal: AlertTriangle,
  demo: Eye,
  ok: ShieldAlert,
};

const STATE_INTENT: Record<PageStateType, "verified" | "degraded" | "critical" | "ai" | "healthy" | "inactive"> = {
  empty: "inactive",
  loading: "inactive",
  auth: "critical",
  provider: "degraded",
  stale: "degraded",
  partial: "degraded",
  fatal: "critical",
  demo: "ai",
  ok: "healthy",
};

// ---------------------------------------------------------------------------
// PageStatePanel
// ---------------------------------------------------------------------------

export interface PageStatePanelProps {
  state: PageState;
  /** Compact mode — smaller, inline */
  compact?: boolean;
  className?: string;
}

export function PageStatePanel({ state, compact = false, className }: PageStatePanelProps) {
  // Don't render anything for ok state
  if (state.type === "ok") return null;

  const Icon = STATE_ICON[state.type];
  const intent = STATE_INTENT[state.type];

  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed text-center",
        INTENT_BORDER[intent],
        compact ? "p-4" : "p-8",
        className,
      )}
    >
      <div className={cn("rounded-full p-3", INTENT_BORDER[intent], INTENT_TEXT[intent])}>
        <Icon
          className={cn(
            compact ? "h-4 w-4" : "h-6 w-6",
            state.type === "loading" && "animate-spin",
          )}
        />
      </div>
      <div>
        <div className="flex items-center justify-center gap-2">
          <h3 className={cn("font-medium", compact ? "text-xs" : "text-sm")}>
            {state.label}
          </h3>
          <StatusPill intent={intent} label={state.type.toUpperCase()} compact dot />
        </div>
        <p className={cn("mt-1 max-w-md text-muted-foreground", compact ? "text-[10px]" : "text-xs")}>
          {state.description}
        </p>
      </div>
      {state.remediation && (
        <p className={cn("text-muted-foreground", compact ? "text-[10px]" : "text-xs")}>
          → {state.remediation}
        </p>
      )}
      {/* Demo data always labeled */}
      {state.type === "demo" && (
        <div className={cn("rounded border border-purple-400/30 bg-purple-400/5 px-3 py-1.5 text-purple-400", compact ? "text-[10px]" : "text-xs")}>
          ⚠ This is sample data, not live production data
        </div>
      )}
      {/* Stale data includes timestamp */}
      {state.type === "stale" && state.lastOkAt && (
        <div className="text-[10px] text-muted-foreground">
          Last fresh: {new Date(state.lastOkAt * 1000).toISOString()}
        </div>
      )}
      {/* Provider error detail */}
      {state.type === "provider" && state.errorDetail && (
        <div className="text-[10px] text-amber">
          Error: {state.errorDetail}
        </div>
      )}
      {/* Fatal error detail */}
      {state.type === "fatal" && state.errorDetail && (
        <div className="text-[10px] text-short">
          Error: {state.errorDetail}
        </div>
      )}
      {/* Partial data shows what's available */}
      {state.type === "partial" && state.missingParts && (
        <div className="text-[10px] text-muted-foreground">
          Missing: {state.missingParts.join(", ")}
        </div>
      )}
    </div>
  );
}
