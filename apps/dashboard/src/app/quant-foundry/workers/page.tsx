"use client";

import { useQuery } from "@tanstack/react-query";
import { HeartPulse } from "lucide-react";

import { AppShell } from "@/components/shell/app-shell";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { StatusPill } from "@/components/widgets/status-pill";
import { api, UnavailableError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SemanticIntent } from "@/lib/design-tokens";
import type {
  QuantFoundryWorkerHealth,
  QuantFoundryWorkerStatus,
} from "@/lib/types";

export default function QuantFoundryWorkerHealthPage() {
  const token = useAuth((s) => s.token);
  const healthQ = useQuery({
    queryKey: ["quant-foundry", "worker-health"],
    queryFn: () => api.quantFoundryWorkerHealth(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  // 503 = gateway absent = safe disabled state (mirrors the dashboard's
  // existing pattern for jobs / tournament / promotion / shadow pages).
  const disabled = healthQ.error instanceof UnavailableError && healthQ.error.status === 503;
  const health = healthQ.data;
  const realError = healthQ.error && !disabled ? healthQ.error : null;

  return (
    <AppShell>
      <PageHeader
        title="Worker Health"
        description="RunPod worker heartbeat status and stale worker detection. Surfaces the live heartbeat ledger plus any workers that have crossed the stale threshold. Disabled is the safe resting state."
        action={
          <StatusPill
            intent={pillIntent(health, disabled)}
            label={disabled ? "DISABLED" : health?.enabled ? "ACTIVE" : "DISABLED"}
          />
        }
      />

      {disabled ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <HeartPulse className="h-4 w-4 text-primary" />
              Worker heartbeat gateway
            </CardTitle>
            <CardDescription>The Quant Foundry gateway is not configured.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState
              title="Quant Foundry is disabled"
              body="No worker heartbeats are produced or tracked while the gateway is absent or disabled."
            />
          </CardContent>
        </Card>
      ) : realError ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <HeartPulse className="h-4 w-4 text-primary" />
              Worker heartbeat gateway
            </CardTitle>
            <CardDescription>Unable to read worker health.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState
              title="Unable to load worker health"
              body={realError instanceof Error ? realError.message : "Unknown error"}
            />
          </CardContent>
        </Card>
      ) : healthQ.isLoading || !health ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <HeartPulse className="h-4 w-4 text-primary" />
              Worker heartbeat gateway
            </CardTitle>
            <CardDescription>Reading worker heartbeat ledger.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState title="Loading worker health" body="Aggregating heartbeats from the worker status directory." />
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          <SummaryCard health={health} />
          <WorkerStatusCard health={health} />
          {health.stale_count > 0 && <StaleWorkersCard health={health} />}
        </div>
      )}
    </AppShell>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function SummaryCard({ health }: { readonly health: QuantFoundryWorkerHealth }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <HeartPulse className="h-4 w-4 text-primary" />
          Worker heartbeat summary
        </CardTitle>
        <CardDescription>
          Aggregate counts from the worker status directory. Empty ledger is a safe state, not a failure.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <MetricRow label="Enabled" value={health.enabled ? "yes" : "no"} />
        <MetricRow label="Total workers" value={String(health.total_workers)} />
        <MetricRow label="Stale count" value={String(health.stale_count)} />
        <MetricRow
          label="Stale threshold (s)"
          value={String(health.stale_threshold_seconds)}
        />
        <MetricRow
          label="Worker status dir"
          value={health.worker_status_dir === null ? "not configured" : health.worker_status_dir}
        />
        {health.total_workers === 0 && (
          <p className="pt-2 text-xs text-muted-foreground">
            No worker heartbeats recorded yet. Heartbeats appear once a RunPod worker writes its status file.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function WorkerStatusCard({ health }: { readonly health: QuantFoundryWorkerHealth }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          Worker status
        </CardTitle>
        <CardDescription>
          All heartbeats read from {health.worker_status_dir ?? "the worker status directory"}. Relative times are computed against render time.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {health.heartbeats.length === 0 ? (
          <p className="py-6 text-center text-xs text-muted-foreground">
            No heartbeats yet. Workers appear once they write a status file.
          </p>
        ) : (
          <div className="space-y-2">
            {health.heartbeats.map((hb) => (
              <WorkerRow key={hb.job_id} hb={hb} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function StaleWorkersCard({ health }: { readonly health: QuantFoundryWorkerHealth }) {
  return (
    <Card className="border-short/40">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal text-short">
          <HeartPulse className="h-4 w-4 text-short" />
          Stale workers ({health.stale_count})
        </CardTitle>
        <CardDescription>
          Workers whose heartbeats exceed the stale threshold of {health.stale_threshold_seconds}s. Investigate or restart these workers.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {health.stale_workers.map((hb) => (
          <div
            key={hb.job_id}
            className="flex items-center justify-between rounded-md border border-short/30 bg-short/5 p-3 text-xs"
          >
            <div className="min-w-0">
              <div className="truncate font-medium">{hb.job_id}</div>
              <div className="text-muted-foreground">status: {hb.status}</div>
            </div>
            <div className="shrink-0 text-right">
              <StatusPill intent="critical" label="STALE" compact />
              <div className="mt-1 text-muted-foreground">
                last heartbeat {relativeTime(hb.heartbeat_at)}
              </div>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Rows & helpers
// ---------------------------------------------------------------------------

function WorkerRow({ hb }: { readonly hb: QuantFoundryWorkerStatus }) {
  return (
    <div className="flex items-center justify-between rounded-md border border-border/30 bg-card/40 p-3 text-xs">
      <div className="min-w-0">
        <div className="truncate font-medium">{hb.job_id}</div>
        <div className="text-muted-foreground">status: {hb.status}</div>
      </div>
      <div className="shrink-0 text-right">
        <StatusPill intent={statusIntent(hb.status)} label={hb.status.toUpperCase()} compact />
        <div className="mt-1 text-muted-foreground">
          heartbeat {relativeTime(hb.heartbeat_at)}
        </div>
        <div className="text-muted-foreground">
          updated {relativeTime(hb.updated_at)}
        </div>
      </div>
    </div>
  );
}

function MetricRow({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">{value}</span>
    </div>
  );
}

function EmptyState({ title, body }: { readonly title: string; readonly body: string }) {
  return (
    <div className="rounded-md border border-border/30 bg-card/40 p-6 text-center">
      <p className="text-sm font-medium">{title}</p>
      <p className="mt-1 text-xs text-muted-foreground">{body}</p>
    </div>
  );
}

function pillIntent(health: QuantFoundryWorkerHealth | undefined, disabled: boolean): SemanticIntent {
  if (disabled || !health || !health.enabled) return "inactive";
  if (health.stale_count > 0) return "degraded";
  return "verified";
}

function statusIntent(status: string): SemanticIntent {
  const s = status.toLowerCase();
  if (s === "stale" || s === "error" || s === "failed" || s === "dead") return "critical";
  if (s === "disabled" || s === "stopped" || s === "paused") return "inactive";
  return "verified";
}

/**
 * Format a heartbeat timestamp (seconds since epoch) as a relative time
 * string, e.g. "3s ago", "2m ago", "1h ago". Falls back to a locale string
 * for timestamps older than a day.
 */
function relativeTime(ts: number): string {
  const now = Date.now() / 1000;
  const delta = Math.max(0, now - ts);
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleString();
}
