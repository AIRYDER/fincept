"use client";

import { Activity, Sparkles } from "lucide-react";
import { useCallback, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { ConfidenceBar } from "@/components/widgets/confidence-bar";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { Prediction, WsFrame } from "@/lib/types";
import { cn, nsToDate } from "@/lib/utils";
import { useFinceptStream } from "@/lib/ws";
import { formatDistanceToNowStrict } from "date-fns";

interface AgentSymKey {
  agent_id: string;
  symbol: string;
}

function key(p: AgentSymKey) {
  return `${p.agent_id}:${p.symbol}`;
}

export default function PredictionsPage() {
  // latest = consolidated view per (agent, symbol)
  const [latest, setLatest] = useState<Map<string, Prediction>>(new Map());
  // history = scrolling feed of last N predictions
  const [history, setHistory] = useState<Prediction[]>([]);

  const onFrame = useCallback((frame: WsFrame) => {
    if (frame.topic !== "predictions") return;
    const p = frame.event.payload;
    setLatest((prev) => {
      const next = new Map(prev);
      next.set(key(p), p);
      return next;
    });
    setHistory((prev) => [p, ...prev].slice(0, 200));
  }, []);

  useFinceptStream({ topics: ["predictions"], onFrame });

  const tiles = Array.from(latest.values()).sort(
    (a, b) => b.ts_event - a.ts_event,
  );
  const agentCount = new Set(tiles.map((p) => p.agent_id)).size;
  const symbolCount = new Set(tiles.map((p) => p.symbol)).size;

  return (
    <AppShell>
      <PageHeader
        title="Predictions"
        description="Live signals from running agents.  Each tile shows the latest direction/confidence per (agent, symbol).  The orchestrator consumes these and emits Decisions when consensus + sizing thresholds are met."
        action={
          <div className="flex items-center gap-2">
            <Badge variant="muted">{agentCount} agents</Badge>
            <Badge variant="muted">{symbolCount} symbols</Badge>
            <Badge variant="default">Realtime</Badge>
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_22rem]">
        {/* Tiles */}
        <Card>
          <CardContent className="p-4">
            {tiles.length === 0 ? (
              <EmptyState
                icon={Sparkles}
                title="Waiting for first prediction"
                description="When the gbm_predictor agent runs and posts to STREAM_SIG_PREDICT, tiles will appear here."
              />
            ) : (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {tiles.map((p) => {
                  const long = p.direction >= 0;
                  return (
                    <div
                      key={key(p)}
                      className="rounded-lg border border-border/40 bg-background/30 p-4 transition-colors hover:bg-background/60"
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
                            "num text-2xl font-semibold",
                            long ? "text-long" : "text-short",
                          )}
                        >
                          {long ? "▲" : "▼"} {p.direction.toFixed(2)}
                        </span>
                      </div>
                      <div className="mt-3">
                        <ConfidenceBar
                          direction={p.direction}
                          confidence={p.confidence}
                        />
                      </div>
                      <div className="mt-2 flex items-center justify-between text-[11px] text-muted-foreground">
                        <span className="font-mono">
                          conf {(p.confidence * 100).toFixed(0)}%
                        </span>
                        <span className="font-mono">
                          horizon{" "}
                          {p.horizon_ns
                            ? `${Math.round(p.horizon_ns / 1e9)}s`
                            : "—"}
                        </span>
                        <span className="font-mono">{ago(p.ts_event)}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        {/* History feed */}
        <Card>
          <CardContent className="p-0">
            <div className="flex items-center justify-between border-b border-border/40 px-4 py-3">
              <span className="flex items-center gap-2 text-sm font-medium">
                <Activity className="h-3.5 w-3.5" />
                Stream
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
              <ScrollArea className="h-[32rem]">
                <ul className="divide-y divide-border/40">
                  {history.map((p, i) => (
                    <li
                      key={`${p.agent_id}:${p.symbol}:${p.ts_event}:${i}`}
                      className="flex items-center gap-3 px-4 py-2 text-xs"
                    >
                      <span
                        className={cn(
                          "font-mono",
                          p.direction >= 0 ? "text-long" : "text-short",
                        )}
                      >
                        {p.direction >= 0 ? "▲" : "▼"}
                      </span>
                      <span className="font-mono">{p.symbol}</span>
                      <span className="text-muted-foreground">
                        {p.agent_id}
                      </span>
                      <span className="ml-auto font-mono text-muted-foreground">
                        {p.direction.toFixed(2)} · {(p.confidence * 100).toFixed(0)}%
                      </span>
                      <span className="font-mono text-[10px] text-muted-foreground">
                        {ago(p.ts_event)}
                      </span>
                    </li>
                  ))}
                </ul>
              </ScrollArea>
            )}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  );
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
