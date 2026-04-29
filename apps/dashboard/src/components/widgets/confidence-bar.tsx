"use client";

import { cn } from "@/lib/utils";

/**
 * Compact horizontal bar showing a signed direction in [-1, +1].
 * Center at 0; left = short (red), right = long (green).
 * Width is proportional to ``Math.abs(direction)``; opacity scales
 * with confidence.
 */
export function ConfidenceBar({
  direction,
  confidence,
}: {
  direction: number;
  confidence: number;
}) {
  const clamped = Math.max(-1, Math.min(1, direction));
  const widthPct = Math.abs(clamped) * 50;
  const isLong = clamped >= 0;
  return (
    <div className="relative h-2 w-full overflow-hidden rounded-full bg-muted/40">
      <div className="absolute inset-y-0 left-1/2 w-px bg-border/80" />
      <div
        className={cn(
          "absolute inset-y-0 transition-all",
          isLong ? "bg-long left-1/2" : "bg-short right-1/2",
        )}
        style={{
          width: `${widthPct}%`,
          opacity: 0.35 + 0.65 * Math.max(0, Math.min(1, confidence)),
        }}
      />
    </div>
  );
}
