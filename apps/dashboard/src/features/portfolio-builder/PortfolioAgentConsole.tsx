"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, Cpu, MessageSquareText, RadioTower } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

import type {
  PortfolioAllocationResult,
  PortfolioModelProvider,
  PortfolioReportLLMResponse,
} from "./portfolioBuilder.types";

export type PortfolioAgentEventStatus =
  | "queued"
  | "running"
  | "complete"
  | "warning";

export interface PortfolioAgentEvent {
  at: string;
  agent: string;
  status: PortfolioAgentEventStatus;
  message: string;
}

export function PortfolioAgentConsole({
  events,
  loading,
  provider,
  allocation,
  report,
}: {
  events: PortfolioAgentEvent[];
  loading: boolean;
  provider: PortfolioModelProvider;
  allocation: PortfolioAllocationResult | null;
  report: PortfolioReportLLMResponse | null;
}) {
  const token = useAuth((state) => state.token);
  const services = useQuery({
    queryKey: ["services", "portfolio-agent-console"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  const watchedServices =
    services.data?.services.filter((service) =>
      [
        "market_data",
        "openbb",
        "regime",
        "gbm_predictor",
        "news_alpha_predictor",
        "jobs",
      ].includes(service.name),
    ) ?? [];

  const providerLabel =
    provider === "auto"
      ? "Auto provider race"
      : provider === "openai"
        ? "GPT-5.5 route"
        : "Opus 4.7 route";
  const trace = events.length
    ? events
    : [
        {
          at: new Date().toISOString(),
          agent: "Universe Scout",
          status: "queued" as const,
          message: "Waiting for capital, horizon, risk, and research universe.",
        },
      ];

  return (
    <Card className="print:hidden">
      <CardHeader className="flex-row items-center justify-between gap-3">
        <CardTitle>
          <MessageSquareText className="h-3.5 w-3.5 text-cyan" />
          Agent Compute Watch
        </CardTitle>
        <Badge variant={loading ? "warn" : report ? "long" : "muted"}>
          {loading ? "Running" : report ? "Report ready" : providerLabel}
        </Badge>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-2 md:grid-cols-3">
          <Stat
            icon={Cpu}
            label="Universe"
            value={
              allocation
                ? `${allocation.candidateAudit.eligibleCount}/${allocation.candidateAudit.universeCount}`
                : "queued"
            }
            sub="eligible / total"
          />
          <Stat
            icon={Activity}
            label="Constraints"
            value={allocation?.optimization.feasible ? "feasible" : "pending"}
            sub={allocation?.optimization.method ?? "optimizer idle"}
          />
          <Stat
            icon={RadioTower}
            label="Services"
            value={services.isError ? "offline" : watchedServices.length ? `${watchedServices.length} seen` : "polling"}
            sub="live agent plane"
          />
        </div>

        <div className="border border-border">
          <div className="border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-cyan">
            Portfolio agent transcript
          </div>
          <div className="max-h-60 space-y-2 overflow-y-auto p-3 scrollbar-thin">
            {trace.map((event, index) => (
              <div key={`${event.at}-${event.agent}-${index}`} className="grid grid-cols-[92px_1fr] gap-3 text-xs">
                <div>
                  <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                    {event.agent}
                  </div>
                  <div className={cn("mt-1 text-[10px] uppercase tracking-wider", statusClass(event.status))}>
                    {event.status}
                  </div>
                </div>
                <div className="border-l border-border pl-3 leading-5 text-muted-foreground">
                  {event.message}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="grid gap-2 md:grid-cols-2">
          {watchedServices.map((service) => (
            <div
              key={service.name}
              className="flex items-center justify-between gap-3 border border-border px-2 py-1.5 text-[10px] uppercase tracking-wider"
            >
              <span className="truncate text-muted-foreground">{service.name}</span>
              <span
                className={cn(
                  service.status === "up" && "text-long",
                  service.status === "stale" && "text-warn",
                  service.status === "down" && "text-short",
                )}
              >
                {service.status}
              </span>
            </div>
          ))}
          {!watchedServices.length ? (
            <div className="border border-dashed border-border px-2 py-2 text-[10px] uppercase tracking-wider text-muted-foreground md:col-span-2">
              Service heartbeat unavailable in this browser session.
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

function Stat({
  icon: Icon,
  label,
  value,
  sub,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  sub: string;
}) {
  return (
    <div className="border border-border p-2">
      <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <Icon className="h-3 w-3 text-cyan" />
        {label}
      </div>
      <div className="mt-1 font-mono text-sm text-foreground">{value}</div>
      <div className="mt-1 truncate text-[10px] uppercase tracking-wider text-muted-foreground">
        {sub}
      </div>
    </div>
  );
}

function statusClass(status: PortfolioAgentEventStatus): string {
  if (status === "complete") return "text-long";
  if (status === "running") return "text-cyan";
  if (status === "warning") return "text-warn";
  return "text-muted-foreground";
}
