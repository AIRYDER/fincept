"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  Bell,
  Brain,
  CircleDot,
  Database,
  Power,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  AlertEvent,
  ModelRecord,
  RegimeResponse,
  ServiceStatus,
  WsFrame,
} from "@/lib/types";
import { cn, formatUsd, nsToDate } from "@/lib/utils";
import { useFinceptStream } from "@/lib/ws";
import { formatDistanceToNowStrict } from "date-fns";

const PER_SYMBOL_CAP = 20_000; // mirrors the default in fincept_core.config
const GROSS_CAP = 250_000;

function asNum(v: string | null | undefined) {
  if (v == null) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export default function RiskPage() {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();

  const [killOpen, setKillOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [killState, setKillState] = useState<"clear" | "engaged">("clear");
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);

  const { data: positions } = useQuery({
    queryKey: ["positions", "risk"],
    queryFn: () => api.positions(token, true),
    enabled: !!token,
    refetchInterval: 5000,
  });

  const { data: servicesData } = useQuery({
    queryKey: ["services"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 5000,
    staleTime: 2_000,
  });

  const { data: regimeData } = useQuery({
    queryKey: ["regime", "latest"],
    queryFn: () => api.regime(token, 5),
    enabled: !!token,
    // Regime updates ~hourly; 60s is plenty.  Adds a freshness ticker
    // by re-rendering age_seconds each refetch.
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: () => api.models(token),
    enabled: !!token,
    // Models change only on retrain; polling once a minute is fine
    // for the staleness badge (age_seconds) to feel live.
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const tripMut = useMutation({
    mutationFn: (r: string) => api.tripKillSwitch(token, r),
    onSuccess: () => {
      setKillState("engaged");
      setKillOpen(false);
      setReason("");
      queryClient.invalidateQueries();
    },
  });
  const clearMut = useMutation({
    mutationFn: () => api.clearKillSwitch(token),
    onSuccess: () => {
      setKillState("clear");
      queryClient.invalidateQueries();
    },
  });

  const onFrame = useCallback((frame: WsFrame) => {
    if (frame.topic !== "alerts") return;
    const a = frame.event.payload;
    setAlerts((prev) => [a, ...prev].slice(0, 50));
    if (a.code === "kill_switch_engaged") setKillState("engaged");
    if (a.code === "kill_switch_cleared") setKillState("clear");
  }, []);
  useFinceptStream({ topics: ["alerts"], onFrame });

  // Per-symbol notional aggregation.  Position carries cost basis only;
  // a future enhancement reads marks from the live-prices stream.
  const exposureBySymbol = new Map<string, number>();
  for (const p of positions ?? []) {
    const cost = asNum(p.avg_cost);
    const market = cost * Math.abs(asNum(p.quantity)) + asNum(p.unrealized_pnl);
    exposureBySymbol.set(
      p.symbol,
      (exposureBySymbol.get(p.symbol) ?? 0) + market,
    );
  }
  const grossExposure = Array.from(exposureBySymbol.values()).reduce(
    (acc, v) => acc + v,
    0,
  );

  return (
    <AppShell>
      <PageHeader
        title="Risk"
        description="Kill switch, exposure breakdown, and the live alert feed.  All limits are enforced inside the OMS via the risk gate library."
        action={
          <Badge
            variant={killState === "engaged" ? "destructive" : "long"}
            className="px-3 py-1"
          >
            {killState === "engaged" ? "KILL SWITCH ENGAGED" : "ALL CLEAR"}
          </Badge>
        }
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[20rem_1fr]">
        {/* Big kill switch */}
        <Card
          className={cn(
            "relative overflow-hidden border-2",
            killState === "engaged"
              ? "border-destructive/60"
              : "border-border/40",
          )}
        >
          <CardHeader>
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <Power className="h-4 w-4 text-destructive" />
              Kill switch
            </CardTitle>
            <CardDescription>
              Tripping the switch publishes a critical alert that the OMS,
              orchestrator, and agents subscribe to.  Open orders should be
              cancelled and new orders rejected until cleared.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {killState === "engaged" ? (
              <motion.div
                initial={{ scale: 0.95 }}
                animate={{ scale: [1, 1.02, 1] }}
                transition={{ duration: 1.5, repeat: Infinity }}
                className="flex flex-col items-center gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4"
              >
                <ShieldAlert className="h-8 w-8 text-destructive" />
                <span className="text-sm font-medium text-destructive">
                  Trading halted
                </span>
              </motion.div>
            ) : (
              <div className="flex flex-col items-center gap-3 rounded-lg border border-long/30 bg-long/5 p-4">
                <ShieldCheck className="h-8 w-8 text-long" />
                <span className="text-sm font-medium text-long">
                  Risk gate active · all limits enforced
                </span>
              </div>
            )}

            <Button
              variant="destructive"
              size="xl"
              className="w-full"
              onClick={() => setKillOpen(true)}
              disabled={killState === "engaged"}
            >
              <Power className="mr-2 h-5 w-5" />
              Trip kill switch
            </Button>
            <Button
              variant="outline"
              size="lg"
              className="w-full"
              onClick={() => clearMut.mutate()}
              disabled={killState === "clear" || clearMut.isPending}
            >
              {clearMut.isPending ? "Clearing…" : "Clear kill switch"}
            </Button>
          </CardContent>
        </Card>

        {/* Exposure */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle>Exposure vs. caps</CardTitle>
            <CardDescription>
              Aggregated notional exposure by symbol.  Caps shown are the
              defaults from <code>fincept_core.config</code>.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <UsageBar
                label="Per-symbol cap (USD)"
                cap={PER_SYMBOL_CAP}
                used={Math.max(...exposureBySymbol.values(), 0)}
              />
              <UsageBar
                label="Gross portfolio cap (USD)"
                cap={GROSS_CAP}
                used={grossExposure}
              />
            </div>

            {exposureBySymbol.size === 0 ? (
              <EmptyState
                icon={ShieldCheck}
                title="No open exposure"
                description="No active positions; the gate has nothing to constrain."
              />
            ) : (
              <div className="space-y-2">
                {Array.from(exposureBySymbol.entries())
                  .sort((a, b) => b[1] - a[1])
                  .map(([sym, value]) => {
                    const pct = Math.min(100, (value / PER_SYMBOL_CAP) * 100);
                    const danger = pct > 85;
                    return (
                      <div
                        key={sym}
                        className="rounded-md border border-border/40 bg-background/30 p-3"
                      >
                        <div className="flex items-center justify-between text-sm">
                          <span className="font-mono">{sym}</span>
                          <span className="font-mono text-xs">
                            {formatUsd(value, { compact: true })} /{" "}
                            <span className="text-muted-foreground">
                              {formatUsd(PER_SYMBOL_CAP, { compact: true })}
                            </span>
                          </span>
                        </div>
                        <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted/50">
                          <div
                            className={cn(
                              "h-full transition-all",
                              danger ? "bg-destructive" : "bg-primary",
                            )}
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                      </div>
                    );
                  })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Services Health */}
      <Card className="mt-6">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <Activity className="h-4 w-4 text-primary" />
            Services Health
          </CardTitle>
          <CardDescription>
            Live heartbeat from each background service. UP = beat within{" "}
            {servicesData?.summary.stale_after_sec ?? 15}s, STALE = older but
            still in TTL, DOWN = no key (crashed or never started).
          </CardDescription>
        </CardHeader>
        <CardContent>
          {servicesData ? (
            <>
              <div className="mb-3 flex items-center gap-3 text-xs">
                <span className="font-mono text-muted-foreground">
                  {servicesData.summary.up} / {servicesData.summary.expected}{" "}
                  expected services up
                </span>
                <span className="font-mono text-muted-foreground">
                  · TTL {servicesData.summary.ttl_sec}s
                </span>
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                {servicesData.services.map((s: ServiceStatus) => (
                  <ServiceTile key={s.name} svc={s} />
                ))}
              </div>
            </>
          ) : (
            <EmptyState
              icon={Activity}
              title="Loading service status..."
              description="Polling /services every 5s."
            />
          )}
        </CardContent>
      </Card>

      {/* Regime panel */}
      <Card className="mt-6">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <Brain className="h-4 w-4 text-primary" />
            Macro Regime
          </CardTitle>
          <CardDescription>
            Latest classification from <code className="font-mono">regime_agent</code> with the
            FRED inputs that drove it.  Tilts the consensus by{" "}
            <code className="font-mono">direction_bias</code>.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <RegimePanel data={regimeData} />
        </CardContent>
      </Card>

      {/* Models panel */}
      <Card className="mt-6">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <Database className="h-4 w-4 text-primary" />
            Models
          </CardTitle>
          <CardDescription>
            Trained models with their evaluation provenance.  CV-evaluated
            models show mean ± std AUC across folds; legacy holdout models
            show a single-split AUC.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {modelsData?.models?.length ? (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {modelsData.models.map((m: ModelRecord) => (
                <ModelCard key={m.name} m={m} />
              ))}
            </div>
          ) : (
            <EmptyState
              icon={Database}
              title="No models found"
              description={`Train one with: python -m agents.gbm_predictor.train --input data/X.parquet --cv-folds 5 --out-dir ${modelsData?.summary?.models_dir ?? "models/gbm_predictor"}`}
            />
          )}
        </CardContent>
      </Card>

      {/* Alert feed */}
      <Card className="mt-6">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
          <CardTitle className="flex items-center gap-2">
            <Bell className="h-3.5 w-3.5" />
            Alert feed
          </CardTitle>
          <Badge variant="muted">{alerts.length}</Badge>
        </CardHeader>
        <CardContent className="px-0">
          {alerts.length === 0 ? (
            <div className="px-6 pb-6">
              <EmptyState
                icon={Bell}
                title="No alerts received"
                description="When any service emits to events.alerts, you'll see it here in realtime."
              />
            </div>
          ) : (
            <ScrollArea className="h-72">
              <ul className="divide-y divide-border/40">
                {alerts.map((a) => (
                  <li
                    key={a.alert_id}
                    className="flex items-start gap-3 px-6 py-3 text-sm"
                  >
                    <Badge
                      variant={
                        a.severity === "critical"
                          ? "destructive"
                          : a.severity === "warning"
                            ? "warn"
                            : "muted"
                      }
                      className="w-20 justify-center"
                    >
                      {a.severity.toUpperCase()}
                    </Badge>
                    <div className="flex-1">
                      <div className="font-mono text-xs text-muted-foreground">
                        {a.code} · {a.source}
                      </div>
                      <div>{a.message}</div>
                    </div>
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {ago(a.ts_event)}
                    </span>
                  </li>
                ))}
              </ul>
            </ScrollArea>
          )}
        </CardContent>
      </Card>

      {/* Confirm dialog */}
      <Dialog open={killOpen} onOpenChange={setKillOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-destructive" />
              Trip the kill switch?
            </DialogTitle>
            <DialogDescription>
              All running services will halt new orders and attempt to
              cancel open ones.  Provide a short reason for the audit
              log.
            </DialogDescription>
          </DialogHeader>
          <Input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. operator-pause, model-anomaly, liquidity-crunch"
            autoFocus
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setKillOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => tripMut.mutate(reason || "manual")}
              disabled={tripMut.isPending}
            >
              {tripMut.isPending ? "Engaging…" : "Trip kill switch"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </AppShell>
  );
}

function UsageBar({
  label,
  cap,
  used,
}: {
  label: string;
  cap: number;
  used: number;
}) {
  const pct = Math.min(100, (used / cap) * 100);
  const danger = pct > 85;
  return (
    <div className="rounded-md border border-border/40 bg-background/30 p-3">
      <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="num text-lg font-semibold">
          {formatUsd(used, { compact: true })}
        </span>
        <span className="text-xs text-muted-foreground">
          / {formatUsd(cap, { compact: true })}
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted/50">
        <div
          className={cn(
            "h-full transition-all",
            danger ? "bg-destructive" : "bg-long",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
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

// --- Regime helpers ----------------------------------------------------

const REGIME_STYLES: Record<
  string,
  { label: string; classes: string; dot: string }
> = {
  risk_on: {
    label: "RISK ON",
    classes: "text-long border-long/40 bg-long/5",
    dot: "bg-long animate-pulse-slow",
  },
  neutral: {
    label: "NEUTRAL",
    classes: "text-muted-foreground border-border/60 bg-muted/10",
    dot: "bg-muted-foreground",
  },
  high_vol: {
    label: "HIGH VOL",
    classes: "text-warn border-warn/40 bg-warn/5",
    dot: "bg-warn",
  },
  risk_off: {
    label: "RISK OFF",
    classes: "text-destructive border-destructive/40 bg-destructive/5",
    dot: "bg-destructive",
  },
};

function RegimePanel({ data }: { data: RegimeResponse | undefined }) {
  if (!data) {
    return (
      <EmptyState
        icon={Brain}
        title="Loading regime..."
        description="Polling /regime every 60s."
      />
    );
  }
  if (data.status !== "ok" || !data.snapshot) {
    return (
      <EmptyState
        icon={Brain}
        title="Regime agent inactive"
        description="Set FRED_API_KEY and start the regime_agent service to populate this panel."
      />
    );
  }
  const snap = data.snapshot;
  const style = REGIME_STYLES[snap.regime] ?? REGIME_STYLES.neutral;
  const ageLabel =
    snap.age_seconds == null
      ? "unknown"
      : snap.age_seconds < 60
        ? `${Math.round(snap.age_seconds)}s ago`
        : snap.age_seconds < 3600
          ? `${Math.round(snap.age_seconds / 60)}m ago`
          : `${Math.round(snap.age_seconds / 3600)}h ago`;
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div
        className={cn(
          "flex flex-col gap-2 rounded-md border p-4",
          style.classes,
        )}
      >
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-widest">
            Current regime
          </span>
          <CircleDot className={cn("h-2.5 w-2.5 rounded-full", style.dot)} />
        </div>
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-2xl font-bold">{style.label}</span>
          <span className="font-mono text-sm text-muted-foreground">
            conf {(snap.confidence * 100).toFixed(0)}%
          </span>
        </div>
        <div className="text-xs text-muted-foreground">{snap.rationale}</div>
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11px] font-mono uppercase tracking-widest text-muted-foreground">
          <span>tilt {snap.direction_bias > 0 ? "+" : ""}
            {(snap.direction_bias * 100).toFixed(0)}%</span>
          <span>updated {ageLabel}</span>
          <span>agent {snap.agent_id}</span>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <RegimeMetric label="VIX" value={snap.vix?.toFixed(1) ?? "—"} />
        <RegimeMetric
          label="10Y-2Y"
          value={snap.yield_spread?.toFixed(2) ?? "—"}
        />
        <RegimeMetric
          label="Fed funds"
          value={
            snap.fed_funds != null ? `${snap.fed_funds.toFixed(2)}%` : "—"
          }
        />
        <div className="col-span-3 rounded-md border border-border/40 bg-background/30 p-3">
          <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
            Recent changes
          </div>
          {data.history.length === 0 ? (
            <div className="mt-1 text-xs text-muted-foreground">
              no entries yet
            </div>
          ) : (
            <ul className="mt-1 space-y-1 text-xs font-mono">
              {data.history.slice(0, 5).map((h) => {
                const r = h.regime ?? "unknown";
                const s = REGIME_STYLES[r] ?? REGIME_STYLES.neutral;
                return (
                  <li
                    key={h.stream_id}
                    className="flex items-center justify-between"
                  >
                    <span className={cn("px-1", s.classes)}>{s.label}</span>
                    <span className="text-muted-foreground">
                      conf {(h.confidence ? h.confidence * 100 : 0).toFixed(0)}%
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function RegimeMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border/40 bg-background/30 p-3">
      <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 font-mono text-lg font-semibold">{value}</div>
    </div>
  );
}

// --- Models helpers ----------------------------------------------------

function ModelCard({ m }: { m: ModelRecord }) {
  const ageLabel =
    m.age_seconds == null
      ? "unknown"
      : m.age_seconds < 60
        ? `${Math.round(m.age_seconds)}s ago`
        : m.age_seconds < 3600
          ? `${Math.round(m.age_seconds / 60)}m ago`
          : m.age_seconds < 86400
            ? `${Math.round(m.age_seconds / 3600)}h ago`
            : `${Math.round(m.age_seconds / 86400)}d ago`;
  const evalBadge =
    m.eval_mode === "walk_forward"
      ? { label: "WALK-FWD", classes: "text-long border-long/40 bg-long/5" }
      : m.eval_mode === "holdout_80_20"
        ? { label: "80/20 HOLDOUT", classes: "text-warn border-warn/40 bg-warn/5" }
        : { label: "UNKNOWN", classes: "text-muted-foreground border-border/60 bg-muted/5" };
  const aucDisplay = (() => {
    if (m.cv_summary?.mean_auc != null) {
      const mean = m.cv_summary.mean_auc;
      const std = m.cv_summary.std_auc ?? 0;
      return {
        primary: mean.toFixed(3),
        secondary: `± ${std.toFixed(3)} across ${m.cv_summary.n_scored ?? 0} folds`,
      };
    }
    if (m.holdout_auc != null) {
      return {
        primary: m.holdout_auc.toFixed(3),
        secondary: `single 80/20 holdout (${m.holdout_rows ?? "?"} val rows)`,
      };
    }
    return { primary: "—", secondary: "no AUC recorded" };
  })();
  return (
    <div className="flex flex-col gap-2 rounded-md border border-border/40 bg-background/30 p-4">
      <div className="flex items-center justify-between">
        <span className="font-mono text-sm font-semibold">{m.name}</span>
        <span
          className={cn(
            "rounded border px-2 py-0.5 text-[10px] font-mono uppercase tracking-widest",
            evalBadge.classes,
          )}
        >
          {evalBadge.label}
        </span>
      </div>
      <div>
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-2xl font-bold">
            {aucDisplay.primary}
          </span>
          <span className="text-[11px] uppercase tracking-widest text-muted-foreground">
            AUC
          </span>
        </div>
        <div className="text-xs text-muted-foreground">{aucDisplay.secondary}</div>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] font-mono uppercase tracking-widest text-muted-foreground">
        <span>{m.feature_count} features</span>
        {m.horizon_bars != null ? <span>{m.horizon_bars}-bar horizon</span> : null}
        <span>trained {ageLabel}</span>
      </div>
      {m.warnings.length > 0 ? (
        <div className="rounded border border-warn/40 bg-warn/5 px-2 py-1 text-[11px] text-warn">
          {m.warnings.join("; ")}
        </div>
      ) : null}
      {!m.model_file_exists ? (
        <div className="text-[11px] text-destructive">
          model.txt missing – inference disabled
        </div>
      ) : null}
    </div>
  );
}

function ServiceTile({ svc }: { svc: ServiceStatus }) {
  const statusColor =
    svc.status === "up"
      ? "text-long border-long/40 bg-long/5"
      : svc.status === "stale"
        ? "text-warn border-warn/40 bg-warn/5"
        : "text-destructive border-destructive/40 bg-destructive/5";
  const dotColor =
    svc.status === "up"
      ? "bg-long animate-pulse-slow"
      : svc.status === "stale"
        ? "bg-warn"
        : "bg-destructive";
  const ageLabel =
    svc.age_sec === null
      ? "no beat"
      : svc.age_sec < 1
        ? "<1s ago"
        : svc.age_sec < 60
          ? `${Math.round(svc.age_sec)}s ago`
          : `${Math.round(svc.age_sec / 60)}m ago`;
  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-md border px-3 py-2 transition-colors",
        statusColor,
      )}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono text-xs font-semibold lowercase tracking-tight">
          {svc.name}
        </span>
        <CircleDot className={cn("h-2.5 w-2.5 rounded-full", dotColor)} />
      </div>
      <div className="flex items-center justify-between text-[10px] uppercase tracking-widest">
        <span>{svc.status}</span>
        <span className="font-mono text-muted-foreground">{ageLabel}</span>
      </div>
      {!svc.expected ? (
        <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
          rogue (not in expected list)
        </span>
      ) : null}
    </div>
  );
}
