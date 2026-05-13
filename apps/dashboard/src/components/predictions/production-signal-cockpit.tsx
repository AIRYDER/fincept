"use client";

import { Activity, AlertTriangle, RadioTower, ShieldCheck, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { Prediction, PromotionStateResponse, ServicesResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import { buildSignalCockpit } from "./signal-cockpit";

type Cockpit = ReturnType<typeof buildSignalCockpit>;
type Check = Cockpit["checks"][number];
type SymbolRow = Cockpit["symbols"][number];

export function ProductionSignalCockpit({
  predictions,
  services,
  promotion,
}: {
  predictions: Prediction[];
  services?: ServicesResponse | null;
  promotion?: PromotionStateResponse | null;
}) {
  const cockpit = buildSignalCockpit({ predictions, services: services ?? null, promotion: promotion ?? null });

  return (
    <Card
      className={cn(
        "mb-4",
        cockpit.state === "ready" && "border-cyan/35",
        cockpit.state === "review" && "border-warn/40",
        cockpit.state === "blocked" && "border-short/45",
      )}
    >
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              {cockpit.state === "blocked" ? (
                <AlertTriangle className="h-4 w-4 text-short" />
              ) : (
                <RadioTower className="h-4 w-4 text-primary" />
              )}
              Production signal cockpit
            </CardTitle>
            <CardDescription>
              Read-only signal readiness from live prediction tiles, predictor services, and model binding.
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={cockpit.state === "blocked" ? "destructive" : cockpit.state === "review" ? "warn" : "default"}>
              {cockpit.state}
            </Badge>
            <Badge variant="muted">Score {cockpit.score.toFixed(0)}</Badge>
            <Badge variant="outline">No execution</Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm leading-6 text-muted-foreground">{cockpit.headline}</p>
        <div className="grid gap-3 md:grid-cols-4">
          <Metric label="Predictions" value={String(cockpit.stats.predictionCount)} tone="primary" />
          <Metric label="Agents" value={String(cockpit.stats.agentCount)} tone="cyan" />
          <Metric label="Symbols" value={String(cockpit.stats.symbolCount)} tone="long" />
          <Metric label="Avg confidence" value={`${Math.round(cockpit.stats.avgConfidence * 100)}%`} tone="warn" />
        </div>
        <div className="grid gap-3 xl:grid-cols-[1fr_0.9fr]">
          <div className="space-y-2">
            {cockpit.checks.map((check) => (
              <SignalCheck key={check.id} check={check} />
            ))}
          </div>
          <div className="space-y-3">
            <div className="border border-border/50 bg-background/30 p-3">
              <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
                <ShieldCheck className="h-3 w-3 text-cyan" />
                Operator actions
              </div>
              <ul className="space-y-1.5 text-[11px] leading-4 text-muted-foreground">
                {cockpit.actions.map((action) => (
                  <li key={action} className="border-l border-border pl-2">
                    {action}
                  </li>
                ))}
              </ul>
            </div>
            <div className="border border-border/50 bg-background/30 p-3">
              <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
                <Sparkles className="h-3 w-3 text-cyan" />
                Top symbol posture
              </div>
              {cockpit.symbols.length === 0 ? (
                <div className="text-[11px] text-muted-foreground">No signal posture yet.</div>
              ) : (
                <div className="space-y-1.5">
                  {cockpit.symbols.slice(0, 5).map((row) => (
                    <SymbolPosture key={row.symbol} row={row} />
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone: "primary" | "cyan" | "long" | "warn" }) {
  return (
    <div className="border border-border/50 bg-background/30 p-3">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={cn("mt-1 font-mono text-2xl font-bold", tone === "primary" && "text-primary", tone === "cyan" && "text-cyan", tone === "long" && "text-long", tone === "warn" && "text-warn")}>{value}</div>
    </div>
  );
}

function SignalCheck({ check }: { check: Check }) {
  return (
    <div className={cn("border p-2", checkClass(check.severity))}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">{check.label}</span>
        <span className="font-mono text-[10px] uppercase">{check.severity}</span>
      </div>
      <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{check.detail}</p>
    </div>
  );
}

function SymbolPosture({ row }: { row: SymbolRow }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border/25 pb-1.5 text-xs">
      <div>
        <div className="font-mono text-[11px]">{row.symbol}</div>
        <div className="text-[10px] text-muted-foreground">
          {row.count} signal(s) · {row.longCount}/{row.shortCount} L/S
        </div>
      </div>
      <div className="text-right">
        <Badge variant={row.state === "candidate" ? "long" : row.state === "watch" ? "warn" : "muted"}>{row.state}</Badge>
        <div className={cn("mt-1 font-mono text-[10px]", row.avgDirection >= 0 ? "text-long" : "text-short")}>
          {row.avgDirection >= 0 ? "+" : ""}{row.avgDirection.toFixed(2)} · {Math.round(row.avgConfidence * 100)}%
        </div>
      </div>
    </div>
  );
}

function checkClass(severity: Check["severity"]): string {
  if (severity === "pass") return "border-cyan/30 bg-cyan/5";
  if (severity === "watch") return "border-warn/35 bg-warn/5";
  return "border-short/40 bg-short/5";
}
