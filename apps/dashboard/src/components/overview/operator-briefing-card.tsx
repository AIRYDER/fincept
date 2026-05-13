"use client";

import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  AlertTriangle,
  ChevronRight,
  ClipboardCheck,
  Power,
  ShieldCheck,
} from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import {
  buildOperatorBriefing,
  type BriefingState,
  type BriefingStripItem,
} from "@/components/overview/operator-briefing";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill } from "@/components/widgets/status-pill";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SemanticIntent } from "@/lib/design-tokens";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Intent mapping
// ---------------------------------------------------------------------------

function briefingIntent(state: BriefingState): SemanticIntent {
  if (state === "ready") return "verified";
  if (state === "watch") return "degraded";
  return "critical";
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

export function OperatorBriefingCard() {
  const token = useAuth((s) => s.token);

  const positionsQ = useQuery({
    queryKey: ["positions", "briefing"],
    queryFn: () => api.positions(token, true),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const strategiesQ = useQuery({
    queryKey: ["strategies", "briefing"],
    queryFn: () => api.strategies(token),
    enabled: !!token,
    refetchInterval: 60_000,
  });
  const configsQ = useQuery({
    queryKey: ["strategy-configs", "briefing"],
    queryFn: () => api.strategyConfigs(token),
    enabled: !!token,
    refetchInterval: 60_000,
  });
  const universeQ = useQuery({
    queryKey: ["universe", "briefing"],
    queryFn: () => api.universe(token),
    enabled: !!token,
  });
  const coverageQ = useQuery({
    queryKey: ["data-coverage", "briefing"],
    queryFn: () => api.dataCoverage(token, { freq: "1m" }),
    enabled: !!token,
    refetchInterval: 60_000,
  });
  const ordersQ = useQuery({
    queryKey: ["orders", "briefing"],
    queryFn: () => api.orders(token, { limit: 100 }),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const servicesQ = useQuery({
    queryKey: ["services", "briefing"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 15_000,
  });
  const killQ = useQuery({
    queryKey: ["kill-switch", "briefing"],
    queryFn: () => api.killSwitchState(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });

  const packet = useMemo(
    () =>
      buildOperatorBriefing({
        positions: positionsQ.data ?? [],
        strategies: strategiesQ.data ?? [],
        configs: configsQ.data ?? [],
        universe: universeQ.data ?? [],
        coverage: coverageQ.data?.rows ?? [],
        orders: ordersQ.data ?? [],
        services: servicesQ.data,
        killSwitch: killQ.data,
      }),
    [
      positionsQ.data,
      strategiesQ.data,
      configsQ.data,
      universeQ.data,
      coverageQ.data,
      ordersQ.data,
      servicesQ.data,
      killQ.data,
    ],
  );

  const overallIntent = briefingIntent(packet.state);

  return (
    <Card className="border-primary/20">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <ShieldCheck className="h-4 w-4 text-primary" />
              Operator briefing
            </CardTitle>
            <CardDescription>
              At-a-glance operator status. Aggregates safety, services, reconciliation, strategies, and receipt catalog.
            </CardDescription>
          </div>
          <StatusPill intent={overallIntent} label={packet.state.toUpperCase()} />
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">{packet.headline}</p>

        {/* Safety strip --------------------------------------------------- */}
        <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
          {packet.strip.map((item) => (
            <StripCell key={item.id} item={item} />
          ))}
        </div>

        {/* Top issues ----------------------------------------------------- */}
        {packet.topIssues.length > 0 && (
          <div>
            <h4 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
              <ClipboardCheck className="h-3 w-3" />
              Top unresolved issues
              <span className="text-[10px] normal-case tracking-normal">
                ({packet.topIssues.length} shown)
              </span>
            </h4>
            <ul className="space-y-1.5">
              {packet.topIssues.map((issue) => (
                <li
                  key={issue.id}
                  className="flex items-start gap-2 rounded-md border border-border/30 bg-card/40 px-3 py-2 text-xs"
                >
                  {issue.severity === "critical" ? (
                    <AlertCircle className="mt-0.5 h-3 w-3 shrink-0 text-short" />
                  ) : (
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-amber" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="font-medium">{issue.label}</span>
                      <Badge variant="outline" className="text-[9px]">
                        {issue.owner}
                      </Badge>
                    </div>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">{issue.detail}</p>
                  </div>
                </li>
              ))}
            </ul>
            <Link
              href="/reconciliation"
              className="mt-2 inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              View full reconciliation checklist <ChevronRight className="h-3 w-3" />
            </Link>
          </div>
        )}

        {/* Strategies attention ------------------------------------------- */}
        {packet.strategies.attention.length > 0 && (
          <div>
            <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Strategies needing attention
              <span className="ml-1 text-[10px] normal-case tracking-normal">
                ({packet.strategies.attention.length} of {packet.strategies.total})
              </span>
            </h4>
            <ul className="space-y-1.5">
              {packet.strategies.attention.map((s) => (
                <li
                  key={s.strategy_id}
                  className="flex items-center justify-between gap-2 rounded-md border border-border/30 bg-card/40 px-3 py-1.5 text-xs"
                >
                  <Link
                    href={`/strategies/${encodeURIComponent(s.strategy_id)}`}
                    className="flex min-w-0 flex-1 items-center gap-2 hover:underline"
                  >
                    <StatusPill
                      intent={s.state === "ready" ? "verified" : s.state === "review" ? "degraded" : "critical"}
                      label={s.state.toUpperCase()}
                      compact
                      dot={false}
                    />
                    <code className="truncate font-mono text-[11px]">{s.strategy_id}</code>
                  </Link>
                  <span className="hidden truncate text-[10px] text-muted-foreground md:inline">
                    {s.headline}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Kill switch detail ---------------------------------------------- */}
        {packet.killSwitch.engaged && (
          <div className="flex items-start gap-2 rounded-md border border-short/40 bg-short/5 px-3 py-2 text-xs">
            <Power className="mt-0.5 h-3.5 w-3.5 shrink-0 text-short" />
            <div className="min-w-0 flex-1">
              <div className="font-medium text-short">Kill switch is ENGAGED</div>
              <p className="text-[11px] text-muted-foreground">
                {packet.killSwitch.reason ?? "No reason recorded."} Clear via Risk page.
              </p>
            </div>
            <Link href="/risk" className="shrink-0 text-[11px] text-primary hover:underline">
              Go to Risk →
            </Link>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Strip cell
// ---------------------------------------------------------------------------

function StripCell({ item }: { item: BriefingStripItem }) {
  const intent = briefingIntent(item.state);
  const content = (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-md border bg-card/40 px-3 py-2 transition-colors",
        item.state === "alert"
          ? "border-short/40 hover:bg-short/5"
          : item.state === "watch"
            ? "border-amber/40 hover:bg-amber/5"
            : "border-border/30 hover:bg-card/60",
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          {item.label}
        </span>
        <StatusPill intent={intent} label={item.state.toUpperCase()} compact dot={item.state !== "ready"} />
      </div>
      <span className="truncate text-xs font-medium" title={item.detail}>
        {item.detail}
      </span>
    </div>
  );

  if (item.href) {
    return (
      <Link href={item.href} className="block">
        {content}
      </Link>
    );
  }
  return content;
}
