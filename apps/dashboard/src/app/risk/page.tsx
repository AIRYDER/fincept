"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNowStrict } from "date-fns";
import { motion } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  Bell,
  Brain,
  CheckCircle2,
  CircleDot,
  Crosshair,
  Database,
  Flame,
  Gamepad2,
  Gauge,
  Lock,
  Power,
  Radar,
  ShieldCheck,
  Siren,
  Target,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
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
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  AlertEvent,
  AlertSeverity,
  ModelRecord,
  ModelsResponse,
  Position,
  RegimeResponse,
  ServiceStatus,
  ServicesResponse,
  WsFrame,
} from "@/lib/types";
import { cn, formatUsd, nsToDate } from "@/lib/utils";
import { useFinceptStream } from "@/lib/ws";

const PER_SYMBOL_CAP = 20_000; // mirrors the default in fincept_core.config
const GROSS_CAP = 250_000;

type KillState = "clear" | "engaged";
type RiskLevel = "clear" | "guarded" | "stressed" | "lockdown";
type AlertFilter = AlertSeverity | "all";
type ScenarioId = "liquidity" | "volatility" | "model_drift" | "data_blackout";

interface ExposureRow {
  symbol: string;
  strategyCount: number;
  quantity: number;
  notional: number;
  unrealizedPnl: number;
  capPct: number;
  strategies: string[];
}

interface StrategyExposure {
  strategyId: string;
  notional: number;
  symbols: string[];
}

interface ServiceSummary {
  up: number;
  stale: number;
  down: number;
  expected: number;
}

interface AlertSummary {
  critical: number;
  warning: number;
  info: number;
}

interface ModelSummary {
  count: number;
  missing: number;
  warnings: number;
  walkForward: number;
  medianAgeSeconds: number | null;
}

interface RiskPosture {
  score: number;
  level: RiskLevel;
  label: string;
  subtitle: string;
  reasons: string[];
}

interface ThreatLayer {
  label: string;
  value: string;
  detail: string;
  level: RiskLevel;
  icon: LucideIcon;
}

interface ScenarioDrill {
  id: ScenarioId;
  label: string;
  tagline: string;
  icon: LucideIcon;
  scoreHit: number;
  telemetry: Array<{ label: string; value: string }>;
  checks: string[];
}

const SCENARIO_DRILLS: ScenarioDrill[] = [
  {
    id: "liquidity",
    label: "Liquidity Lockbox",
    tagline: "What if venues thin out and marks gap while exposure stays open?",
    icon: Lock,
    scoreHit: 18,
    telemetry: [
      { label: "shock", value: "spread +90 bps" },
      { label: "focus", value: "gross + symbol caps" },
      { label: "mode", value: "paper drill" },
    ],
    checks: [
      "Confirm no symbol is above 85% of its cap.",
      "Confirm stale services are not market-data or OMS critical.",
      "Review open exposure concentration before any new strategy launch.",
    ],
  },
  {
    id: "volatility",
    label: "Volatility Arcade",
    tagline: "A fast macro jump: high-vol regime, wider tails, noisy signals.",
    icon: Flame,
    scoreHit: 22,
    telemetry: [
      { label: "shock", value: "vol x1.8" },
      { label: "focus", value: "regime + alerts" },
      { label: "mode", value: "tail-risk drill" },
    ],
    checks: [
      "Scan critical alerts for model or market-data anomalies.",
      "Check high-vol or risk-off regime confidence before trusting signals.",
      "Prioritize reducing concentration pressure over adding complexity.",
    ],
  },
  {
    id: "model_drift",
    label: "Model Drift Maze",
    tagline: "Prediction quality decays: old models, warnings, noisy folds.",
    icon: Brain,
    scoreHit: 14,
    telemetry: [
      { label: "shock", value: "AUC confidence down" },
      { label: "focus", value: "models + provenance" },
      { label: "mode", value: "validation drill" },
    ],
    checks: [
      "Prefer walk-forward evaluated models over legacy holdout-only models.",
      "Inspect model warnings before relying on model-driven automation.",
      "Treat missing model files as inference-disabled until repaired.",
    ],
  },
  {
    id: "data_blackout",
    label: "Data Blackout",
    tagline: "A source or worker disappears and the cockpit has to fail closed.",
    icon: Radar,
    scoreHit: 20,
    telemetry: [
      { label: "shock", value: "service heartbeat lost" },
      { label: "focus", value: "services + freshness" },
      { label: "mode", value: "resilience drill" },
    ],
    checks: [
      "Identify expected services that are stale or down.",
      "Confirm the alert stream is still receiving heartbeat or control events.",
      "Keep the risk gate as the source of truth until source health recovers.",
    ],
  },
];

function asNum(value: string | null | undefined) {
  if (value == null) return 0;
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

export default function RiskPage() {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();

  const [killOpen, setKillOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [killState, setKillState] = useState<KillState>("clear");
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [selectedScenario, setSelectedScenario] = useState<ScenarioId>("volatility");
  const [alertFilter, setAlertFilter] = useState<AlertFilter>("all");

  const { data: positions } = useQuery({
    queryKey: ["positions", "risk"],
    queryFn: () => api.positions(token, true),
    enabled: !!token,
    refetchInterval: 15_000,
  });

  const { data: servicesData } = useQuery({
    queryKey: ["services"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });

  const { data: regimeData } = useQuery({
    queryKey: ["regime", "latest"],
    queryFn: () => api.regime(token, 5),
    enabled: !!token,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: () => api.models(token),
    enabled: !!token,
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
    const alert = frame.event.payload;
    setAlerts((prev) => [alert, ...prev].slice(0, 50));
    if (alert.code === "kill_switch_engaged") setKillState("engaged");
    if (alert.code === "kill_switch_cleared") setKillState("clear");
  }, []);
  useFinceptStream({ topics: ["alerts"], onFrame });

  const exposureRows = buildExposureRows(positions ?? []);
  const strategyRows = buildStrategyExposureRows(positions ?? []);
  const grossExposure = exposureRows.reduce((sum, row) => sum + row.notional, 0);
  const maxSymbolCapPct = exposureRows.reduce(
    (max, row) => Math.max(max, row.capPct),
    0,
  );
  const grossCapPct = GROSS_CAP > 0 ? (grossExposure / GROSS_CAP) * 100 : 0;
  const serviceSummary = summarizeServices(servicesData);
  const alertSummary = summarizeAlerts(alerts);
  const modelSummary = summarizeModels(modelsData);
  const posture = buildRiskPosture({
    killState,
    maxSymbolCapPct,
    grossCapPct,
    serviceSummary,
    alertSummary,
    modelSummary,
    regimeData,
  });
  const threatLayers = buildThreatLayers({
    exposureRows,
    grossExposure,
    serviceSummary,
    alertSummary,
    modelSummary,
    regimeData,
  });
  const selectedDrill =
    SCENARIO_DRILLS.find((scenario) => scenario.id === selectedScenario) ??
    SCENARIO_DRILLS[0];
  const filteredAlerts =
    alertFilter === "all"
      ? alerts
      : alerts.filter((alert) => alert.severity === alertFilter);

  return (
    <AppShell>
      <PageHeader
        title="Risk"
        description="A mission-control surface for exposure pressure, service reliability, macro regime, model provenance, alert triage, and paper-only crisis drills."
        action={
          <div className="flex items-center gap-2">
            <Badge variant={postureBadgeVariant(posture.level)} className="px-3 py-1">
              {posture.label}
            </Badge>
            <Badge variant={killState === "engaged" ? "destructive" : "long"}>
              {killState === "engaged" ? "KILL SWITCH ENGAGED" : "GATE ACTIVE"}
            </Badge>
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.05fr_0.95fr_0.95fr]">
        <RiskPostureCard posture={posture} threatLayers={threatLayers} />
        <KillSwitchCard
          killState={killState}
          clearPending={clearMut.isPending}
          onOpenTrip={() => setKillOpen(true)}
          onClear={() => clearMut.mutate()}
        />
        <ScenarioDrillDeck
          posture={posture}
          selected={selectedDrill}
          onSelect={setSelectedScenario}
          grossCapPct={grossCapPct}
          maxSymbolCapPct={maxSymbolCapPct}
        />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        <ExposureHeatMap rows={exposureRows} grossExposure={grossExposure} />
        <BlastRadiusPanel
          exposureRows={exposureRows}
          strategyRows={strategyRows}
          grossExposure={grossExposure}
        />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 2xl:grid-cols-[1.2fr_0.8fr]">
        <ServiceConstellation data={servicesData} summary={serviceSummary} />
        <RegimePanel data={regimeData} />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 2xl:grid-cols-[1fr_1fr]">
        <ModelsPanel data={modelsData} summary={modelSummary} />
        <AlertFeedPanel
          alerts={filteredAlerts}
          summary={alertSummary}
          activeFilter={alertFilter}
          onFilter={setAlertFilter}
        />
      </div>

      <Dialog open={killOpen} onOpenChange={setKillOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-destructive" />
              Trip the kill switch?
            </DialogTitle>
            <DialogDescription>
              All running services will halt new orders and attempt to cancel
              open ones. Provide a short reason for the audit log.
            </DialogDescription>
          </DialogHeader>
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
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
              {tripMut.isPending ? "Engaging..." : "Trip kill switch"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </AppShell>
  );
}

function RiskPostureCard({
  posture,
  threatLayers,
}: {
  posture: RiskPosture;
  threatLayers: ThreatLayer[];
}) {
  const gaugeDegrees = Math.round(posture.score * 3.6);
  return (
    <Card className="relative overflow-hidden border-cyan/30">
      <CardHeader className="pb-3">
        <CardTitle>
          <Gauge className="h-3.5 w-3.5 text-cyan" />
          Risk Posture Engine
        </CardTitle>
        <CardDescription>
          One score made from exposure, services, alerts, regime, and model
          provenance. Higher is safer.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 lg:grid-cols-[14rem_1fr] xl:grid-cols-1 2xl:grid-cols-[14rem_1fr]">
        <div className="flex items-center justify-center">
          <div
            className="relative flex h-44 w-44 items-center justify-center rounded-full border border-border"
            style={{
              background: `conic-gradient(${postureColor(posture.level)} ${gaugeDegrees}deg, hsl(var(--muted) / 0.28) ${gaugeDegrees}deg)`,
            }}
          >
            <div className="flex h-32 w-32 flex-col items-center justify-center rounded-full border border-border bg-background text-center">
              <span className="font-mono text-4xl font-bold">{posture.score}</span>
              <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                risk score
              </span>
              <Badge variant={postureBadgeVariant(posture.level)} className="mt-2">
                {posture.label}
              </Badge>
            </div>
          </div>
        </div>

        <div className="space-y-3">
          <div className="border border-border p-3">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Operator read
            </div>
            <p className="mt-1 text-sm leading-6 text-foreground">
              {posture.subtitle}
            </p>
          </div>
          <div className="grid gap-2">
            {threatLayers.map((layer) => (
              <ThreatLayerRow key={layer.label} layer={layer} />
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function ThreatLayerRow({ layer }: { layer: ThreatLayer }) {
  const Icon = layer.icon;
  return (
    <div className="grid grid-cols-[auto_1fr_auto] items-center gap-3 border border-border/70 px-3 py-2">
      <Icon className={cn("h-3.5 w-3.5", textForLevel(layer.level))} />
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium uppercase tracking-wider">
            {layer.label}
          </span>
          <span className="truncate text-[11px] text-muted-foreground">
            {layer.detail}
          </span>
        </div>
        <div className="mt-1 h-1 overflow-hidden bg-muted/50">
          <div
            className={cn("h-full", bgForLevel(layer.level))}
            style={{ width: `${threatLevelPct(layer.level)}%` }}
          />
        </div>
      </div>
      <span className={cn("font-mono text-xs", textForLevel(layer.level))}>
        {layer.value}
      </span>
    </div>
  );
}

function KillSwitchCard({
  killState,
  clearPending,
  onOpenTrip,
  onClear,
}: {
  killState: KillState;
  clearPending: boolean;
  onOpenTrip: () => void;
  onClear: () => void;
}) {
  return (
    <Card
      className={cn(
        "relative overflow-hidden border-2",
        killState === "engaged" ? "border-destructive/60" : "border-long/30",
      )}
    >
      <CardHeader>
        <CardTitle>
          <Power className="h-3.5 w-3.5 text-destructive" />
          Big Red Button
        </CardTitle>
        <CardDescription>
          A visible circuit breaker for the paper trading loop. This panel is
          intentionally serious: it halts new order flow until cleared.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {killState === "engaged" ? (
          <motion.div
            initial={{ scale: 0.98 }}
            animate={{ scale: [1, 1.025, 1] }}
            transition={{ duration: 1.25, repeat: Infinity }}
            className="flex min-h-36 flex-col items-center justify-center gap-3 border border-destructive/40 bg-destructive/10 p-4"
          >
            <Siren className="h-10 w-10 text-destructive" />
            <span className="text-sm font-medium uppercase tracking-wider text-destructive">
              Trading halted
            </span>
          </motion.div>
        ) : (
          <div className="flex min-h-36 flex-col items-center justify-center gap-3 border border-long/30 bg-long/5 p-4">
            <ShieldCheck className="h-10 w-10 text-long" />
            <span className="text-center text-sm font-medium uppercase tracking-wider text-long">
              Risk gate active
            </span>
            <span className="text-center text-xs leading-5 text-muted-foreground">
              Caps, service state, and alerts are visible before escalation.
            </span>
          </div>
        )}

        <Button
          variant="destructive"
          size="xl"
          className="w-full"
          onClick={onOpenTrip}
          disabled={killState === "engaged"}
        >
          <Power className="mr-2 h-5 w-5" />
          Trip kill switch
        </Button>
        <Button
          variant="outline"
          size="lg"
          className="w-full"
          onClick={onClear}
          disabled={killState === "clear" || clearPending}
        >
          {clearPending ? "Clearing..." : "Clear kill switch"}
        </Button>
      </CardContent>
    </Card>
  );
}

function ScenarioDrillDeck({
  posture,
  selected,
  onSelect,
  grossCapPct,
  maxSymbolCapPct,
}: {
  posture: RiskPosture;
  selected: ScenarioDrill;
  onSelect: (id: ScenarioId) => void;
  grossCapPct: number;
  maxSymbolCapPct: number;
}) {
  const projectedScore = Math.max(
    0,
    Math.round(
      posture.score -
        selected.scoreHit -
        (grossCapPct > 70 ? 5 : 0) -
        (maxSymbolCapPct > 70 ? 5 : 0),
    ),
  );
  const SelectedIcon = selected.icon;

  return (
    <Card className="border-primary/35">
      <CardHeader>
        <CardTitle>
          <Gamepad2 className="h-3.5 w-3.5 text-primary" />
          Scenario Drill Deck
        </CardTitle>
        <CardDescription>
          Entertaining, but still useful: pick a paper-only crisis card and
          read the containment checklist.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-1">
          {SCENARIO_DRILLS.map((scenario) => (
            <button
              key={scenario.id}
              type="button"
              aria-pressed={selected.id === scenario.id}
              onClick={() => onSelect(scenario.id)}
              className={cn(
                "border px-2 py-2 text-left text-[10px] uppercase tracking-wider transition-colors",
                selected.id === scenario.id
                  ? "border-primary/70 bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:border-primary/40 hover:text-foreground",
              )}
            >
              {scenario.label}
            </button>
          ))}
        </div>

        <div className="border border-border p-3">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-medium">
                <SelectedIcon className="h-4 w-4 text-primary" />
                {selected.label}
              </div>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {selected.tagline}
              </p>
            </div>
            <div className="text-right">
              <div className="font-mono text-xl text-primary">
                {projectedScore}
              </div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                drill score
              </div>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-3 gap-2">
            {selected.telemetry.map((item) => (
              <div key={item.label} className="border border-border/70 p-2">
                <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                  {item.label}
                </div>
                <div className="mt-1 truncate font-mono text-[11px] text-foreground">
                  {item.value}
                </div>
              </div>
            ))}
          </div>

          <ul className="mt-3 space-y-1 text-xs leading-5 text-muted-foreground">
            {selected.checks.map((check) => (
              <li key={check} className="flex gap-2">
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-cyan" />
                <span>{check}</span>
              </li>
            ))}
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}

function ExposureHeatMap({
  rows,
  grossExposure,
}: {
  rows: ExposureRow[];
  grossExposure: number;
}) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>
            <Crosshair className="h-3.5 w-3.5 text-cyan" />
            Exposure Heat Map
          </CardTitle>
          <CardDescription>
            Notional pressure by symbol, sized against the per-symbol cap.
          </CardDescription>
        </div>
        <Badge variant="muted">
          Gross {formatUsd(grossExposure, { compact: true })}
        </Badge>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <EmptyState
            icon={ShieldCheck}
            title="No open exposure"
            description="No active positions; the gate has nothing to constrain."
          />
        ) : (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-4">
            {rows.map((row) => (
              <ExposureTile key={row.symbol} row={row} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ExposureTile({ row }: { row: ExposureRow }) {
  const level = levelForPct(row.capPct);
  return (
    <article
      className={cn(
        "relative min-h-32 overflow-hidden border p-3",
        borderForLevel(level),
      )}
    >
      <div
        className={cn("absolute inset-x-0 bottom-0 opacity-15", bgForLevel(level))}
        style={{ height: `${Math.min(100, row.capPct)}%` }}
      />
      <div className="relative z-10 flex h-full flex-col justify-between gap-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <h3 className="font-mono text-lg text-foreground">{row.symbol}</h3>
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
              {row.strategyCount} strategies
            </p>
          </div>
          <Badge variant={badgeForLevel(level)}>{Math.round(row.capPct)}%</Badge>
        </div>
        <div>
          <div className="font-mono text-sm text-foreground">
            {formatUsd(row.notional, { compact: true })}
          </div>
          <div className="font-mono text-[10px] text-muted-foreground">
            qty {row.quantity.toLocaleString("en-US", { maximumFractionDigits: 4 })}
          </div>
          <div
            className={cn(
              "font-mono text-[11px]",
              row.unrealizedPnl >= 0 ? "text-long" : "text-destructive",
            )}
          >
            uPnL {formatUsd(row.unrealizedPnl, { compact: true, signed: true })}
          </div>
        </div>
      </div>
    </article>
  );
}

function BlastRadiusPanel({
  exposureRows,
  strategyRows,
  grossExposure,
}: {
  exposureRows: ExposureRow[];
  strategyRows: StrategyExposure[];
  grossExposure: number;
}) {
  const topSymbol = exposureRows[0];
  const topStrategy = strategyRows[0];
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Target className="h-3.5 w-3.5 text-primary" />
          Blast Radius
        </CardTitle>
        <CardDescription>
          Fast answer to: if something goes wrong, where is the damage
          concentrated?
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-2">
          <MiniMetric
            label="top symbol"
            value={topSymbol?.symbol ?? "—"}
            sub={topSymbol ? formatUsd(topSymbol.notional, { compact: true }) : "no exposure"}
          />
          <MiniMetric
            label="top strategy"
            value={topStrategy?.strategyId ?? "—"}
            sub={topStrategy ? formatUsd(topStrategy.notional, { compact: true }) : "no exposure"}
          />
          <MiniMetric
            label="gross cap"
            value={`${Math.round((grossExposure / GROSS_CAP) * 100)}%`}
            sub={`${formatUsd(grossExposure, { compact: true })} / ${formatUsd(GROSS_CAP, { compact: true })}`}
          />
          <MiniMetric
            label="symbols"
            value={String(exposureRows.length)}
            sub="open pressure points"
          />
        </div>

        <section className="border border-border">
          <div className="border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-cyan">
            Symbol ladder
          </div>
          {exposureRows.length === 0 ? (
            <p className="p-3 text-xs text-muted-foreground">No ladder yet.</p>
          ) : (
            <div className="divide-y divide-border/50">
              {exposureRows.slice(0, 6).map((row) => (
                <LadderRow
                  key={row.symbol}
                  label={row.symbol}
                  value={formatUsd(row.notional, { compact: true })}
                  pct={row.capPct}
                />
              ))}
            </div>
          )}
        </section>

        <section className="border border-border">
          <div className="border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-cyan">
            Strategy ladder
          </div>
          {strategyRows.length === 0 ? (
            <p className="p-3 text-xs text-muted-foreground">No strategy exposure yet.</p>
          ) : (
            <div className="divide-y divide-border/50">
              {strategyRows.slice(0, 6).map((row) => (
                <LadderRow
                  key={row.strategyId}
                  label={row.strategyId}
                  value={formatUsd(row.notional, { compact: true })}
                  pct={grossExposure > 0 ? (row.notional / grossExposure) * 100 : 0}
                />
              ))}
            </div>
          )}
        </section>
      </CardContent>
    </Card>
  );
}

function ServiceConstellation({
  data,
  summary,
}: {
  data: ServicesResponse | undefined;
  summary: ServiceSummary;
}) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>
            <Activity className="h-3.5 w-3.5 text-primary" />
            Service Constellation
          </CardTitle>
          <CardDescription>
            Heartbeats from background services. UP is fresh, STALE is late,
            DOWN is absent.
          </CardDescription>
        </div>
        <Badge variant={summary.down > 0 ? "destructive" : summary.stale > 0 ? "warn" : "long"}>
          {summary.up}/{summary.expected} up
        </Badge>
      </CardHeader>
      <CardContent>
        {data ? (
          <>
            <div className="mb-3 grid grid-cols-3 gap-2">
              <MiniMetric label="up" value={String(summary.up)} sub="fresh beats" />
              <MiniMetric label="stale" value={String(summary.stale)} sub="late beats" />
              <MiniMetric label="down" value={String(summary.down)} sub="missing beats" />
            </div>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-4">
              {data.services.map((service) => (
                <ServiceTile key={service.name} svc={service} />
              ))}
            </div>
          </>
        ) : (
          <EmptyState
            icon={Activity}
            title="Loading service status..."
            description="Polling /services every 15s."
          />
        )}
      </CardContent>
    </Card>
  );
}

function RegimePanel({ data }: { data: RegimeResponse | undefined }) {
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>
            <Brain className="h-3.5 w-3.5 text-primary" />
            Macro Weather
          </CardTitle>
        </CardHeader>
        <CardContent>
          <EmptyState
            icon={Brain}
            title="Loading regime..."
            description="Polling /regime every 60s."
          />
        </CardContent>
      </Card>
    );
  }
  if (data.status !== "ok" || !data.snapshot) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>
            <Brain className="h-3.5 w-3.5 text-primary" />
            Macro Weather
          </CardTitle>
        </CardHeader>
        <CardContent>
          <EmptyState
            icon={Brain}
            title="Regime agent inactive"
            description="Set FRED_API_KEY and start the regime_agent service to populate this panel."
          />
        </CardContent>
      </Card>
    );
  }

  const snap = data.snapshot;
  const style = REGIME_STYLES[snap.regime] ?? REGIME_STYLES.neutral;
  const ageLabel = secondsAgo(snap.age_seconds);
  return (
    <Card className={cn("border", style.border)}>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>
            <Brain className={cn("h-3.5 w-3.5", style.text)} />
            Macro Weather
          </CardTitle>
          <CardDescription>
            Latest regime classification from regime_agent and FRED inputs.
          </CardDescription>
        </div>
        <Badge variant={style.badge}>{style.label}</Badge>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className={cn("border p-4", style.surface)}>
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Current climate
            </span>
            <CircleDot className={cn("h-3 w-3 rounded-full", style.dot)} />
          </div>
          <div className="mt-2 flex items-baseline gap-3">
            <span className="font-mono text-3xl font-bold">{style.label}</span>
            <span className="font-mono text-sm text-muted-foreground">
              conf {(snap.confidence * 100).toFixed(0)}%
            </span>
          </div>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            {snap.rationale}
          </p>
        </div>

        <div className="grid grid-cols-3 gap-2">
          <RegimeMetric label="VIX" value={snap.vix?.toFixed(1) ?? "—"} />
          <RegimeMetric label="10Y-2Y" value={snap.yield_spread?.toFixed(2) ?? "—"} />
          <RegimeMetric
            label="Fed funds"
            value={snap.fed_funds != null ? `${snap.fed_funds.toFixed(2)}%` : "—"}
          />
        </div>

        <div className="grid grid-cols-2 gap-2">
          <MiniMetric
            label="direction bias"
            value={`${snap.direction_bias > 0 ? "+" : ""}${(snap.direction_bias * 100).toFixed(0)}%`}
            sub="risk-asset tilt"
          />
          <MiniMetric label="updated" value={ageLabel} sub={snap.agent_id} />
        </div>

        <section className="border border-border">
          <div className="border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-cyan">
            Recent regime tape
          </div>
          {data.history.length === 0 ? (
            <p className="p-3 text-xs text-muted-foreground">No entries yet.</p>
          ) : (
            <ul className="divide-y divide-border/50 text-xs">
              {data.history.slice(0, 5).map((entry) => {
                const regime = entry.regime ?? "neutral";
                const entryStyle = REGIME_STYLES[regime] ?? REGIME_STYLES.neutral;
                return (
                  <li
                    key={entry.stream_id}
                    className="flex items-center justify-between px-3 py-2"
                  >
                    <span className={cn("font-mono", entryStyle.text)}>
                      {entryStyle.label}
                    </span>
                    <span className="font-mono text-muted-foreground">
                      conf {((entry.confidence ?? 0) * 100).toFixed(0)}%
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </CardContent>
    </Card>
  );
}

function ModelsPanel({
  data,
  summary,
}: {
  data: ModelsResponse | undefined;
  summary: ModelSummary;
}) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>
            <Database className="h-3.5 w-3.5 text-primary" />
            Model Provenance Arcade
          </CardTitle>
          <CardDescription>
            Evaluation mode, freshness, warnings, and inference readiness.
          </CardDescription>
        </div>
        <Badge variant={summary.missing > 0 || summary.warnings > 0 ? "warn" : "muted"}>
          {summary.count} models
        </Badge>
      </CardHeader>
      <CardContent>
        {data?.models?.length ? (
          <>
            <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
              <MiniMetric label="walk-forward" value={String(summary.walkForward)} sub="validated" />
              <MiniMetric label="warnings" value={String(summary.warnings)} sub="model notes" />
              <MiniMetric label="missing files" value={String(summary.missing)} sub="disabled" />
              <MiniMetric
                label="median age"
                value={summary.medianAgeSeconds == null ? "—" : secondsAgo(summary.medianAgeSeconds)}
                sub="trained"
              />
            </div>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {data.models.map((model) => (
                <ModelCard key={model.name} model={model} />
              ))}
            </div>
          </>
        ) : (
          <EmptyState
            icon={Database}
            title="No models found"
            description={`Train one with: python -m agents.gbm_predictor.train --input data/X.parquet --cv-folds 5 --out-dir ${data?.summary?.models_dir ?? "models/gbm_predictor"}`}
          />
        )}
      </CardContent>
    </Card>
  );
}

function AlertFeedPanel({
  alerts,
  summary,
  activeFilter,
  onFilter,
}: {
  alerts: AlertEvent[];
  summary: AlertSummary;
  activeFilter: AlertFilter;
  onFilter: (filter: AlertFilter) => void;
}) {
  const filters: Array<{ label: string; value: AlertFilter; count: number }> = [
    {
      label: "All",
      value: "all",
      count: summary.critical + summary.warning + summary.info,
    },
    { label: "Critical", value: "critical", count: summary.critical },
    { label: "Warning", value: "warning", count: summary.warning },
    { label: "Info", value: "info", count: summary.info },
  ];
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>
            <Bell className="h-3.5 w-3.5 text-cyan" />
            Alert Pinball
          </CardTitle>
          <CardDescription>
            Realtime stream from events.alerts, with severity triage.
          </CardDescription>
        </div>
        <Badge variant={summary.critical > 0 ? "destructive" : summary.warning > 0 ? "warn" : "muted"}>
          {summary.critical} critical
        </Badge>
      </CardHeader>
      <CardContent className="space-y-3 px-0">
        <div className="flex flex-wrap gap-1 px-4">
          {filters.map((filter) => (
            <button
              key={filter.value}
              type="button"
              aria-pressed={activeFilter === filter.value}
              onClick={() => onFilter(filter.value)}
              className={cn(
                "border px-2 py-1 text-[10px] uppercase tracking-wider transition-colors",
                activeFilter === filter.value
                  ? "border-cyan/70 bg-cyan/10 text-cyan"
                  : "border-border text-muted-foreground hover:border-cyan/40 hover:text-foreground",
              )}
            >
              {filter.label} {filter.count}
            </button>
          ))}
        </div>

        {alerts.length === 0 ? (
          <div className="px-4 pb-4">
            <EmptyState
              icon={Bell}
              title="No alerts in this lane"
              description="When services emit to events.alerts, matching alerts appear here in realtime."
            />
          </div>
        ) : (
          <ScrollArea className="h-[25rem]">
            <ul className="divide-y divide-border/40">
              {alerts.map((alert) => (
                <li
                  key={alert.alert_id}
                  className="grid grid-cols-[auto_1fr_auto] gap-3 px-4 py-3 text-sm"
                >
                  <Badge
                    variant={
                      alert.severity === "critical"
                        ? "destructive"
                        : alert.severity === "warning"
                          ? "warn"
                          : "muted"
                    }
                    className="w-20 justify-center"
                  >
                    {alert.severity.toUpperCase()}
                  </Badge>
                  <div className="min-w-0">
                    <div className="truncate font-mono text-xs text-muted-foreground">
                      {alert.code} · {alert.source}
                    </div>
                    <div className="text-sm leading-5">{alert.message}</div>
                    {alert.tags ? (
                      <div className="mt-1 truncate font-mono text-[10px] text-muted-foreground">
                        {Object.entries(alert.tags)
                          .map(([key, value]) => `${key}:${value}`)
                          .join(" · ")}
                      </div>
                    ) : null}
                  </div>
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {ago(alert.ts_event)}
                  </span>
                </li>
              ))}
            </ul>
          </ScrollArea>
        )}
      </CardContent>
    </Card>
  );
}

function MiniMetric({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub: string;
}) {
  return (
    <div className="border border-border/70 p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 truncate font-mono text-lg text-foreground">{value}</div>
      <div className="mt-1 truncate text-[11px] text-muted-foreground">{sub}</div>
    </div>
  );
}

function LadderRow({
  label,
  value,
  pct,
}: {
  label: string;
  value: string;
  pct: number;
}) {
  const level = levelForPct(pct);
  return (
    <div className="px-3 py-2">
      <div className="flex items-center justify-between gap-3 text-xs">
        <span className="truncate font-mono text-foreground">{label}</span>
        <span className={cn("font-mono", textForLevel(level))}>{value}</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden bg-muted/50">
        <div
          className={cn("h-full", bgForLevel(level))}
          style={{ width: `${Math.min(100, pct)}%` }}
        />
      </div>
    </div>
  );
}

function RegimeMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-border/70 p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 font-mono text-lg text-foreground">{value}</div>
    </div>
  );
}

function ModelCard({ model }: { model: ModelRecord }) {
  const evalBadge =
    model.eval_mode === "walk_forward"
      ? { label: "WALK-FWD", className: "border-long/40 bg-long/5 text-long" }
      : model.eval_mode === "holdout_80_20"
        ? { label: "80/20", className: "border-warn/40 bg-warn/5 text-warn" }
        : {
            label: "UNKNOWN",
            className: "border-border/60 bg-muted/5 text-muted-foreground",
          };
  const aucDisplay = modelAucDisplay(model);
  return (
    <article className="flex flex-col gap-2 border border-border/70 bg-background/30 p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="truncate font-mono text-sm text-foreground">
          {model.name}
        </span>
        <span
          className={cn(
            "border px-2 py-0.5 text-[10px] uppercase tracking-wider",
            evalBadge.className,
          )}
        >
          {evalBadge.label}
        </span>
      </div>
      <div>
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-2xl text-foreground">
            {aucDisplay.primary}
          </span>
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
            AUC
          </span>
        </div>
        <p className="text-xs leading-5 text-muted-foreground">
          {aucDisplay.secondary}
        </p>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span>{model.feature_count} features</span>
        {model.horizon_bars != null ? <span>{model.horizon_bars}-bar horizon</span> : null}
        <span>trained {secondsAgo(model.age_seconds)}</span>
      </div>
      {model.warnings.length > 0 ? (
        <div className="border border-warn/40 bg-warn/5 px-2 py-1 text-[11px] leading-5 text-warn">
          {model.warnings.join("; ")}
        </div>
      ) : null}
      {!model.model_file_exists ? (
        <div className="border border-destructive/40 bg-destructive/5 px-2 py-1 text-[11px] text-destructive">
          model.txt missing, inference disabled
        </div>
      ) : null}
    </article>
  );
}

function ServiceTile({ svc }: { svc: ServiceStatus }) {
  const level =
    svc.status === "up" ? "clear" : svc.status === "stale" ? "guarded" : "lockdown";
  return (
    <div className={cn("flex flex-col gap-1 border px-3 py-2", borderForLevel(level))}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-xs lowercase tracking-tight">
          {svc.name}
        </span>
        <CircleDot className={cn("h-2.5 w-2.5 rounded-full", dotForLevel(level))} />
      </div>
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wider">
        <span className={textForLevel(level)}>{svc.status}</span>
        <span className="font-mono text-muted-foreground">
          {svc.age_sec === null ? "no beat" : secondsAgo(svc.age_sec)}
        </span>
      </div>
      {!svc.expected ? (
        <span className="text-[9px] uppercase tracking-wider text-muted-foreground">
          rogue service
        </span>
      ) : null}
    </div>
  );
}

function buildExposureRows(positions: Position[]): ExposureRow[] {
  const rows = new Map<string, ExposureRow>();
  for (const position of positions) {
    const quantity = asNum(position.quantity);
    const mark = asNum(position.mark_px) || asNum(position.avg_cost);
    const notional = Math.abs(quantity * mark + asNum(position.unrealized_pnl));
    const existing =
      rows.get(position.symbol) ??
      {
        symbol: position.symbol,
        strategyCount: 0,
        quantity: 0,
        notional: 0,
        unrealizedPnl: 0,
        capPct: 0,
        strategies: [],
      };
    existing.quantity += quantity;
    existing.notional += notional;
    existing.unrealizedPnl += asNum(position.unrealized_pnl);
    if (!existing.strategies.includes(position.strategy_id)) {
      existing.strategies.push(position.strategy_id);
      existing.strategyCount = existing.strategies.length;
    }
    existing.capPct = PER_SYMBOL_CAP > 0 ? (existing.notional / PER_SYMBOL_CAP) * 100 : 0;
    rows.set(position.symbol, existing);
  }
  return Array.from(rows.values())
    .filter((row) => row.notional > 0)
    .sort((a, b) => b.notional - a.notional);
}

function buildStrategyExposureRows(positions: Position[]): StrategyExposure[] {
  const rows = new Map<string, StrategyExposure>();
  for (const position of positions) {
    const quantity = asNum(position.quantity);
    const mark = asNum(position.mark_px) || asNum(position.avg_cost);
    const notional = Math.abs(quantity * mark + asNum(position.unrealized_pnl));
    const existing =
      rows.get(position.strategy_id) ??
      {
        strategyId: position.strategy_id,
        notional: 0,
        symbols: [],
      };
    existing.notional += notional;
    if (!existing.symbols.includes(position.symbol)) {
      existing.symbols.push(position.symbol);
    }
    rows.set(position.strategy_id, existing);
  }
  return Array.from(rows.values())
    .filter((row) => row.notional > 0)
    .sort((a, b) => b.notional - a.notional);
}

function summarizeServices(data: ServicesResponse | undefined): ServiceSummary {
  const services = data?.services ?? [];
  return {
    up: services.filter((service) => service.status === "up").length,
    stale: services.filter((service) => service.status === "stale").length,
    down: services.filter((service) => service.status === "down").length,
    expected: data?.summary.expected ?? services.filter((service) => service.expected).length,
  };
}

function summarizeAlerts(alerts: AlertEvent[]): AlertSummary {
  return {
    critical: alerts.filter((alert) => alert.severity === "critical").length,
    warning: alerts.filter((alert) => alert.severity === "warning").length,
    info: alerts.filter((alert) => alert.severity === "info").length,
  };
}

function summarizeModels(data: ModelsResponse | undefined): ModelSummary {
  const models = data?.models ?? [];
  const sortedAges = models
    .map((model) => model.age_seconds)
    .filter((age): age is number => age !== null)
    .sort((a, b) => a - b);
  return {
    count: models.length,
    missing: models.filter((model) => !model.model_file_exists).length,
    warnings: models.reduce((sum, model) => sum + model.warnings.length, 0),
    walkForward: models.filter((model) => model.eval_mode === "walk_forward").length,
    medianAgeSeconds: sortedAges.length
      ? sortedAges[Math.floor(sortedAges.length / 2)]
      : null,
  };
}

function buildRiskPosture({
  killState,
  maxSymbolCapPct,
  grossCapPct,
  serviceSummary,
  alertSummary,
  modelSummary,
  regimeData,
}: {
  killState: KillState;
  maxSymbolCapPct: number;
  grossCapPct: number;
  serviceSummary: ServiceSummary;
  alertSummary: AlertSummary;
  modelSummary: ModelSummary;
  regimeData: RegimeResponse | undefined;
}): RiskPosture {
  let score = 100;
  const reasons: string[] = [];

  if (killState === "engaged") {
    score -= 65;
    reasons.push("kill switch engaged");
  }
  if (maxSymbolCapPct >= 100) {
    score -= 35;
    reasons.push("symbol cap breached");
  } else if (maxSymbolCapPct >= 85) {
    score -= 18;
    reasons.push("symbol near cap");
  } else if (maxSymbolCapPct >= 60) {
    score -= 8;
    reasons.push("symbol pressure building");
  }
  if (grossCapPct >= 100) {
    score -= 35;
    reasons.push("gross cap breached");
  } else if (grossCapPct >= 85) {
    score -= 18;
    reasons.push("gross exposure near cap");
  } else if (grossCapPct >= 60) {
    score -= 8;
    reasons.push("gross exposure building");
  }
  if (serviceSummary.down > 0) {
    score -= serviceSummary.down * 12;
    reasons.push(`${serviceSummary.down} service down`);
  }
  if (serviceSummary.stale > 0) {
    score -= serviceSummary.stale * 6;
    reasons.push(`${serviceSummary.stale} service stale`);
  }
  if (alertSummary.critical > 0) {
    score -= alertSummary.critical * 12;
    reasons.push(`${alertSummary.critical} critical alerts`);
  }
  if (alertSummary.warning > 0) {
    score -= alertSummary.warning * 5;
    reasons.push(`${alertSummary.warning} warning alerts`);
  }
  const regime = regimeData?.snapshot?.regime;
  if (regime === "risk_off") {
    score -= 14;
    reasons.push("risk-off macro regime");
  } else if (regime === "high_vol") {
    score -= 10;
    reasons.push("high-vol macro regime");
  }
  if (modelSummary.missing > 0) {
    score -= modelSummary.missing * 8;
    reasons.push(`${modelSummary.missing} missing model files`);
  }
  if (modelSummary.warnings > 0) {
    score -= Math.min(12, modelSummary.warnings * 2);
    reasons.push(`${modelSummary.warnings} model warnings`);
  }

  const finalScore = Math.max(0, Math.min(100, Math.round(score)));
  const level =
    finalScore >= 85
      ? "clear"
      : finalScore >= 65
        ? "guarded"
        : finalScore >= 40
          ? "stressed"
          : "lockdown";
  return {
    score: finalScore,
    level,
    label:
      level === "clear"
        ? "CLEAR"
        : level === "guarded"
          ? "GUARDED"
          : level === "stressed"
            ? "STRESSED"
            : "LOCKDOWN",
    subtitle:
      reasons.length > 0
        ? `Watch: ${reasons.slice(0, 4).join(", ")}.`
        : "No active pressure points detected from the current cockpit feed.",
    reasons,
  };
}

function buildThreatLayers({
  exposureRows,
  grossExposure,
  serviceSummary,
  alertSummary,
  modelSummary,
  regimeData,
}: {
  exposureRows: ExposureRow[];
  grossExposure: number;
  serviceSummary: ServiceSummary;
  alertSummary: AlertSummary;
  modelSummary: ModelSummary;
  regimeData: RegimeResponse | undefined;
}): ThreatLayer[] {
  const maxSymbolPct = exposureRows.reduce((max, row) => Math.max(max, row.capPct), 0);
  const grossPct = GROSS_CAP > 0 ? (grossExposure / GROSS_CAP) * 100 : 0;
  const alertTotal = alertSummary.critical + alertSummary.warning + alertSummary.info;
  const regime = regimeData?.snapshot?.regime ?? "unavailable";
  return [
    {
      label: "Exposure",
      value: `${Math.round(Math.max(maxSymbolPct, grossPct))}%`,
      detail: "largest cap pressure",
      level: levelForPct(Math.max(maxSymbolPct, grossPct)),
      icon: Crosshair,
    },
    {
      label: "Services",
      value:
        serviceSummary.expected > 0
          ? `${serviceSummary.up}/${serviceSummary.expected}`
          : "—",
      detail: `${serviceSummary.stale} stale, ${serviceSummary.down} down`,
      level: serviceSummary.down > 0 ? "lockdown" : serviceSummary.stale > 0 ? "guarded" : "clear",
      icon: Activity,
    },
    {
      label: "Regime",
      value: regime.replace(/_/g, " "),
      detail: "macro climate",
      level: regime === "risk_off" ? "stressed" : regime === "high_vol" ? "guarded" : "clear",
      icon: Brain,
    },
    {
      label: "Models",
      value: String(modelSummary.count),
      detail: `${modelSummary.warnings} warnings, ${modelSummary.missing} missing`,
      level: modelSummary.missing > 0 ? "stressed" : modelSummary.warnings > 0 ? "guarded" : "clear",
      icon: Database,
    },
    {
      label: "Alerts",
      value: String(alertTotal),
      detail: `${alertSummary.critical} critical, ${alertSummary.warning} warning`,
      level: alertSummary.critical > 0 ? "lockdown" : alertSummary.warning > 0 ? "guarded" : "clear",
      icon: Bell,
    },
  ];
}

function modelAucDisplay(model: ModelRecord) {
  if (model.cv_summary?.mean_auc != null) {
    const mean = model.cv_summary.mean_auc;
    const std = model.cv_summary.std_auc ?? 0;
    return {
      primary: mean.toFixed(3),
      secondary: `+/- ${std.toFixed(3)} across ${model.cv_summary.n_scored ?? 0} folds`,
    };
  }
  if (model.holdout_auc != null) {
    return {
      primary: model.holdout_auc.toFixed(3),
      secondary: `single holdout with ${model.holdout_rows ?? "?"} validation rows`,
    };
  }
  return { primary: "—", secondary: "no AUC recorded" };
}

function ago(ns: number): string {
  const date = nsToDate(ns);
  if (!date) return "—";
  try {
    return formatDistanceToNowStrict(date, { addSuffix: false });
  } catch {
    return "—";
  }
}

function secondsAgo(seconds: number | null): string {
  if (seconds === null) return "unknown";
  if (seconds < 1) return "<1s ago";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function levelForPct(pct: number): RiskLevel {
  if (pct >= 100) return "lockdown";
  if (pct >= 85) return "stressed";
  if (pct >= 60) return "guarded";
  return "clear";
}

function postureBadgeVariant(level: RiskLevel) {
  if (level === "lockdown") return "destructive";
  if (level === "stressed") return "warn";
  if (level === "guarded") return "default";
  return "long";
}

function badgeForLevel(level: RiskLevel) {
  if (level === "lockdown") return "destructive";
  if (level === "stressed") return "warn";
  if (level === "guarded") return "default";
  return "muted";
}

function postureColor(level: RiskLevel): string {
  if (level === "lockdown") return "hsl(var(--destructive))";
  if (level === "stressed") return "hsl(var(--warn))";
  if (level === "guarded") return "hsl(var(--primary))";
  return "hsl(var(--long))";
}

function textForLevel(level: RiskLevel): string {
  if (level === "lockdown") return "text-destructive";
  if (level === "stressed") return "text-warn";
  if (level === "guarded") return "text-primary";
  return "text-long";
}

function bgForLevel(level: RiskLevel): string {
  if (level === "lockdown") return "bg-destructive";
  if (level === "stressed") return "bg-warn";
  if (level === "guarded") return "bg-primary";
  return "bg-long";
}

function borderForLevel(level: RiskLevel): string {
  if (level === "lockdown") return "border-destructive/50 bg-destructive/5";
  if (level === "stressed") return "border-warn/50 bg-warn/5";
  if (level === "guarded") return "border-primary/50 bg-primary/5";
  return "border-long/40 bg-long/5";
}

function dotForLevel(level: RiskLevel): string {
  if (level === "lockdown") return "bg-destructive";
  if (level === "stressed") return "bg-warn";
  if (level === "guarded") return "bg-primary";
  return "bg-long animate-pulse-slow";
}

function threatLevelPct(level: RiskLevel): number {
  if (level === "lockdown") return 100;
  if (level === "stressed") return 78;
  if (level === "guarded") return 54;
  return 24;
}

const REGIME_STYLES: Record<
  string,
  {
    label: string;
    text: string;
    border: string;
    surface: string;
    dot: string;
    badge: "long" | "warn" | "destructive" | "muted";
  }
> = {
  risk_on: {
    label: "RISK ON",
    text: "text-long",
    border: "border-long/40",
    surface: "border-long/40 bg-long/5",
    dot: "bg-long animate-pulse-slow",
    badge: "long",
  },
  neutral: {
    label: "NEUTRAL",
    text: "text-muted-foreground",
    border: "border-border",
    surface: "border-border bg-muted/10",
    dot: "bg-muted-foreground",
    badge: "muted",
  },
  high_vol: {
    label: "HIGH VOL",
    text: "text-warn",
    border: "border-warn/40",
    surface: "border-warn/40 bg-warn/5",
    dot: "bg-warn",
    badge: "warn",
  },
  risk_off: {
    label: "RISK OFF",
    text: "text-destructive",
    border: "border-destructive/40",
    surface: "border-destructive/40 bg-destructive/5",
    dot: "bg-destructive",
    badge: "destructive",
  },
};
