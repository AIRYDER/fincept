"use client";

import { Activity, FileJson, Gauge, ShieldCheck, ShieldAlert, Workflow } from "lucide-react";

import { EvidenceStack } from "@/components/evidence/evidence-stack";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

import {
  buildPortfolioCockpit,
  buildPortfolioCockpitReceiptFilename,
  portfolioCockpitReceiptToJson,
} from "./portfolioCockpit";
import { downloadTextFile } from "./portfolioExport";
import type {
  PortfolioAllocationResult,
  PortfolioReportLLMResponse,
} from "./portfolioBuilder.types";

export function PortfolioOptimizerCockpit({
  allocation,
  report,
}: {
  allocation: PortfolioAllocationResult;
  report: PortfolioReportLLMResponse;
}) {
  const cockpit = buildPortfolioCockpit(allocation, report);
  const tone = cockpit.state === "ready" ? "verified" : cockpit.state === "blocked" ? "critical" : "caveat";

  return (
    <Card className={cn(
      "break-inside-avoid",
      cockpit.state === "ready" && "border-cyan/40",
      cockpit.state === "review" && "border-warn/40",
      cockpit.state === "blocked" && "border-short/50",
    )}>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <CardTitle>
          {cockpit.state === "blocked" ? (
            <ShieldAlert className="h-3.5 w-3.5 text-short" />
          ) : (
            <ShieldCheck className="h-3.5 w-3.5 text-cyan" />
          )}
          Portfolio Optimizer Cockpit
        </CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={cockpit.state === "ready" ? "default" : cockpit.state === "blocked" ? "destructive" : "warn"}>
            {cockpit.state}
          </Badge>
          <Badge variant="muted">Score {cockpit.score.toFixed(0)}</Badge>
          <Badge variant="outline">Planning only</Badge>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              downloadTextFile(
                buildPortfolioCockpitReceiptFilename(allocation, report),
                portfolioCockpitReceiptToJson(allocation, report),
                "application/json",
              )
            }
          >
            <FileJson className="h-3.5 w-3.5" />
            Receipt
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 xl:grid-cols-[0.95fr_1.05fr]">
          <section className="space-y-3">
            <div className="border border-border p-3">
              <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                <Gauge className="h-3 w-3 text-cyan" />
                Readiness gate
              </div>
              <p className="text-sm leading-6 text-foreground">{cockpit.headline}</p>
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                {cockpit.checks.map((check) => (
                  <CheckTile key={check.id} check={check} />
                ))}
              </div>
            </div>

            <div className="border border-border p-3">
              <div className="mb-3 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                <Workflow className="h-3 w-3 text-cyan" />
                Operator actions
              </div>
              <ul className="space-y-2 text-xs leading-5 text-muted-foreground">
                {cockpit.operatorActions.map((action) => (
                  <li key={action} className="border-l border-border pl-3">
                    {action}
                  </li>
                ))}
              </ul>
            </div>
          </section>

          <section className="space-y-3">
            <div className="border border-border p-3">
              <div className="mb-3 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                <Activity className="h-3 w-3 text-cyan" />
                Risk budget rails
              </div>
              <div className="space-y-3">
                {cockpit.budgetRails.map((rail) => (
                  <BudgetRail key={rail.label} rail={rail} />
                ))}
              </div>
            </div>

            <EvidenceStack
              title="Portfolio optimizer evidence"
              summary={cockpit.headline}
              evidence={cockpit.evidenceRows}
              payload={cockpit.payload}
              trace={cockpit.traceRows}
              tone={tone}
            />
          </section>
        </div>
      </CardContent>
    </Card>
  );
}

type Cockpit = ReturnType<typeof buildPortfolioCockpit>;

type Check = Cockpit["checks"][number];
type Rail = Cockpit["budgetRails"][number];

function CheckTile({ check }: { check: Check }) {
  return (
    <div className={cn("border p-2", statusClass(check.severity))}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{check.label}</span>
        <span className="font-mono text-[10px] uppercase">{check.severity}</span>
      </div>
      <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{check.detail}</p>
    </div>
  );
}

function BudgetRail({ rail }: { rail: Rail }) {
  const width = rail.limitPct > 0 ? Math.min(100, Math.max(0, (rail.valuePct / rail.limitPct) * 100)) : 0;
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-3 text-xs">
        <div>
          <span className="text-foreground">{rail.label}</span>
          <span className="ml-2 text-muted-foreground">{rail.detail}</span>
        </div>
        <span className={cn("font-mono", rail.status === "fail" && "text-short", rail.status === "watch" && "text-warn", rail.status === "pass" && "text-cyan")}>
          {rail.valuePct.toFixed(1)} / {rail.limitPct.toFixed(1)}%
        </span>
      </div>
      <div className="h-2 border border-border bg-background">
        <div
          className={cn(
            "h-full",
            rail.status === "fail" && "bg-short",
            rail.status === "watch" && "bg-warn",
            rail.status === "pass" && "bg-cyan",
          )}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

function statusClass(status: Check["severity"]) {
  if (status === "pass") return "border-cyan/35 bg-cyan/5";
  if (status === "watch") return "border-warn/40 bg-warn/5";
  return "border-short/45 bg-short/5";
}
