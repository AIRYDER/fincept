"use client";

import { FlaskConical } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * MockBadge — unmistakable "MOCK" marker.
 *
 * Place this on any panel, header, or row that is rendering data
 * that is not yet backed by a real API.  It uses a dashed amber
 * border + small flask icon so it stands out from the regular
 * `Badge` vocabulary.
 *
 * Sizes:
 *   default  — chip in a page header
 *   inline   — inside a table row
 *   corner   — overlay a chart/panel
 */

export type MockBadgeSize = "default" | "inline" | "corner";

export interface MockBadgeProps {
  /** Short source label, e.g. "Seed demo" (optional) */
  source?: string;
  /** Optional ticket / issue id, e.g. "FIN-1234" */
  ticket?: string;
  size?: MockBadgeSize;
  className?: string;
}

export function MockBadge({ source, ticket, size = "default", className }: MockBadgeProps) {
  const tooltip = [
    "Mock data — not backed by a real API",
    source ? `· Source: ${source}` : null,
    ticket ? `· See ${ticket}` : null,
  ]
    .filter(Boolean)
    .join(" ");

  if (size === "corner") {
    return (
      <span
        title={tooltip}
        className={cn(
          "pointer-events-auto inline-flex items-center gap-1 border border-dashed border-warn/60 bg-warn/10 px-1.5 py-[1px] text-[9px] font-bold uppercase tracking-widest text-warn",
          className,
        )}
      >
        <FlaskConical className="h-2.5 w-2.5" />
        MOCK
      </span>
    );
  }

  const sizing =
    size === "inline"
      ? "text-[9px] px-1.5 py-[1px]"
      : "text-[10px] px-2 py-[2px]";

  return (
    <span
      title={tooltip}
      className={cn(
        "inline-flex items-center gap-1 border border-dashed border-warn/60 bg-warn/10 font-bold uppercase tracking-widest text-warn",
        sizing,
        className,
      )}
    >
      <FlaskConical className="h-2.5 w-2.5" />
      Mock{source ? ` · ${source}` : ""}
    </span>
  );
}
