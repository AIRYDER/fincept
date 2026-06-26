"use client";

import { cn } from "@/lib/utils";

/**
 * LEDDot — small glowing dot used for live status indicators.
 *
 * Larger and more luminous than the legacy `.live-dot` utility:
 *   sm   4px   — inline with text labels
 *   md   6px   — default (pairs with chip labels)
 *   lg   8px   — standalone status row
 *
 * The dot uses `currentColor` for both fill and glow, so it inherits
 * the surrounding text color and theme-aware intent palette.
 */

export type LEDSize = "sm" | "md" | "lg";
export type LEDTone = "long" | "short" | "warn" | "info" | "cyan" | "muted";

const SIZE_CLASS: Record<LEDSize, string> = {
  sm: "h-1 w-1",
  md: "h-1.5 w-1.5",
  lg: "h-2 w-2",
};

const TONE_CLASS: Record<LEDTone, string> = {
  long: "bg-long text-long",
  short: "bg-short text-short",
  warn: "bg-warn text-warn",
  info: "bg-info text-info",
  cyan: "bg-cyan text-cyan",
  muted: "bg-muted-foreground text-muted-foreground",
};

export interface LEDDotProps {
  tone?: LEDTone;
  size?: LEDSize;
  /** Pulses softly to indicate "live" */
  pulse?: boolean;
  className?: string;
  title?: string;
}

export function LEDDot({
  tone = "long",
  size = "md",
  pulse = false,
  className,
  title,
}: LEDDotProps) {
  return (
    <span
      title={title}
      aria-label={title}
      className={cn(
        "inline-block rounded-full",
        SIZE_CLASS[size],
        TONE_CLASS[tone],
        pulse && "animate-pulse",
        className,
      )}
      style={{
        boxShadow: `0 0 6px currentColor, 0 0 1px currentColor`,
      }}
    />
  );
}

/**
 * DotMatrix — small text label rendered in the dot-matrix style:
 *   - tabular-nums
 *   - extra letter-spacing
 *   - subtle left/right fade for ticker feel
 *
 * Used for last-trade tape, ticker bars, and other high-density
 * numeric strips.
 */
export function DotMatrix({
  children,
  className,
  fade = true,
}: {
  children: React.ReactNode;
  className?: string;
  fade?: boolean;
}) {
  return (
    <span
      className={cn(
        "dot-matrix font-mono uppercase tracking-wider text-foreground/90",
        className,
      )}
      style={
        fade
          ? {
              maskImage:
                "linear-gradient(90deg, transparent 0, #000 8%, #000 92%, transparent 100%)",
              WebkitMaskImage:
                "linear-gradient(90deg, transparent 0, #000 8%, #000 92%, transparent 100%)",
            }
          : undefined
      }
    >
      {children}
    </span>
  );
}
