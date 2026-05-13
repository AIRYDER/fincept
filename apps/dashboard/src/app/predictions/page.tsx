"use client";

import {
  Activity,
  Crosshair,
  Filter,
  RadioTower,
  Sparkles,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";
import { formatDistanceToNowStrict } from "date-fns";

import { ProductionSignalCockpit } from "@/components/predictions/production-signal-cockpit";
import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ConfidenceBar } from "@/components/widgets/confidence-bar";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { ScrollArea } from "@/components/ui/scroll-area";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Prediction, WsFrame } from "@/lib/types";
import { cn, nsToDate } from "@/lib/utils";
import { useFinceptStream } from "@/lib/ws";

interface AgentSymKey {
  agent_id: string;
  symbol: string;
}

type SignalReadiness = "quiet" | "watch" | "candidate";

interface SymbolConsensus {
  symbol: string;
  count: number;
  avgDirection: number;
  avgConfidence: number;
  longCount: number;
  shortCount: number;
  latestTs: number;
  readiness: SignalReadiness;
}

interface PredictionFeedEntry {
  id: string;
  prediction: Prediction;
}

const CONFIDENCE_FLOORS = [
  { label: "All", value: 0 },
  { label: "50%+", value: 0.5 },
  { label: "70%+", value: 0.7 },
  { label: "85%+", value: 0.85 },
];

function key(p: AgentSymKey) {
  return `${p.agent_id}:${p.symbol}`;
}

export default function PredictionsPage() {
  const token = useAuth((s) => s.token);
  const [latest, setLatest] = useState<Map<string, Prediction>>(new Map());
  const [history, setHistory] = useState<PredictionFeedEntry[]>([]);
  const [query, setQuery] = useState("");
  const [confidenceFloor, setConfidenceFloor] = useState(0);

  const services = useQuery({
    queryKey: ["services", "predictions"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const promotion = useQuery({
    queryKey: ["models", "promote", "gbm_predictor.v1", "predictions"],
    queryFn: () =>
      api.modelPromotionState(token, {
        agent_id: "gbm_predictor.v1",
        history_limit: 1,
      }),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const onFrame = useCallback((frame: WsFrame) => {
    if (frame.topic !== "predictions") return;
    const p = frame.event.payload;
    setLatest((prev) => {
      const next = new Map(prev);
      next.set(key(p), p);
      return next;
    });
    setHistory((prev) =>
      [
        {
          id: `${p.agent_id}:${p.symbol}:${p.ts_event}:${Date.now()}:${prev.length}`,
          prediction: p,
        },
        ...prev,
      ].slice(0, 200),
    );
  }, []);

  useFinceptStream({ topics: ["predictions"], onFrame });

  const tiles = useMemo(
    () => Array.from(latest.values()).sort((a, b) => b.ts_event - a.ts_event),
    [latest],
  );
  const visibleTiles = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return tiles.filter((prediction) => {
      const passesConfidence = prediction.confidence >= confidenceFloor;
      if (!needle) return passesConfidence;
      return (
        passesConfidence &&
        (prediction.symbol.toLowerCase().includes(needle) ||
          prediction.agent_id.toLowerCase().includes(needle) ||
          prediction.calibration_tag?.toLowerCase().includes(needle))
      );
    });
  }, [confidenceFloor, query, tiles]);

  const consensus = useMemo(() => buildConsensus(tiles), [tiles]);
  const agentCount = new Set(tiles.map((p) => p.agent_id)).size;
  const symbolCount = new Set(tiles.map((p) => p.symbol)).size;
  const stats = useMemo(() => buildStats(tiles), [tiles]);

  return (
    <AppShell>
      <PageHeader
        title="Predictions"
        description="Live model signals by agent and symbol, reframed as an operator console: consensus, freshness, confidence, and decision-readiness without execution actions."
        action={
          <div className="flex items-center gap-2">
            <Badge variant="muted">{agentCount} agents</Badge>
            <Badge variant="muted">{symbolCount} symbols</Badge>
            <Badge variant="default">Realtime</Badge>
          </div>
        }
      />

      <ProductionSignalCockpit
        predictions={tiles}
        services={services.data ?? null}
        promotion={promotion.data ?? null}
      />

      <Card className="mb-4 border-cyan/30">
        <CardContent className="grid gap-3 p-4 xl:grid-cols-[1.1fr_0.9fr]">
          <section className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <ConsoleMetric
              label="Latest signal"
              value={stats.latestAgeLabel}
              sub="freshness"
            />
            <ConsoleMetric
              label="Average confidence"
              value={percent(stats.avgConfidence)}
              sub={`${stats.highConfidenceCount} high-confidence`}
              tone={stats.avgConfidence >= 0.7 ? "text-long" : "text-cyan"}
            />
            <ConsoleMetric
              label="Net model bias"
              value={signed(stats.netBias)}
              sub={stats.netBias >= 0 ? "positive tilt" : "negative tilt"}
              tone={stats.netBias >= 0 ? "text-long" : "text-short"}
            />
            <ConsoleMetric
              label="Stale tiles"
              value={String(stats.staleCount)}
              sub="older than 5m"
              tone={stats.staleCount > 0 ? "text-warn" : "text-muted-foreground"}
            />
          </section>

          <section className="border border-border p-3">
            <div className="mb-3 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
              <Filter className="h-3 w-3 text-cyan" />
              Operator filters
            </div>
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
              <Input
                placeholder="Filter symbol, agent, or calibration tag..."
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="lg:max-w-xs"
              />
              <div className="flex flex-wrap gap-1">
                {CONFIDENCE_FLOORS.map((option) => (
                  <button
                    key={option.label}
                    type="button"
                    aria-pressed={confidenceFloor === option.value}
                    onClick={() => setConfidenceFloor(option.value)}
                    className={cn(
                      "border px-2 py-1 text-[10px] uppercase tracking-wider transition-colors",
                      confidenceFloor === option.value
                        ? "border-cyan/70 bg-cyan/10 text-cyan"
                        : "border-border text-muted-foreground hover:border-cyan/40 hover:text-foreground",
                    )}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <Badge variant="muted" className="lg:ml-auto">
                {visibleTiles.length} shown
              </Badge>
            </div>
          </section>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_23rem]">
        <div className="space-y-4">
          <Card>
            <CardContent className="p-0">
              <div className="flex items-center justify-between border-b border-border/40 px-4 py-3">
                <span className="flex items-center gap-2 text-sm font-medium">
                  <Crosshair className="h-3.5 w-3.5 text-cyan" />
                  Symbol Consensus
                </span>
                <Badge variant="muted">{consensus.length}</Badge>
              </div>
              {consensus.length === 0 ? (
                <div className="p-4">
                  <EmptyState
                    icon={Crosshair}
                    title="No consensus yet"
                    description="Consensus rows appear once at least one agent publishes a prediction for a symbol."
                  />
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
                      <tr className="border-b border-border/40">
                        <th className="px-4 py-2 font-medium">Symbol</th>
                        <th className="px-4 py-2 font-medium">Agents</th>
                        <th className="px-4 py-2 font-medium">Tilt</th>
                        <th className="px-4 py-2 font-medium">Confidence</th>
                        <th className="px-4 py-2 font-medium">Split</th>
                        <th className="px-4 py-2 font-medium">Status</th>
                        <th className="px-4 py-2 font-medium">Freshness</th>
                      </tr>
                    </thead>
                    <tbody>
                      {consensus.slice(0, 8).map((row) => (
                        <tr
                          key={row.symbol}
                          className="border-b border-border/30 last:border-0 hover:bg-accent/30"
                        >
                          <td className="px-4 py-2 font-mono text-foreground">
                            {row.symbol}
                          </td>
                          <td className="px-4 py-2 font-mono text-muted-foreground">
                            {row.count}
                          </td>
                          <td
                            className={cn(
                              "px-4 py-2 font-mono",
                              row.avgDirection >= 0 ? "text-long" : "text-short",
                            )}
                          >
                            {signed(row.avgDirection)}
                          </td>
                          <td className="px-4 py-2 font-mono">
                            {percent(row.avgConfidence)}
                          </td>
                          <td className="px-4 py-2 font-mono text-muted-foreground">
                            {row.longCount} / {row.shortCount}
                          </td>
                          <td className="px-4 py-2">
                            <Badge variant={readinessVariant(row.readiness)}>
                              {readinessLabel(row.readiness)}
                            </Badge>
                          </td>
                          <td className="px-4 py-2 font-mono text-[10px] text-muted-foreground">
                            {ago(row.latestTs)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-4">
              {visibleTiles.length === 0 ? (
                <EmptyState
                  icon={Sparkles}
                  title={tiles.length === 0 ? "Waiting for first prediction" : "No predictions match"}
                  description={
                    tiles.length === 0
                      ? "When a predictor posts to STREAM_SIG_PREDICT, signal tiles and consensus rows will appear here."
                      : "Lower the confidence floor or clear the filter to restore hidden tiles."
                  }
                />
              ) : (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 2xl:grid-cols-3">
                  {visibleTiles.map((p) => {
                    const long = p.direction >= 0;
                    return (
                      <div
                        key={key(p)}
                        className="border border-border/60 bg-background/30 p-4 transition-colors hover:border-cyan/40 hover:bg-background/60"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <div className="font-mono text-base">{p.symbol}</div>
                            <Badge variant="muted" className="mt-1">
                              {p.agent_id}
                            </Badge>
                          </div>
                          <span
                            className={cn(
                              "num flex items-center gap-1 text-2xl font-semibold",
                              long ? "text-long" : "text-short",
                            )}
                          >
                            {long ? (
                              <TrendingUp className="h-4 w-4" />
                            ) : (
                              <TrendingDown className="h-4 w-4" />
                            )}
                            {p.direction.toFixed(2)}
                          </span>
                        </div>
                        <div className="mt-3">
                          <ConfidenceBar
                            direction={p.direction}
                            confidence={p.confidence}
                          />
                        </div>
                        <div className="mt-2 grid grid-cols-3 gap-2 text-[11px] text-muted-foreground">
                          <TileFact label="conf" value={percent(p.confidence)} />
                          <TileFact
                            label="horizon"
                            value={
                              p.horizon_ns
                                ? `${Math.round(p.horizon_ns / 1e9)}s`
                                : "—"
                            }
                          />
                          <TileFact label="age" value={ago(p.ts_event)} />
                        </div>
                        {p.calibration_tag ? (
                          <div className="mt-3 border-t border-border/50 pt-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                            {p.calibration_tag}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardContent className="p-0">
            <div className="flex items-center justify-between border-b border-border/40 px-4 py-3">
              <span className="flex items-center gap-2 text-sm font-medium">
                <RadioTower className="h-3.5 w-3.5 text-cyan" />
                Signal Tape
              </span>
              <Badge variant="muted">{history.length}</Badge>
            </div>
            {history.length === 0 ? (
              <div className="p-4">
                <EmptyState
                  icon={Activity}
                  title="No frames yet"
                  description="Frames appear as soon as the WebSocket delivers."
                />
              </div>
            ) : (
              <ScrollArea className="h-[42rem]">
                <ul className="divide-y divide-border/40">
                  {history.map((entry) => {
                    const p = entry.prediction;
                    return (
                      <li
                        key={entry.id}
                        className="grid grid-cols-[auto_1fr_auto] gap-3 px-4 py-2 text-xs"
                      >
                        <span
                          className={cn(
                            "mt-0.5 font-mono",
                            p.direction >= 0 ? "text-long" : "text-short",
                          )}
                        >
                          {p.direction >= 0 ? "▲" : "▼"}
                        </span>
                        <span className="min-w-0">
                          <span className="font-mono text-foreground">{p.symbol}</span>
                          <span className="ml-2 truncate text-muted-foreground">
                            {p.agent_id}
                          </span>
                          <span className="block truncate font-mono text-[10px] text-muted-foreground">
                            {p.calibration_tag ?? "uncalibrated"}
                          </span>
                        </span>
                        <span className="text-right font-mono text-[10px] text-muted-foreground">
                          <span
                            className={cn(
                              "block text-xs",
                              p.direction >= 0 ? "text-long" : "text-short",
                            )}
                          >
                            {p.direction.toFixed(2)} · {percent(p.confidence)}
                          </span>
                          {ago(p.ts_event)}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </ScrollArea>
            )}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  );
}

function buildConsensus(predictions: Prediction[]): SymbolConsensus[] {
  const groups = new Map<string, Prediction[]>();
  for (const prediction of predictions) {
    const bucket = groups.get(prediction.symbol) ?? [];
    bucket.push(prediction);
    groups.set(prediction.symbol, bucket);
  }

  return Array.from(groups.entries())
    .map(([symbol, rows]) => {
      const avgDirection =
        rows.reduce((sum, row) => sum + row.direction, 0) / rows.length;
      const avgConfidence =
        rows.reduce((sum, row) => sum + row.confidence, 0) / rows.length;
      const longCount = rows.filter((row) => row.direction >= 0).length;
      const shortCount = rows.length - longCount;
      const latestTs = Math.max(...rows.map((row) => row.ts_event));
      return {
        symbol,
        count: rows.length,
        avgDirection,
        avgConfidence,
        longCount,
        shortCount,
        latestTs,
        readiness: readinessFor(rows.length, avgDirection, avgConfidence),
      };
    })
    .sort(
      (a, b) =>
        readinessRank(b.readiness) - readinessRank(a.readiness) ||
        b.avgConfidence - a.avgConfidence ||
        Math.abs(b.avgDirection) - Math.abs(a.avgDirection),
    );
}

function buildStats(predictions: Prediction[]) {
  if (!predictions.length) {
    return {
      avgConfidence: 0,
      highConfidenceCount: 0,
      latestAgeLabel: "—",
      netBias: 0,
      staleCount: 0,
    };
  }
  const avgConfidence =
    predictions.reduce((sum, prediction) => sum + prediction.confidence, 0) /
    predictions.length;
  const netBias =
    predictions.reduce((sum, prediction) => sum + prediction.direction, 0) /
    predictions.length;
  const latestTs = Math.max(...predictions.map((prediction) => prediction.ts_event));
  return {
    avgConfidence,
    highConfidenceCount: predictions.filter((prediction) => prediction.confidence >= 0.7).length,
    latestAgeLabel: ago(latestTs),
    netBias,
    staleCount: predictions.filter((prediction) => {
      const age = ageSeconds(prediction.ts_event);
      return age === null || age > 300;
    }).length,
  };
}

function readinessFor(
  agentCount: number,
  avgDirection: number,
  avgConfidence: number,
): SignalReadiness {
  if (agentCount >= 2 && avgConfidence >= 0.7 && Math.abs(avgDirection) >= 0.35) {
    return "candidate";
  }
  if (avgConfidence >= 0.5 || Math.abs(avgDirection) >= 0.2) return "watch";
  return "quiet";
}

function readinessRank(readiness: SignalReadiness): number {
  if (readiness === "candidate") return 2;
  if (readiness === "watch") return 1;
  return 0;
}

function readinessLabel(readiness: SignalReadiness): string {
  if (readiness === "candidate") return "Decision candidate";
  if (readiness === "watch") return "Watch";
  return "Quiet";
}

function readinessVariant(
  readiness: SignalReadiness,
): "default" | "warn" | "muted" {
  if (readiness === "candidate") return "default";
  if (readiness === "watch") return "warn";
  return "muted";
}

function ConsoleMetric({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub: string;
  tone?: string;
}) {
  return (
    <div className="border border-border p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-1 font-mono text-lg text-foreground", tone)}>
        {value}
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground">{sub}</div>
    </div>
  );
}

function TileFact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="truncate font-mono text-foreground">{value}</div>
    </div>
  );
}

function percent(value: number): string {
  return `${(value * 100).toFixed(0)}%`;
}

function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function ageSeconds(ns: number): number | null {
  const d = nsToDate(ns);
  if (!d) return null;
  return Math.max(0, (Date.now() - d.getTime()) / 1000);
}

function ago(ns: number): string {
  const d = nsToDate(ns);
  if (!d) return "—";
  try {
    return formatDistanceToNowStrict(d, { addSuffix: false });
  } catch {
    return "—";
  }
}
