"use client";

import { motion } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  Brain,
  ChevronRight,
  type LucideIcon,
  Minus,
  Sparkles,
  Target,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";

import { LEDDot } from "@/components/widgets/led-dot";
import { Badge } from "@/components/ui/badge";
import { cn, formatNumber } from "@/lib/utils";

/**
 * SignalCard — compact instrument-panel card for predictions, alerts,
 * and other model output.
 *
 * Variants:
 *   - prediction  (default) — model direction + confidence
 *   - alert                — severity-tagged system message
 *   - signal               — generic directional event
 *
 * Every card carries an LEDDot, an icon, a source badge (AI / SYSTEM /
 * HUMAN), and a chevron that implies "click for more".
 */

export type SignalKind = "prediction" | "alert" | "signal";

export interface SignalCardProps {
  kind?: SignalKind;
  title: string;
  symbol?: string;
  /** Direction in [-1, +1] — drives the long/short bar. */
  direction?: number | null;
  /** Confidence in [0, 1] — drives opacity / prominence. */
  confidence?: number | null;
  /** Source of the signal */
  source?: "system" | "model" | "human" | "unknown";
  /** Severity (alerts only) */
  severity?: "info" | "warning" | "critical";
  /** Optional subtitle / context line */
  context?: string;
  /** Optional right-side metric (e.g. target price, pnl) */
  metric?: { label: string; value: string; tone?: "long" | "short" | "neutral" };
  /** Optional href to navigate to on click */
  href?: string;
  /** Optional timestamp in nanoseconds */
  ts?: number;
  /** Mark this card as mock/seed data */
  isMock?: boolean;
  className?: string;
}

const SOURCE_BADGE: Record<NonNullable<SignalCardProps["source"]>, {
  label: string;
  variant: "default" | "long" | "muted";
  cls: string;
}> = {
  system: { label: "SYSTEM", variant: "default", cls: "border-cyan/40 text-cyan" },
  model: { label: "AI", variant: "muted", cls: "border-warn/40 text-warn" },
  human: { label: "HUMAN", variant: "long", cls: "border-long/40 text-long" },
  unknown: { label: "—", variant: "muted", cls: "border-border text-muted-foreground" },
};

const SEVERITY_BADGE: Record<NonNullable<SignalCardProps["severity"]>, {
  label: string;
  cls: string;
  led: "short" | "warn" | "info";
}> = {
  info: { label: "INFO", cls: "border-info/40 text-info", led: "info" },
  warning: { label: "WARN", cls: "border-warn/40 text-warn", led: "warn" },
  critical: { label: "CRIT", cls: "border-short/50 text-short", led: "short" },
};

const KIND_ICON: Record<SignalKind, LucideIcon> = {
  prediction: Sparkles,
  alert: AlertTriangle,
  signal: Activity,
};

const KIND_LABEL: Record<SignalKind, string> = {
  prediction: "PREDICTION",
  alert: "ALERT",
  signal: "SIGNAL",
};

function ago(ns?: number): string {
  if (!ns) return "—";
  const d = new Date(ns / 1_000_000);
  const diffSec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (diffSec < 60) return `${diffSec}s`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  return `${Math.floor(diffSec / 86400)}d`;
}

export function SignalCard({
  kind = "prediction",
  title,
  symbol,
  direction = null,
  confidence = null,
  source = "model",
  severity = "info",
  context,
  metric,
  href,
  ts,
  isMock = false,
  className,
}: SignalCardProps) {
  const Icon = KIND_ICON[kind];
  const sourceMeta = SOURCE_BADGE[source];
  const sevMeta = SEVERITY_BADGE[severity];

  // Direction / confidence bar geometry
  const dir = direction ?? 0;
  const conf = confidence ?? 0;
  const widthPct = Math.abs(dir) * 50;
  const isLong = dir >= 0;
  const ArrowIcon = isLong ? ArrowUpRight : ArrowDownRight;
  const DirTrend = isLong ? TrendingUp : TrendingDown;

  const ledTone =
    kind === "alert"
      ? sevMeta.led
      : isLong
        ? "long"
        : dir < 0
          ? "short"
          : "muted";

  const body = (
    <motion.div
      whileHover={href ? { y: -1 } : undefined}
      transition={{ duration: 0.15 }}
      className={cn(
        "group relative flex flex-col gap-2 overflow-hidden border border-hairline bg-card/60 p-3 transition-colors",
        "hover:border-cobalt-soft hover:bg-card/80",
        kind === "alert" && severity === "critical" && "border-short/40 bg-short/[0.04]",
        className,
      )}
    >
      {/* Top hairline accent (cobalt for normal, short for critical alerts) */}
      <span
        className={cn(
          "pointer-events-none absolute inset-x-0 top-0 h-px",
          kind === "alert" && severity === "critical"
            ? "bg-short/60"
            : kind === "alert" && severity === "warning"
              ? "bg-warn/60"
              : "bg-cobalt/40",
        )}
      />

      <div className="flex items-start gap-2">
        <div
          className={cn(
            "flex h-6 w-6 shrink-0 items-center justify-center border",
            kind === "alert" && severity === "critical"
              ? "border-short/40 bg-short/10 text-short"
              : kind === "alert" && severity === "warning"
                ? "border-warn/40 bg-warn/10 text-warn"
                : "border-cobalt-soft bg-cobalt/10 text-cobalt",
          )}
        >
          <Icon className="h-3 w-3" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
              {KIND_LABEL[kind]}
            </span>
            {kind === "alert" ? (
              <Badge variant="outline" className={cn("h-4 px-1 text-[9px]", sevMeta.cls)}>
                {sevMeta.label}
              </Badge>
            ) : (
              <Badge variant="outline" className={cn("h-4 px-1 text-[9px]", sourceMeta.cls)}>
                {sourceMeta.label}
              </Badge>
            )}
            {isMock ? (
              <span className="border border-dashed border-warn/60 bg-warn/10 px-1 text-[8px] font-bold uppercase tracking-widest text-warn">
                MOCK
              </span>
            ) : null}
          </div>
          <div className="mt-0.5 flex items-baseline gap-1.5">
            {symbol ? (
              <span className="font-mono text-sm font-semibold tracking-tight text-foreground">
                {symbol}
              </span>
            ) : null}
            <span className="truncate text-xs text-foreground/90">{title}</span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <LEDDot tone={ledTone} size="md" pulse />
          {href ? (
            <ChevronRight className="h-3 w-3 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
          ) : null}
        </div>
      </div>

      {/* Direction bar + numeric line */}
      {direction !== null ? (
        <div className="flex items-center gap-2">
          <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-muted/40">
            <div className="absolute inset-y-0 left-1/2 w-px bg-hairline" />
            <div
              className={cn(
                "absolute inset-y-0 transition-all",
                isLong ? "bg-long left-1/2" : "bg-short right-1/2",
              )}
              style={{
                width: `${widthPct}%`,
                opacity: 0.35 + 0.55 * Math.max(0, Math.min(1, conf)),
              }}
            />
          </div>
          <span
            className={cn(
              "inline-flex shrink-0 items-center gap-0.5 font-mono text-[10px] font-semibold tabular-nums",
              isLong ? "text-long" : "text-short",
            )}
          >
            <DirTrend className="h-3 w-3" />
            {(dir >= 0 ? "+" : "").concat(dir.toFixed(2))}
          </span>
        </div>
      ) : null}

      {/* Bottom row: context + metric + ago */}
      <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
        <span className="truncate">{context ?? "\u00a0"}</span>
        <span className="flex shrink-0 items-center gap-2">
          {metric ? (
            <span
              className={cn(
                "font-mono font-semibold tabular-nums",
                metric.tone === "long" && "text-long",
                metric.tone === "short" && "text-short",
                !metric.tone || metric.tone === "neutral"
                  ? "text-foreground"
                  : "",
              )}
            >
              {metric.label} {metric.value}
            </span>
          ) : null}
          {confidence !== null ? (
            <span className="font-mono text-muted-foreground">
              conf {Math.round(conf * 100)}%
            </span>
          ) : null}
          {ts ? <span className="font-mono">{ago(ts)} ago</span> : null}
        </span>
      </div>
    </motion.div>
  );

  if (href) {
    return (
      <Link href={href} className="block">
        {body}
      </Link>
    );
  }
  return body;
}

/**
 * Compact signal summary strip — used in tight spaces like the
 * symbol detail header.  Shows just LED + direction + symbol.
 */
export function SignalStrip({
  direction,
  confidence,
  symbol,
  className,
}: {
  direction: number;
  confidence: number;
  symbol: string;
  className?: string;
}) {
  const isLong = direction >= 0;
  const Icon = isLong ? ArrowUpRight : dirNeutral(direction);
  return (
    <div
      className={cn(
        "flex items-center gap-2 border border-hairline bg-card/60 px-2 py-1",
        className,
      )}
    >
      <LEDDot tone={isLong ? "long" : "short"} pulse />
      <span className="font-mono text-xs font-semibold">{symbol}</span>
      <span
        className={cn(
          "inline-flex items-center gap-0.5 font-mono text-[10px] font-semibold tabular-nums",
          isLong ? "text-long" : "text-short",
        )}
      >
        <Icon className="h-3 w-3" />
        {direction >= 0 ? "+" : ""}
        {formatNumber(direction, 2)}
      </span>
      <span className="ml-auto font-mono text-[10px] text-muted-foreground">
        {Math.round(confidence * 100)}%
      </span>
    </div>
  );
}

function dirNeutral(direction: number) {
  if (direction > 0) return ArrowUpRight;
  if (direction < 0) return ArrowDownRight;
  return Minus;
}
