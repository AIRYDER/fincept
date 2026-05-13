"use client";

/**
 * StatusDot — a pulsing indicator for a strategy's live state.
 *
 * Three states map onto the operator mental model:
 *
 *   - "live"     -> enabled + runner verified via heartbeat / positions
 *                    (cyan pulse aura).
 *   - "enabled"  -> enabled flag is True but we have no runtime
 *                    evidence yet (dim long halo, no pulse).
 *   - "stopped"  -> enabled flag is False (flat muted dot).
 *
 * We intentionally render a compound (dot + optional aura) rather
 * than colouring a Badge.  A pulse inside a Badge competes visually
 * with the Badge's border; a bare dot + halo reads as a "beacon" at
 * a distance, which is the right metaphor for at-a-glance scanning
 * of a 50-row table.
 */

import { cn } from "@/lib/utils";

export type StrategyLiveState = "live" | "enabled" | "stopped";

export function StatusDot({
  state,
  className,
  title,
}: {
  state: StrategyLiveState;
  className?: string;
  title?: string;
}) {
  const defaultTitle =
    state === "live"
      ? "Live — runner active"
      : state === "enabled"
        ? "Enabled — awaiting runner"
        : "Stopped";
  return (
    <span
      className={cn("relative inline-flex h-2.5 w-2.5", className)}
      title={title ?? defaultTitle}
      aria-label={defaultTitle}
    >
      {state === "live" ? (
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-long/60 opacity-75" />
      ) : null}
      <span
        className={cn(
          "relative inline-flex h-2.5 w-2.5 rounded-full",
          state === "live" && "bg-long shadow-[0_0_8px_hsl(var(--long)/0.9)]",
          state === "enabled" &&
            "bg-long/40 shadow-[0_0_4px_hsl(var(--long)/0.4)]",
          state === "stopped" && "bg-muted-foreground/40",
        )}
      />
    </span>
  );
}
