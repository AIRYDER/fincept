"use client";

import { AlertTriangle, CheckCircle2, ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { Position, StrategyConfigRow } from "@/lib/types";
import { cn } from "@/lib/utils";

import { buildStrategyReadiness } from "./strategy-readiness";

type Readiness = ReturnType<typeof buildStrategyReadiness>;
type Check = Readiness["checks"][number];

export function StrategyReadinessPanel({
  config,
  positions,
}: {
  config: StrategyConfigRow;
  positions: Position[];
}) {
  const readiness = buildStrategyReadiness(config, positions);

  return (
    <Card
      className={cn(
        readiness.state === "ready" && "border-cyan/35",
        readiness.state === "review" && "border-warn/40",
        readiness.state === "blocked" && "border-short/45",
      )}
    >
      <CardHeader>
        <CardTitle>
          {readiness.state === "blocked" ? (
            <AlertTriangle className="mr-1 h-3.5 w-3.5 text-short" />
          ) : (
            <ShieldCheck className="mr-1 h-3.5 w-3.5 text-cyan" />
          )}
          Strategy readiness
        </CardTitle>
        <CardDescription>
          Read-only gate for config hygiene before lifecycle changes.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={readiness.state === "blocked" ? "destructive" : readiness.state === "review" ? "warn" : "default"}>
            {readiness.state}
          </Badge>
          <Badge variant="muted">Score {readiness.score.toFixed(0)}</Badge>
          <Badge variant="outline">Paper guardrail</Badge>
        </div>
        <p className="text-xs leading-5 text-muted-foreground">{readiness.headline}</p>
        <div className="space-y-2">
          {readiness.checks.map((check) => (
            <ReadinessCheck key={check.id} check={check} />
          ))}
        </div>
        <div className="border border-border/50 bg-background/30 p-2">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
            <CheckCircle2 className="h-3 w-3 text-cyan" />
            Operator actions
          </div>
          <ul className="space-y-1 text-[11px] leading-4 text-muted-foreground">
            {readiness.actions.map((action) => (
              <li key={action} className="border-l border-border pl-2">
                {action}
              </li>
            ))}
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}

function ReadinessCheck({ check }: { check: Check }) {
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

function checkClass(severity: Check["severity"]): string {
  if (severity === "pass") return "border-cyan/30 bg-cyan/5";
  if (severity === "watch") return "border-warn/35 bg-warn/5";
  return "border-short/40 bg-short/5";
}
