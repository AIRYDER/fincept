import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Format a number as USD currency, with sign-aware coloring helpers
 * elsewhere.  Compact mode collapses to "1.2K"/"3.4M" for KPI tiles.
 */
export function formatUsd(
  value: number | string | null | undefined,
  opts: { compact?: boolean; signed?: boolean } = {},
): string {
  if (value === null || value === undefined) return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "—";
  const formatter = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: opts.compact ? "compact" : "standard",
    maximumFractionDigits: opts.compact ? 1 : 2,
    minimumFractionDigits: opts.compact ? 0 : 2,
    signDisplay: opts.signed ? "exceptZero" : "auto",
  });
  return formatter.format(n);
}

export function formatNumber(
  value: number | string | null | undefined,
  fractionDigits = 4,
): string {
  if (value === null || value === undefined) return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US", {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: 0,
  });
}

export function formatPercent(
  value: number | null | undefined,
  fractionDigits = 2,
): string {
  if (value === null || value === undefined || !Number.isFinite(value))
    return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(fractionDigits)}%`;
}

/**
 * Convert a UTC nanosecond timestamp (Pydantic ts_event) to a Date.
 */
export function nsToDate(ns: number | string | null | undefined): Date | null {
  if (ns === null || ns === undefined) return null;
  const n = typeof ns === "string" ? Number(ns) : ns;
  if (!Number.isFinite(n)) return null;
  return new Date(n / 1_000_000);
}

export function pnlClass(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "text-muted-foreground";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n) || n === 0) return "text-muted-foreground";
  return n > 0 ? "text-long" : "text-short";
}
