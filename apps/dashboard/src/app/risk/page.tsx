"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  Bell,
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
import type { AlertEvent, WsFrame } from "@/lib/types";
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

  // Per-symbol notional aggregation.
  const exposureBySymbol = new Map<string, number>();
  for (const p of positions ?? []) {
    const px = asNum(p.current_mark_price ?? p.avg_entry_price);
    const notional = Math.abs(asNum(p.quantity)) * px;
    exposureBySymbol.set(
      p.symbol,
      (exposureBySymbol.get(p.symbol) ?? 0) + notional,
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
