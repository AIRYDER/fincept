"use client";

import { AlertTriangle, DatabaseZap, ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type {
  DataCoverageResponse,
  DataSourcesResponse,
  OpenBBHealthResponse,
  ProviderDataResponse,
  ServicesResponse,
} from "@/lib/types";
import { cn } from "@/lib/utils";

import { buildSourceHealthSummary } from "./source-health";

type Summary = ReturnType<typeof buildSourceHealthSummary>;
type Check = Summary["checks"][number];

export function SourceHealthControlCenter({
  sources,
  coverage,
  openbb,
  providerData,
  services,
}: {
  sources?: DataSourcesResponse | null;
  coverage?: DataCoverageResponse | null;
  openbb?: OpenBBHealthResponse | null;
  providerData?: ProviderDataResponse | null;
  services?: ServicesResponse | null;
}) {
  const summary = buildSourceHealthSummary({ sources, coverage, openbb, providerData, services });

  return (
    <Card
      className={cn(
        "mb-4",
        summary.state === "ready" && "border-cyan/35",
        summary.state === "review" && "border-warn/40",
        summary.state === "blocked" && "border-short/45",
      )}
    >
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              {summary.state === "blocked" ? (
                <AlertTriangle className="h-4 w-4 text-short" />
              ) : (
                <DatabaseZap className="h-4 w-4 text-primary" />
              )}
              Source health control center
            </CardTitle>
            <CardDescription>
              Read-only source registry, OpenBB, service heartbeat, and coverage review.
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={summary.state === "blocked" ? "destructive" : summary.state === "review" ? "warn" : "default"}>
              {summary.state}
            </Badge>
            <Badge variant="muted">Score {summary.score.toFixed(0)}</Badge>
            <Badge variant="outline">No order path</Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm leading-6 text-muted-foreground">{summary.headline}</p>
        <div className="grid gap-3 lg:grid-cols-[1fr_0.8fr]">
          <div className="space-y-2">
            {summary.checks.map((check) => (
              <HealthCheck key={check.id} check={check} />
            ))}
          </div>
          <div className="space-y-3">
            <div className="border border-border/50 bg-background/30 p-3">
              <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
                <ShieldCheck className="h-3 w-3 text-cyan" />
                Operator actions
              </div>
              <ul className="space-y-1.5 text-[11px] leading-4 text-muted-foreground">
                {summary.actions.map((action) => (
                  <li key={action} className="border-l border-border pl-2">
                    {action}
                  </li>
                ))}
              </ul>
            </div>
            <div className="border border-border/50 bg-background/30 p-3">
              <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
                Registered sources
              </div>
              <div className="max-h-52 space-y-1.5 overflow-y-auto pr-1">
                {summary.registryRows.length === 0 ? (
                  <div className="text-[11px] text-muted-foreground">No source rows loaded.</div>
                ) : (
                  summary.registryRows.map((row) => (
                    <div key={row.id} className="flex items-center justify-between gap-3 border-b border-border/25 pb-1.5 text-xs">
                      <div className="min-w-0">
                        <div className="truncate font-mono text-[11px]">{row.name}</div>
                        <div className="text-[10px] text-muted-foreground">{row.category} · {row.healthMode}</div>
                      </div>
                      <Badge variant={row.safety === "read_only" || row.safety === "experimental_read_only" ? "muted" : "warn"}>
                        {row.safety}
                      </Badge>
                    </div>
                  ))
                )}
              </div>
            </div>
            <div className="border border-border/50 bg-background/30 p-3">
              <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
                Provider capture ledger
              </div>
              <p className="mb-2 text-[11px] leading-4 text-muted-foreground">{summary.captureDetail}</p>
              <div className="max-h-52 space-y-1.5 overflow-y-auto pr-1">
                {summary.captureRows.length === 0 ? (
                  <div className="text-[11px] text-muted-foreground">No captured provider rows loaded.</div>
                ) : (
                  summary.captureRows.map((row) => (
                    <div key={row.id} className="flex items-center justify-between gap-3 border-b border-border/25 pb-1.5 text-xs">
                      <div className="min-w-0">
                        <div className="truncate font-mono text-[11px]">{row.dataset}</div>
                        <div className="truncate text-[10px] text-muted-foreground">
                          {row.provider} · {row.symbol ?? "GLOBAL"} · {row.endpoint}
                        </div>
                      </div>
                      <Badge variant={row.ok ? "muted" : "destructive"}>
                        {row.ok ? `${row.rowCount} row${row.rowCount === 1 ? "" : "s"}` : row.errorType ?? "error"}
                      </Badge>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function HealthCheck({ check }: { check: Check }) {
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
