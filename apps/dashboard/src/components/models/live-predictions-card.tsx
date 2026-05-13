"use client";

/**
 * LivePredictionsCard — compact "what is this model producing right now"
 * panel for the /models/[name] detail page (Phase D2).
 *
 * Two queries with the same poll cadence:
 *
 *   1. ``modelPredictionStats`` — count + mean confidence + long/short
 *      distribution.  Drives the four KPI tiles up top.
 *   2. ``modelPredictions``     — last 20 rows for the tail table.
 *
 * The card renders even when the model has emitted nothing (the stats
 * endpoint returns zeros and the table shows an empty hint).  This is
 * intentional: an operator who just promoted a candidate model wants
 * to see a panel that says "0 predictions so far" rather than nothing
 * at all -- it's how they confirm hot-reload took effect.
 *
 * Performance / scaling caveats:
 *   * The api caps ``limit`` at 1000; we ask for 20.
 *   * Stats query reads the whole JSONL.  At ~150 bytes/row and a
 *     daily-rotated file, this is fine for the 10s poll cadence.
 *   * If/when the JSONL crosses 100MB, the api side will need a
 *     "since_ns" default; the dashboard already supports that field.
 */

import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowDownRight, ArrowRightLeft, ArrowUpRight, Hash } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

interface Props {
  /** Model name (from the page route). */
  modelName: string;
  /** Defaults to ``gbm_predictor.v1`` -- the only model-backed agent today. */
  agentId?: string;
}

const DEFAULT_AGENT = "gbm_predictor.v1";
const TAIL_LIMIT = 20;
const POLL_INTERVAL_MS = 30_000;

export function LivePredictionsCard({ modelName, agentId }: Props) {
  const token = useAuth((s) => s.token);
  const aid = agentId ?? DEFAULT_AGENT;

  const stats = useQuery({
    queryKey: ["models", "prediction-stats", modelName, aid],
    queryFn: () =>
      api.modelPredictionStats(token, modelName, { agent_id: aid }),
    enabled: !!token && !!modelName,
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: 15_000,
  });

  const tail = useQuery({
    queryKey: ["models", "predictions", modelName, aid],
    queryFn: () =>
      api.modelPredictions(token, modelName, {
        agent_id: aid,
        limit: TAIL_LIMIT,
      }),
    enabled: !!token && !!modelName,
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: 15_000,
  });

  const s = stats.data?.stats;
  const rows = tail.data?.predictions ?? [];

  return (
    <Card className="mt-6">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Activity className="h-4 w-4 text-primary" />
          Live predictions
        </CardTitle>
        <CardDescription>
          Predictions emitted by{" "}
          <code className="font-mono text-[11px]">{aid}</code> while this
          model is the active binding.  Polled every {POLL_INTERVAL_MS / 1000}
          {"s"}; settlement-based hit-rate / Brier score lands in Phase E.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {/* KPI tiles */}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <KpiTile
            icon={Hash}
            label="Count"
            value={s ? formatInt(s.count) : "--"}
          />
          <KpiTile
            icon={ArrowRightLeft}
            label="Mean confidence"
            value={s ? formatPct(s.mean_confidence) : "--"}
          />
          <KpiTile
            icon={ArrowUpRight}
            label="Long"
            value={s ? formatInt(s.long_count) : "--"}
            tone="long"
            sub={s && s.count > 0 ? formatPct(s.long_count / s.count) : undefined}
          />
          <KpiTile
            icon={ArrowDownRight}
            label="Short"
            value={s ? formatInt(s.short_count) : "--"}
            tone="short"
            sub={s && s.count > 0 ? formatPct(s.short_count / s.count) : undefined}
          />
        </div>

        {/* Tail table */}
        <div className="rounded-md border border-border/40 bg-background/30">
          <div className="grid grid-cols-12 border-b border-border/40 px-3 py-2 text-[10px] font-mono uppercase tracking-widest text-muted-foreground">
            <div className="col-span-3">Time</div>
            <div className="col-span-3">Symbol</div>
            <div className="col-span-3">Direction</div>
            <div className="col-span-3 text-right">Confidence</div>
          </div>
          {rows.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              {stats.isLoading || tail.isLoading
                ? "Loading…"
                : "No predictions recorded yet.  The agent records one row per published prediction."}
            </div>
          ) : (
            <ul className="divide-y divide-border/30">
              {rows.map((r) => (
                <li
                  key={r.id}
                  className="grid grid-cols-12 items-center px-3 py-1.5 text-xs hover:bg-accent/20"
                >
                  <span className="col-span-3 font-mono text-[11px] text-muted-foreground">
                    {formatRelativeTime(r.ts_recorded)}
                  </span>
                  <span className="col-span-3 font-mono text-[11px]">
                    {r.symbol}
                  </span>
                  <span className="col-span-3">
                    <DirectionBadge direction={r.direction} />
                  </span>
                  <span className="col-span-3 text-right font-mono text-[11px]">
                    {formatPct(r.confidence)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// Subcomponents + helpers                                                    //
// --------------------------------------------------------------------------- //

function KpiTile({
  icon: Icon,
  label,
  value,
  tone,
  sub,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  tone?: "long" | "short";
  sub?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-md border border-border/40 bg-background/30 px-3 py-2",
      )}
    >
      <div className="flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-widest text-muted-foreground">
        <Icon className="h-3 w-3" />
        {label}
      </div>
      <div
        className={cn(
          "font-mono text-lg font-bold leading-tight",
          tone === "long" && "text-long",
          tone === "short" && "text-short",
        )}
      >
        {value}
      </div>
      {sub ? (
        <div className="text-[10px] font-mono text-muted-foreground">{sub}</div>
      ) : null}
    </div>
  );
}

function DirectionBadge({ direction }: { direction: number }) {
  if (direction > 0) {
    return (
      <Badge
        variant="long"
        className="font-mono text-[10px] uppercase tracking-wider"
      >
        Long {formatSigned(direction)}
      </Badge>
    );
  }
  if (direction < 0) {
    return (
      <Badge
        variant="short"
        className="font-mono text-[10px] uppercase tracking-wider"
      >
        Short {formatSigned(direction)}
      </Badge>
    );
  }
  return (
    <Badge
      variant="muted"
      className="font-mono text-[10px] uppercase tracking-wider"
    >
      Flat
    </Badge>
  );
}

function formatInt(n: number): string {
  return new Intl.NumberFormat("en-US").format(n);
}

function formatPct(n: number): string {
  return `${(n * 100).toFixed(1)}%`;
}

function formatSigned(n: number): string {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}`;
}

/** Relative time formatter for ns-since-epoch timestamps. */
function formatRelativeTime(tsNs: number): string {
  const tsMs = tsNs / 1_000_000;
  const deltaSec = (Date.now() - tsMs) / 1000;
  if (deltaSec < 1) return "just now";
  if (deltaSec < 60) return `${Math.floor(deltaSec)}s ago`;
  if (deltaSec < 3600) return `${Math.floor(deltaSec / 60)}m ago`;
  if (deltaSec < 86400) return `${Math.floor(deltaSec / 3600)}h ago`;
  return `${Math.floor(deltaSec / 86400)}d ago`;
}
