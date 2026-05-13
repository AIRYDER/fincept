"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  Activity,
  BrainCircuit,
  Briefcase,
  CalendarClock,
  CheckCircle2,
  CircleAlert,
  Coins,
  Cpu,
  Database,
  DollarSign,
  Gauge,
  GitCompareArrows,
  Lightbulb,
  ListChecks,
  Loader2,
  Newspaper,
  Power,
  RadioTower,
  Rocket,
  Search,
  ScrollText,
  Server,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { OperatorBriefingCard } from "@/components/overview/operator-briefing-card";
import { AppShell } from "@/components/shell/app-shell";
import { ConfidenceBar } from "@/components/widgets/confidence-bar";
import { EmptyState } from "@/components/widgets/empty-state";
import { KpiTile } from "@/components/widgets/kpi-tile";
import { PageHeader } from "@/components/widgets/page-header";
import { SideBadge } from "@/components/widgets/side-badge";
import { Sparkline } from "@/components/widgets/sparkline";
import { OrderStatusBadge } from "@/components/widgets/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { FeatureId, Fill, Prediction, ServiceStatus, WsFrame } from "@/lib/types";
import { cn, formatNumber, formatUsd, nsToDate, pnlClass } from "@/lib/utils";
import { useFinceptStream } from "@/lib/ws";
import { formatDistanceToNowStrict } from "date-fns";

interface ActivityItem {
  id: string;
  ts: number;
  kind: "prediction" | "fill" | "alert";
  text: React.ReactNode;
}

function asNum(v: string | null | undefined) {
  if (v === null || v === undefined) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

type FeatureTone = "primary" | "cyan" | "long" | "warn" | "info";

type FeatureVisualStatus = "running" | "partial" | "idle" | "external";

type RecommendationTone = "primary" | "cyan" | "long" | "warn";

interface FeatureLauncher {
  id: FeatureId;
  title: string;
  eyebrow: string;
  description: string;
  services: string[];
  icon: React.ComponentType<{ className?: string }>;
  tone: FeatureTone;
}

interface NextFeatureRecommendation {
  id: string;
  title: string;
  gap: string;
  nextAction: string;
  impact: string;
  icon: React.ComponentType<{ className?: string }>;
  tone: RecommendationTone;
  badge: string;
}

const FEATURE_LAUNCHERS: FeatureLauncher[] = [
  {
    id: "market_data",
    title: "Market data",
    eyebrow: "Data plane",
    description: "Realtime bars, venue streams, and online feature snapshots.",
    services: ["ingestor", "features"],
    icon: Database,
    tone: "cyan",
  },
  {
    id: "news_learning",
    title: "News learning",
    eyebrow: "Learning loop",
    description: "Enrichment and outcome labeling for book-aware news alpha.",
    services: ["information_enricher", "news_outcome_labeler"],
    icon: Newspaper,
    tone: "info",
  },
  {
    id: "jobs",
    title: "Scheduled jobs",
    eyebrow: "Automation",
    description: "EOD loads and candidate training jobs on demand.",
    services: ["jobs"],
    icon: CalendarClock,
    tone: "warn",
  },
  {
    id: "gbm_predictor",
    title: "GBM predictor",
    eyebrow: "Model agent",
    description: "Model-backed prediction stream from online market features.",
    services: ["gbm_predictor"],
    icon: BrainCircuit,
    tone: "long",
  },
  {
    id: "news_alpha_predictor",
    title: "News alpha",
    eyebrow: "Model agent",
    description: "Promoted news-impact model predictions for candidate signals.",
    services: ["news_alpha_predictor"],
    icon: RadioTower,
    tone: "primary",
  },
  {
    id: "sentiment",
    title: "Sentiment",
    eyebrow: "LLM lane",
    description: "Article scoring plus sentiment feature snapshots.",
    services: ["sentiment_agent", "sentiment_features"],
    icon: Cpu,
    tone: "info",
  },
  {
    id: "regime",
    title: "Regime",
    eyebrow: "Macro lane",
    description: "FRED-backed macro regime detection for risk context.",
    services: ["regime_agent"],
    icon: Gauge,
    tone: "warn",
  },
  {
    id: "openbb",
    title: "OpenBB",
    eyebrow: "Research backend",
    description: "Local OpenBB Platform API for research quotes on 127.0.0.1:6900.",
    services: [],
    icon: Search,
    tone: "cyan",
  },
];

const FEATURE_TONE_CLASSES: Record<
  FeatureTone,
  { icon: string; rail: string; glow: string; ring: string }
> = {
  primary: {
    icon: "bg-primary/15 text-primary",
    rail: "bg-primary",
    glow: "from-primary/20",
    ring: "group-hover:border-primary/60",
  },
  cyan: {
    icon: "bg-cyan/10 text-cyan",
    rail: "bg-cyan",
    glow: "from-cyan/15",
    ring: "group-hover:border-cyan/60",
  },
  long: {
    icon: "bg-long/10 text-long",
    rail: "bg-long",
    glow: "from-long/15",
    ring: "group-hover:border-long/60",
  },
  warn: {
    icon: "bg-warn/10 text-warn",
    rail: "bg-warn",
    glow: "from-warn/15",
    ring: "group-hover:border-warn/60",
  },
  info: {
    icon: "bg-info/10 text-info",
    rail: "bg-info",
    glow: "from-info/15",
    ring: "group-hover:border-info/60",
  },
};

const FEATURE_STATUS_META: Record<
  FeatureVisualStatus,
  { label: string; badge: "long" | "warn" | "muted" | "default"; dot: string }
> = {
  running: { label: "Running", badge: "long", dot: "bg-long" },
  partial: { label: "Partial", badge: "warn", dot: "bg-warn" },
  idle: { label: "Idle", badge: "muted", dot: "bg-muted-foreground" },
  external: { label: "Manual", badge: "default", dot: "bg-primary" },
};

export default function HomePage() {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();

  const { data: positions } = useQuery({
    queryKey: ["positions"],
    queryFn: () => api.positions(token),
    enabled: !!token,
    refetchInterval: 15_000,
  });
  const { data: orders } = useQuery({
    queryKey: ["orders", "recent"],
    queryFn: () => api.orders(token, { limit: 50 }),
    enabled: !!token,
    refetchInterval: 15_000,
  });
  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: () => api.strategies(token),
    enabled: !!token,
    refetchInterval: 30000,
  });

  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [equityHistory, setEquityHistory] = useState<{ x: number; y: number }[]>(
    [],
  );

  // Live stream: predictions, fills, alerts.
  useFinceptStream({
    topics: ["predictions", "fills", "alerts"],
    onFrame: (frame: WsFrame) => {
      const item = toActivity(frame);
      if (item) {
        setActivity((prev) => [item, ...prev].slice(0, 50));
      }
      if (frame.topic === "fills") {
        queryClient.invalidateQueries({ queryKey: ["orders"] });
        queryClient.invalidateQueries({ queryKey: ["positions"] });
        queryClient.invalidateQueries({ queryKey: ["strategies"] });
      }
    },
  });

  // Compute KPIs.
  const kpis = useMemo(() => {
    const equity = (positions ?? []).reduce(
      (acc, p) => acc + asNum(p.realized_pnl) + asNum(p.unrealized_pnl),
      0,
    );
    const unrealized = (positions ?? []).reduce(
      (acc, p) => acc + asNum(p.unrealized_pnl),
      0,
    );
    const open = (positions ?? []).filter((p) => asNum(p.quantity) !== 0).length;
    const fills24h = (orders ?? []).filter((o) => o.status === "filled").length;
    return { equity, unrealized, open, fills24h };
  }, [positions, orders]);

  // Build a "live" sparkline of equity by sampling whenever positions
  // refetch.  This is intentionally cheap - real PnL chart lives on
  // /positions page once we wire the time-series back-end.
  useEffect(() => {
    if (equityHistory.length === 0 && positions !== undefined) {
      setEquityHistory([{ x: Date.now(), y: kpis.equity }]);
    }
  }, [equityHistory.length, kpis.equity, positions]);

  return (
    <AppShell>
      <PageHeader
        title="Overview"
        description="One pane to see everything: equity, exposure, recent decisions, live signals."
        action={
          <div className="flex flex-wrap justify-end gap-2">
            <Badge variant="long">WebSocket live</Badge>
            <Badge variant="muted">Auto-refresh · 15s</Badge>
          </div>
        }
      />

      {/* Operator briefing — aggregates safety, services, recon, strategies, receipts */}
      <div className="mb-4">
        <OperatorBriefingCard />
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile
          label="Total equity P&L"
          value={formatUsd(kpis.equity, { signed: true, compact: false })}
          icon={DollarSign}
          delta={kpis.equity}
          sub="Realized + unrealized"
        >
          <Sparkline
            data={equityHistory.length > 1 ? equityHistory : [{ x: 0, y: 0 }, { x: 1, y: kpis.equity }]}
            positive={kpis.equity >= 0}
          />
        </KpiTile>

        <KpiTile
          label="Unrealized P&L"
          value={formatUsd(kpis.unrealized, { signed: true })}
          icon={TrendingUp}
          delta={kpis.unrealized}
          sub="Mark-to-market on open positions"
        />

        <KpiTile
          label="Open positions"
          value={String(kpis.open)}
          icon={Briefcase}
          sub={`${(positions ?? []).length} symbols tracked`}
        />

        <KpiTile
          label="Fills today"
          value={String(kpis.fills24h)}
          icon={ScrollText}
          sub={`${(orders ?? []).length} recent orders`}
        />
      </div>

      <FeatureLaunchPanel />

      {/* Activity + strategy grid */}
      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
            <CardTitle className="flex items-center gap-2">
              <Activity className="h-3.5 w-3.5" />
              Live activity
            </CardTitle>
            <Badge variant="muted">WebSocket</Badge>
          </CardHeader>
          <CardContent className="px-0 pb-3">
            {activity.length === 0 ? (
              <div className="px-6 pb-3">
                <EmptyState
                  icon={Sparkles}
                  title="Listening for events…"
                  description="Predictions, fills, and alerts will appear here as they happen."
                />
              </div>
            ) : (
              <ScrollArea className="h-72">
                <ul className="divide-y divide-border/50">
                  {activity.map((item) => (
                    <motion.li
                      key={item.id}
                      initial={{ opacity: 0, x: -8 }}
                      animate={{ opacity: 1, x: 0 }}
                      className="flex items-center gap-3 px-6 py-2.5 text-sm"
                    >
                      <Badge
                        variant={
                          item.kind === "alert"
                            ? "destructive"
                            : item.kind === "fill"
                              ? "long"
                              : "default"
                        }
                        className="w-20 justify-center"
                      >
                        {item.kind.toUpperCase()}
                      </Badge>
                      <span className="flex-1 truncate">{item.text}</span>
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {ago(item.ts)}
                      </span>
                    </motion.li>
                  ))}
                </ul>
              </ScrollArea>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2">
              <Coins className="h-3.5 w-3.5" />
              Strategies
            </CardTitle>
            <CardDescription>
              Known strategies in the portfolio store
            </CardDescription>
          </CardHeader>
          <CardContent>
            {(strategies ?? []).length === 0 ? (
              <EmptyState
                icon={Coins}
                title="No strategies yet"
                description="They appear here once an OMS fill creates the first position."
              />
            ) : (
              <ul className="space-y-2">
                {strategies?.map((s) => (
                  <li
                    key={s.strategy_id}
                    className="flex items-center justify-between rounded-md border border-border/40 bg-card/60 px-3 py-2 text-sm"
                  >
                    <span className="font-mono text-xs">{s.strategy_id}</span>
                    <span className="flex items-center gap-2">
                      <Badge variant="muted">{s.position_count} sym</Badge>
                      <Badge variant="long">{s.open_positions} open</Badge>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Top positions strip */}
      <Card className="mt-6">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
          <CardTitle>Top positions by |unrealized P&L|</CardTitle>
          <Badge variant="muted">Snapshot</Badge>
        </CardHeader>
        <CardContent className="overflow-x-auto px-0">
          <table className="w-full text-sm">
            <thead className="text-[11px] uppercase tracking-wider text-muted-foreground">
              <tr className="border-b border-border/40">
                <th className="px-6 py-2 text-left">Symbol</th>
                <th className="px-6 py-2 text-left">Strategy</th>
                <th className="px-6 py-2 text-right">Qty</th>
                <th className="px-6 py-2 text-right">Avg entry</th>
                <th className="px-6 py-2 text-right">Mark</th>
                <th className="px-6 py-2 text-right">Unrealized</th>
                <th className="px-6 py-2 text-right">Realized</th>
              </tr>
            </thead>
            <tbody>
              {(positions ?? [])
                .filter((p) => asNum(p.quantity) !== 0)
                .sort(
                  (a, b) =>
                    Math.abs(asNum(b.unrealized_pnl)) -
                    Math.abs(asNum(a.unrealized_pnl)),
                )
                .slice(0, 8)
                .map((p) => {
                  const qty = asNum(p.quantity);
                  const avg = asNum(p.avg_cost);
                  // Prefer live mark from the server; fall back to implied
                  // (avg_cost + unrealized/qty) until the scheduler has
                  // delivered a mark for this symbol.
                  const mark = p.mark_px
                    ? asNum(p.mark_px)
                    : qty !== 0
                      ? avg + asNum(p.unrealized_pnl) / Math.abs(qty)
                      : avg;
                  return (
                    <tr
                      key={`${p.strategy_id}:${p.symbol}`}
                      className="border-b border-border/30 last:border-b-0 hover:bg-accent/40"
                    >
                      <td className="px-6 py-2 font-mono">{p.symbol}</td>
                      <td className="px-6 py-2 font-mono text-xs text-muted-foreground">
                        {p.strategy_id}
                      </td>
                      <td className="num px-6 py-2 text-right">
                        {formatNumber(p.quantity, 6)}
                      </td>
                      <td className="num px-6 py-2 text-right text-muted-foreground">
                        {formatUsd(p.avg_cost)}
                      </td>
                      <td
                        className={cn(
                          "num px-6 py-2 text-right",
                          p.mark_px ? "text-foreground" : "text-muted-foreground",
                        )}
                        title={p.mark_px ? "Live mark" : "Implied from P&L"}
                      >
                        {formatUsd(mark)}
                      </td>
                      <td
                        className={cn(
                          "num px-6 py-2 text-right",
                          pnlClass(p.unrealized_pnl),
                        )}
                      >
                        {formatUsd(p.unrealized_pnl, { signed: true })}
                      </td>
                      <td
                        className={cn(
                          "num px-6 py-2 text-right",
                          pnlClass(p.realized_pnl),
                        )}
                      >
                        {formatUsd(p.realized_pnl, { signed: true })}
                      </td>
                    </tr>
                  );
                })}
              {(positions ?? []).filter((p) => asNum(p.quantity) !== 0).length ===
              0 ? (
                <tr>
                  <td colSpan={7}>
                    <EmptyState
                      icon={Briefcase}
                      title="No open positions"
                      description="Your first fill will land here automatically."
                      className="m-4"
                    />
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {/* Live predictions strip */}
      <LivePredictions />
    </AppShell>
  );
}

function FeatureLaunchPanel() {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const servicesQ = useQuery({
    queryKey: ["services"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  const refreshServices = () => {
    queryClient.invalidateQueries({ queryKey: ["services"] });
    window.setTimeout(() => {
      queryClient.invalidateQueries({ queryKey: ["services"] });
    }, 2500);
  };
  const start = useMutation({
    mutationFn: (featureId: FeatureId) => api.startFeature(token, featureId),
    onSuccess: refreshServices,
  });
  const stop = useMutation({
    mutationFn: (featureId: FeatureId) => api.stopFeature(token, featureId),
    onSuccess: refreshServices,
  });
  const restart = useMutation({
    mutationFn: (featureId: FeatureId) => api.restartFeature(token, featureId),
    onSuccess: refreshServices,
  });
  const [logFeature, setLogFeature] = useState<FeatureId | null>(null);
  const logsQ = useQuery({
    queryKey: ["features", logFeature, "logs"],
    queryFn: () => api.featureLogs(token, logFeature!),
    enabled: !!token && !!logFeature,
  });
  const servicesByName = useMemo(() => {
    return new Map<string, ServiceStatus>(
      (servicesQ.data?.services ?? []).map((service) => [service.name, service]),
    );
  }, [servicesQ.data]);
  const featureStates = useMemo(() => {
    return FEATURE_LAUNCHERS.map((feature) => {
      const rows = feature.services.map((name) => servicesByName.get(name));
      const up = rows.filter((row) => row?.status === "up").length;
      const stale = rows.filter((row) => row?.status === "stale").length;
      const status: FeatureVisualStatus =
        feature.services.length === 0
          ? "external"
          : up === feature.services.length
            ? "running"
            : up > 0 || stale > 0
              ? "partial"
              : "idle";
      return { feature, rows, up, stale, status };
    });
  }, [servicesByName]);
  const runningCount = featureStates.filter((item) => item.status === "running").length;
  const partialCount = featureStates.filter((item) => item.status === "partial").length;
  const activeControlId = start.variables ?? stop.variables ?? restart.variables ?? null;
  const controlPending = start.isPending || stop.isPending || restart.isPending;
  const controlError = start.error ?? stop.error ?? restart.error;
  const error = controlError instanceof Error ? controlError.message : null;
  const latestControl = restart.data ?? stop.data ?? start.data;
  const launchMessage = latestControl
    ? `${latestControl.feature_id.replaceAll("_", " ")} · ${latestControl.status.replaceAll("_", " ")}`
    : null;

  return (
    <Card className="relative mt-6 overflow-hidden border-primary/20 bg-card">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,hsl(var(--primary)/0.14),transparent_32%),radial-gradient(circle_at_bottom_right,hsl(var(--cyan)/0.08),transparent_36%)]" />
      <CardHeader className="relative flex flex-col gap-3 border-primary/20 bg-transparent p-4 md:flex-row md:items-start md:justify-between">
        <div className="max-w-3xl">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <Badge variant="default" className="gap-1">
              <Server className="h-3 w-3" />
              Lean core online
            </Badge>
            <Badge variant={servicesQ.isFetching ? "warn" : "muted"}>
              Services · 15s
            </Badge>
          </div>
          <CardTitle className="text-sm tracking-[0.16em]">
            <Rocket className="h-4 w-4 text-primary" />
            Feature control center
          </CardTitle>
          <CardDescription className="mt-1 max-w-2xl text-xs leading-relaxed">
            Start heavier data, research, model, and macro lanes only when the workflow needs them. Core portfolio execution stays lean by default.
          </CardDescription>
        </div>
        <div className="grid min-w-56 grid-cols-3 gap-2 text-center">
          <div className="border border-border/60 bg-background/45 px-2 py-2">
            <div className="num text-lg font-semibold text-long">{runningCount}</div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Running</div>
          </div>
          <div className="border border-border/60 bg-background/45 px-2 py-2">
            <div className="num text-lg font-semibold text-warn">{partialCount}</div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Partial</div>
          </div>
          <div className="border border-border/60 bg-background/45 px-2 py-2">
            <div className="num text-lg font-semibold text-cyan">{FEATURE_LAUNCHERS.length}</div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Features</div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="relative space-y-3 p-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          {featureStates.map(({ feature, rows, up, stale, status }) => {
            const Icon = feature.icon;
            const tone = FEATURE_TONE_CLASSES[feature.tone];
            const meta = FEATURE_STATUS_META[status];
            const pending = controlPending && activeControlId === feature.id;
            const launched = latestControl?.feature_id === feature.id;
            return (
              <motion.div
                key={feature.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                className={cn(
                  "group relative flex min-h-52 flex-col justify-between overflow-hidden border border-border/50 bg-background/55 p-3 transition-colors hover:bg-accent/30",
                  tone.ring,
                )}
              >
                <div className={cn("absolute inset-y-0 left-0 w-1", tone.rail)} />
                <div className={cn("pointer-events-none absolute inset-x-0 top-0 h-24 bg-gradient-to-b to-transparent opacity-70", tone.glow)} />
                <div className="relative space-y-3 pl-1">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <div className={cn("border border-border/60 p-2", tone.icon)}>
                        <Icon className="h-4 w-4" />
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                          {feature.eyebrow}
                        </div>
                        <h3 className="text-sm font-semibold text-foreground">
                          {feature.title}
                        </h3>
                      </div>
                    </div>
                    <Badge variant={meta.badge} className="gap-1">
                      <span className={cn("h-1.5 w-1.5 rounded-full", meta.dot)} />
                      {launched && status !== "running" ? "Requested" : meta.label}
                    </Badge>
                  </div>
                  <p className="min-h-10 text-xs leading-relaxed text-muted-foreground">
                    {feature.description}
                  </p>
                  <div className="flex items-center justify-between border border-border/50 bg-card/60 px-2 py-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                    <span>Services</span>
                    <span className="num text-foreground">
                      {feature.services.length ? `${up}/${feature.services.length} up` : "external"}
                    </span>
                  </div>
                  {feature.services.length > 0 ? (
                    <div className="grid grid-cols-1 gap-1.5">
                      {feature.services.map((name, index) => {
                        const service = rows[index];
                        const serviceStatus = service?.status ?? "down";
                        return (
                          <div
                            key={name}
                            className="flex items-center justify-between gap-2 border border-border/40 bg-card/40 px-2 py-1 text-[10px]"
                          >
                            <span className="truncate font-mono text-muted-foreground">
                              {name}
                            </span>
                            <Badge
                              variant={
                                serviceStatus === "up"
                                  ? "long"
                                  : serviceStatus === "stale"
                                    ? "warn"
                                    : "muted"
                              }
                            >
                              {serviceStatus}
                            </Badge>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="border border-dashed border-border/60 bg-card/30 px-2 py-2 text-[10px] leading-relaxed text-muted-foreground">
                      No heartbeat lane; launcher targets the local OpenBB Platform API, not the Workspace Connect Backend modal.
                    </div>
                  )}
                </div>
                <div className="relative mt-3 grid grid-cols-3 gap-1.5">
                  <Button
                    size="sm"
                    variant={status === "running" ? "outline" : "default"}
                    disabled={pending || status === "running"}
                    onClick={() => start.mutate(feature.id)}
                    className="px-2"
                  >
                    {pending && activeControlId === feature.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : status === "running" ? (
                      <CheckCircle2 className="h-3.5 w-3.5" />
                    ) : (
                      <Power className="h-3.5 w-3.5" />
                    )}
                    Start
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={pending || status === "idle"}
                    onClick={() => restart.mutate(feature.id)}
                    className="px-2"
                  >
                    {pending && restart.variables === feature.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Rocket className="h-3.5 w-3.5" />
                    )}
                    Restart
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={pending || status === "idle"}
                    onClick={() => stop.mutate(feature.id)}
                    className="px-2 text-short hover:text-short"
                  >
                    {pending && stop.variables === feature.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <CircleAlert className="h-3.5 w-3.5" />
                    )}
                    Stop
                  </Button>
                </div>
                <button
                  type="button"
                  onClick={() => setLogFeature(feature.id)}
                  className="relative mt-2 text-left text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground"
                >
                  View last control output
                </button>
              </motion.div>
            );
          })}
        </div>
        <div className="flex flex-col gap-2 border border-border/50 bg-background/45 px-3 py-2 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <span className="flex items-center gap-2">
            <span className="live-dot" />
            Optional lanes report through Redis heartbeats when they come online.
          </span>
          <span className="font-mono uppercase tracking-wider">
            {launchMessage ?? `${servicesQ.data?.summary.up ?? 0}/${servicesQ.data?.summary.expected ?? 0} core up`}
          </span>
        </div>
        {logFeature ? (
          <div className="border border-border/60 bg-card/70 p-3 text-xs">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="font-semibold uppercase tracking-wider text-cyan">
                {logFeature.replaceAll("_", " ")} · last control output
              </div>
              <Button size="sm" variant="ghost" onClick={() => setLogFeature(null)}>
                Close
              </Button>
            </div>
            <pre className="max-h-36 overflow-auto whitespace-pre-wrap bg-background/70 p-2 font-mono text-[11px] text-muted-foreground">
              {logsQ.isLoading
                ? "Loading…"
                : logsQ.data?.last_control?.output ||
                  logsQ.data?.last_control?.status ||
                  "No control output recorded yet."}
            </pre>
          </div>
        ) : null}
        {error ? (
          <div className="flex items-center gap-2 border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            <CircleAlert className="h-3.5 w-3.5" />
            {error}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function LivePredictions() {
  const token = useAuth((s) => s.token);
  const [latest, setLatest] = useState<Map<string, Prediction>>(new Map());
  const recent = useQuery({
    queryKey: ["models", "gbm_predictor", "predictions", "overview"],
    queryFn: () => api.modelPredictions(token, "gbm_predictor", { limit: 24 }),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  useEffect(() => {
    if (!recent.data) return;
    setLatest((prev) => {
      const next = new Map(prev);
      for (const row of recent.data.predictions) {
        const prediction: Prediction = {
          agent_id: recent.data.agent_id,
          symbol: row.symbol,
          horizon_ns: row.horizon_ns,
          ts_event: row.ts_event,
          direction: row.direction,
          confidence: row.confidence,
          calibration_tag: "gbm.v1",
        };
        next.set(`${prediction.agent_id}:${prediction.symbol}`, prediction);
      }
      return next;
    });
  }, [recent.data]);
  useFinceptStream({
    topics: ["predictions"],
    onFrame: (frame) => {
      if (frame.topic !== "predictions") return;
      const p = frame.event.payload;
      setLatest((prev) => {
        const next = new Map(prev);
        next.set(`${p.agent_id}:${p.symbol}`, p);
        return next;
      });
    },
  });
  const rows = Array.from(latest.values()).sort(
    (a, b) => b.ts_event - a.ts_event,
  );
  return (
    <Card className="mt-6">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="h-3.5 w-3.5 text-primary" />
          Live predictions
        </CardTitle>
        <Badge variant="default">Realtime</Badge>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <EmptyState
            icon={Sparkles}
            title="Waiting for first prediction"
            description="The gbm_predictor agent posts new direction signals every minute."
          />
        ) : (
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
            {rows.slice(0, 12).map((p) => (
              <motion.div
                key={`${p.agent_id}:${p.symbol}`}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                className="flex items-center gap-3 rounded-md border border-border/40 bg-card/60 px-3 py-2"
              >
                <div className="flex flex-1 flex-col">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-sm">{p.symbol}</span>
                    <Badge variant="muted">{p.agent_id}</Badge>
                  </div>
                  <ConfidenceBar
                    direction={p.direction}
                    confidence={p.confidence}
                  />
                  <div className="mt-1 flex items-center justify-between text-[10px] text-muted-foreground">
                    <span
                      className={cn(
                        "font-mono",
                        p.direction >= 0 ? "text-long" : "text-short",
                      )}
                    >
                      dir {p.direction.toFixed(2)}
                    </span>
                    <span className="font-mono">
                      conf {(p.confidence * 100).toFixed(0)}%
                    </span>
                    <span className="font-mono">{ago(p.ts_event)}</span>
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
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

function toActivity(frame: WsFrame): ActivityItem | null {
  if (frame.topic === "predictions") {
    const p = frame.event.payload as Prediction;
    return {
      id: `p:${p.agent_id}:${p.symbol}:${p.ts_event}`,
      ts: p.ts_event,
      kind: "prediction",
      text: (
        <span>
          <span className="font-mono">{p.symbol}</span>{" "}
          <span
            className={cn(p.direction >= 0 ? "text-long" : "text-short")}
          >
            {p.direction >= 0 ? "▲" : "▼"} {p.direction.toFixed(2)}
          </span>{" "}
          <span className="text-muted-foreground">
            conf {(p.confidence * 100).toFixed(0)}% · {p.agent_id}
          </span>
        </span>
      ),
    };
  }
  if (frame.topic === "fills") {
    const f = frame.event.payload as Fill;
    return {
      id: `f:${f.fill_id}`,
      ts: f.ts_event,
      kind: "fill",
      text: (
        <span className="flex items-center gap-2">
          <SideBadge side={f.side} />
          <span className="font-mono">{f.symbol}</span>
          <span className="num">{formatNumber(f.quantity, 6)}</span>
          <span className="text-muted-foreground">@ {formatUsd(f.price)}</span>
          <span className="text-muted-foreground">· {f.strategy_id}</span>
        </span>
      ),
    };
  }
  if (frame.topic === "alerts") {
    const a = frame.event.payload;
    return {
      id: `a:${a.alert_id}`,
      ts: a.ts_event,
      kind: "alert",
      text: (
        <span>
          <Badge
            variant={a.severity === "critical" ? "destructive" : "warn"}
            className="mr-2"
          >
            {a.severity.toUpperCase()}
          </Badge>
          {a.code}: {a.message}
        </span>
      ),
    };
  }
  // We don't surface raw position events on the home feed - the table
  // above shows the consolidated view.
  void OrderStatusBadge;
  return null;
}
