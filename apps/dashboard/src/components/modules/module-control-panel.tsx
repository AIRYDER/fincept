"use client";

/**
 * ModuleControlPanel — on-demand module control for the operator (TASK-0203).
 *
 * Renders the allowlisted module registry with live status, idle countdown,
 * start/stop/restart controls, a "Stop all optional modules" button, and the
 * recent receipts catalog. All actions call the auth-required, local-only
 * /modules endpoints; no shell commands are constructed client-side.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Boxes, Power, PowerOff, RefreshCw, Square, History } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import type {
  ModuleCostClass,
  ModuleReceipt,
  ModuleStatus,
  ModuleSummary,
} from "@/lib/types";
import { cn } from "@/lib/utils";

function statusIntent(status: ModuleStatus): SemanticIntent {
  if (status === "running") return "verified";
  if (status === "degraded") return "degraded";
  if (status === "idle") return "degraded";
  if (status === "stopped") return "inactive";
  return "inactive";
}

const COST_VARIANT: Record<ModuleCostClass, "long" | "warn" | "destructive"> = {
  low: "long",
  medium: "warn",
  high: "destructive",
};

function formatDuration(sec: number): string {
  if (sec <= 0) return "0s";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m === 0) return `${s}s`;
  return `${m}m ${s}s`;
}

function formatTimeAgo(unix: number | null): string {
  if (unix === null) return "—";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - unix));
  return formatDuration(sec) + " ago";
}

export function ModuleControlPanel() {
  const token = useAuth((s) => s.token);
  const qc = useQueryClient();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);

  const modulesQ = useQuery({
    queryKey: ["modules"],
    queryFn: () => api.modules(token),
    enabled: !!token,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });

  const receiptsQ = useQuery({
    queryKey: ["module-receipts"],
    queryFn: () => api.moduleReceipts(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["modules"] });
    void qc.invalidateQueries({ queryKey: ["module-receipts"] });
  };

  const startMut = useMutation({
    mutationFn: (moduleId: string) => api.startModule(token, moduleId),
    onMutate: (id) => setBusyId(id),
    onSuccess: () => {
      setLastError(null);
      invalidate();
    },
    onError: (e: unknown) => setLastError(e instanceof Error ? e.message : String(e)),
    onSettled: () => setBusyId(null),
  });

  const stopMut = useMutation({
    mutationFn: (moduleId: string) => api.stopModule(token, moduleId),
    onMutate: (id) => setBusyId(id),
    onSuccess: () => {
      setLastError(null);
      invalidate();
    },
    onError: (e: unknown) => setLastError(e instanceof Error ? e.message : String(e)),
    onSettled: () => setBusyId(null),
  });

  const restartMut = useMutation({
    mutationFn: (moduleId: string) => api.restartModule(token, moduleId),
    onMutate: (id) => setBusyId(id),
    onSuccess: () => {
      setLastError(null);
      invalidate();
    },
    onError: (e: unknown) => setLastError(e instanceof Error ? e.message : String(e)),
    onSettled: () => setBusyId(null),
  });

  const stopAllMut = useMutation({
    mutationFn: () => api.stopAllModules(token),
    onSuccess: () => {
      setLastError(null);
      invalidate();
    },
    onError: (e: unknown) => setLastError(e instanceof Error ? e.message : String(e)),
  });

  const sweepMut = useMutation({
    mutationFn: () => api.sweepIdleModules(token),
    onSuccess: () => {
      setLastError(null);
      invalidate();
    },
    onError: (e: unknown) => setLastError(e instanceof Error ? e.message : String(e)),
  });

  const modules: ModuleSummary[] = modulesQ.data?.modules ?? [];
  const runningCount = modules.filter((m) => m.status === "running").length;
  const receipts: ModuleReceipt[] = receiptsQ.data?.receipts ?? [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <Boxes className="h-4 w-4 text-primary" />
              On-demand modules
            </CardTitle>
            <CardDescription>
              Optional modules run only when you start them. Idle modules auto-stop after their timeout.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-xs">
              {runningCount} running
            </Badge>
            <Button
              size="sm"
              variant="outline"
              onClick={() => sweepMut.mutate()}
              disabled={sweepMut.isPending}
              title="Stop modules past their idle timeout"
            >
              <RefreshCw className={cn("h-3 w-3", sweepMut.isPending && "animate-spin")} />
              Sweep idle
            </Button>
            <Button
              size="sm"
              variant="destructive"
              onClick={() => stopAllMut.mutate()}
              disabled={stopAllMut.isPending || runningCount === 0}
              title="Stop every running optional module"
            >
              <Square className="h-3 w-3" />
              Stop all
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {lastError && (
          <div className="rounded border border-short/40 bg-short/10 px-3 py-2 text-xs text-short">
            {lastError}
          </div>
        )}
        {modulesQ.isLoading ? (
          <p className="py-4 text-center text-xs text-muted-foreground">Loading modules…</p>
        ) : modules.length === 0 ? (
          <p className="py-4 text-center text-xs text-muted-foreground">
            No modules registered.
          </p>
        ) : (
          <div className="space-y-2">
            {modules.map((m) => (
              <ModuleRow
                key={m.module_id}
                mod={m}
                busy={busyId === m.module_id}
                onStart={() => startMut.mutate(m.module_id)}
                onStop={() => stopMut.mutate(m.module_id)}
                onRestart={() => restartMut.mutate(m.module_id)}
              />
            ))}
          </div>
        )}

        {receipts.length > 0 && (
          <div className="mt-4 border-t border-border/30 pt-3">
            <div className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
              <History className="h-3 w-3" />
              Recent receipts
            </div>
            <div className="max-h-40 space-y-1 overflow-y-auto">
              {receipts.slice(0, 10).map((r, i) => (
                <div
                  key={`${r.module_id}-${r.ts_unix}-${i}`}
                  className="flex items-center justify-between gap-2 rounded border border-border/20 bg-card/30 px-2 py-1 text-[11px]"
                >
                  <span className="font-mono">{r.module_id}</span>
                  <Badge
                    variant={
                      r.action === "auto_stop"
                        ? "warn"
                        : r.action === "start"
                          ? "long"
                          : r.action === "stop"
                            ? "outline"
                            : "secondary"
                    }
                    className="text-[9px]"
                  >
                    {r.action}
                  </Badge>
                  <span className="text-muted-foreground">{r.status}</span>
                  <span className="text-muted-foreground">{formatTimeAgo(r.ts_unix)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ModuleRow({
  mod,
  busy,
  onStart,
  onStop,
  onRestart,
}: {
  mod: ModuleSummary;
  busy: boolean;
  onStart: () => void;
  onStop: () => void;
  onRestart: () => void;
}) {
  const running = mod.status === "running";
  const idlePct =
    mod.idle_timeout_sec > 0
      ? Math.min(100, Math.round((mod.idle_seconds / mod.idle_timeout_sec) * 100))
      : 0;
  return (
    <div className="rounded-md border border-border/30 bg-card/40 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <StatusPill intent={statusIntent(mod.status)} label={mod.status.toUpperCase()} compact />
            <span className="text-sm font-medium">{mod.display_name}</span>
            <Badge variant={COST_VARIANT[mod.cost_class]} className="text-[9px]">
              {mod.cost_class} cost
            </Badge>
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{mod.description}</p>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
            <span className="font-mono">{mod.module_id}</span>
            {mod.services.length > 0 && (
              <span>services: {mod.services.join(", ")}</span>
            )}
            {running && (
              <>
                <span>idle: {formatDuration(mod.idle_seconds)}</span>
                <span>timeout in: {formatDuration(mod.idle_countdown_sec)}</span>
              </>
            )}
            {mod.fresh_services.length > 0 && (
              <span className="text-long">fresh: {mod.fresh_services.join(", ")}</span>
            )}
          </div>
          {running && idlePct > 0 && (
            <div className="mt-1.5 h-1 w-full overflow-hidden rounded bg-muted/40">
              <div
                className={cn(
                  "h-full transition-all",
                  idlePct > 80 ? "bg-short" : idlePct > 50 ? "bg-amber-400" : "bg-cyan",
                )}
                style={{ width: `${idlePct}%` }}
              />
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0"
            disabled={busy || running}
            onClick={onStart}
            title="Start module"
          >
            <Power className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0"
            disabled={busy || !running}
            onClick={onStop}
            title="Stop module"
          >
            <PowerOff className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0"
            disabled={busy || !running}
            onClick={onRestart}
            title="Restart module"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} />
          </Button>
        </div>
      </div>
    </div>
  );
}
