"use client";

import Link from "next/link";
import { useMemo } from "react";

import { LEDDot, type LEDTone } from "@/components/widgets/led-dot";
import { Sparkline } from "@/components/widgets/sparkline";
import { Badge } from "@/components/ui/badge";
import {
  BRAND,
  directionOf,
  formatSignedPct,
  formatSignedUsd,
} from "@/lib/design-tokens";
import { cn, formatNumber, formatUsd } from "@/lib/utils";

/**
 * WatchlistRow — single row in the watchlist table.
 *
 * Visual grammar:
 *   - LEDDot left of the symbol — quick status glance
 *   - Symbol + name on the left
 *   - Last price + intraday change on the right
 *   - Mini sparkline + intraday range as a horizontal track
 *   - Clickable: navigates to /symbol/[symbol]
 *
 * Designed to be densely packed but still scannable — the row
 * height is 40px which matches the rest of the Fincept tables.
 */

export type WatchlistTone = "long" | "short" | "warn" | "info" | "muted";

export interface WatchlistRowProps {
  symbol: string;
  name?: string;
  last: number;
  change: number;
  changePct: number;
  /** Sparkline points — already normalized, ≥ 2 points. */
  sparkline: { x: number; y: number }[];
  /** Optional 24h volume to display under the price. */
  volume?: number | null;
  /** Optional market-cap label (e.g. "MEGA", "MID"). */
  cap?: string | null;
  /** Optional posture tone for the LEDDot. */
  tone?: WatchlistTone;
  /** Optional href; default = `/symbol/{symbol}`. */
  href?: string;
  /** Disclose mock data with a small MOCK chip. */
  isMock?: boolean;
  className?: string;
  /** Optional event handlers (e.g. context menu). */
  onSelect?: () => void;
}

const TONE_TO_LED: Record<WatchlistTone, LEDTone> = {
  long: "long",
  short: "short",
  warn: "warn",
  info: "info",
  muted: "muted",
};

export function WatchlistRow({
  symbol,
  name,
  last,
  change,
  changePct,
  sparkline,
  volume,
  cap,
  tone,
  href,
  isMock = false,
  className,
  onSelect,
}: WatchlistRowProps) {
  const dir = directionOf(change);
  const led = tone ? TONE_TO_LED[tone] : dir === "up" ? "long" : dir === "down" ? "short" : "muted";
  const sparklineDir = dir === "down" ? false : dir === "up";
  const target = href ?? `/symbol/${encodeURIComponent(symbol)}`;

  const range = useMemo(() => {
    if (sparkline.length < 2) return null;
    const ys = sparkline.map((p) => p.y);
    return { min: Math.min(...ys), max: Math.max(...ys) };
  }, [sparkline]);

  return (
    <Link
      href={target}
      onClick={onSelect}
      className={cn(
        "group grid h-10 grid-cols-[14px_minmax(0,1fr)_92px_140px_minmax(110px,160px)] items-center gap-3 border-b border-hairline px-3 transition-colors hover:bg-cobalt/[0.04]",
        className,
      )}
    >
      {/* LEDDot + symbol */}
      <div className="flex items-center gap-2 min-w-0">
        <LEDDot tone={led} pulse={dir !== "flat"} size="sm" />
      </div>
      <div className="flex min-w-0 items-center gap-2">
        <span className="font-mono text-[13px] font-semibold tracking-tight text-foreground group-hover:text-cobalt">
          {symbol}
        </span>
        {name ? (
          <span className="truncate text-[11px] text-muted-foreground">{name}</span>
        ) : null}
        {cap ? (
          <Badge variant="muted" className="px-1 py-0 text-[9px]">
            {cap}
          </Badge>
        ) : null}
        {isMock ? (
          <span className="border border-dashed border-warn/60 bg-warn/10 px-1 text-[8px] font-bold uppercase tracking-widest text-warn">
            MOCK
          </span>
        ) : null}
      </div>

      {/* Last price */}
      <div className="flex flex-col items-end leading-tight">
        <span className="num font-mono text-[13px] font-semibold tabular-nums text-foreground">
          {formatUsd(last)}
        </span>
        {volume ? (
          <span className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground/70">
            V {formatNumber(volume, 0)}
          </span>
        ) : null}
      </div>

      {/* Change + pct */}
      <div className="flex flex-col items-end leading-tight">
        <span
          className={cn(
            "num font-mono text-[12px] font-semibold tabular-nums",
            change > 0 ? "text-long" : change < 0 ? "text-short" : "text-muted-foreground",
          )}
        >
          {formatSignedUsd(change)}
        </span>
        <span
          className={cn(
            "num font-mono text-[10px] tabular-nums",
            changePct > 0 ? "text-long" : changePct < 0 ? "text-short" : "text-muted-foreground",
          )}
        >
          {formatSignedPct(changePct)}
        </span>
      </div>

      {/* Mini sparkline */}
      <div className="relative h-7">
        {sparkline.length > 1 ? (
          <Sparkline data={sparkline} positive={sparklineDir} height={28} />
        ) : (
          <div className="flex h-full items-center justify-end text-[10px] text-muted-foreground">
            —
          </div>
        )}
        {/* Range track under the sparkline */}
        {range ? (
          <div
            className="pointer-events-none absolute inset-x-0 bottom-0 h-[1px] bg-hairline"
            aria-hidden
          />
        ) : null}
      </div>
    </Link>
  );
}

export const WATCHLIST_BRAND = BRAND;
