"use client";

import { useQuery } from "@tanstack/react-query";
import { Ghost } from "lucide-react";

import { AppShell } from "@/components/shell/app-shell";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { StatusPill } from "@/components/widgets/status-pill";
import { api, UnavailableError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SemanticIntent } from "@/lib/design-tokens";
import type { QuantFoundryShadowHealth } from "@/lib/types";

// Rejection tracking is not yet durable on the gateway side; when the
// gateway reports a null rate, surface the documented reason inline so
// operators don't think the dashboard is broken.
const REJECTION_TRACKING_NOTE = "rejection tracking not yet durable";

export default function QuantFoundryShadowHealthPage() {
  const token = useAuth((s) => s.token);
  const healthQ = useQuery({
    queryKey: ["quant-foundry", "shadow", "health"],
    queryFn: () => api.quantFoundryShadowHealth(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  // 503 = gateway absent = safe disabled state (mirrors the dashboard's
  // existing pattern for jobs / tournament / promotion pages).
  const disabled = healthQ.error instanceof UnavailableError && healthQ.error.status === 503;
  const health = healthQ.data;
  const realError = healthQ.error && !disabled ? healthQ.error : null;

  return (
    <AppShell>
      <PageHeader
        title="Shadow Inference Health"
        description="Read-only aggregate of the shadow prediction surface: model count, latency, feature availability, and circuit-breaker state. Disabled is the safe resting state."
        action={
          <StatusPill
            intent={pillIntent(health, disabled)}
            label={disabled ? "DISABLED" : health?.enabled ? "SHADOW ACTIVE" : "DISABLED"}
          />
        }
      />

      {disabled ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <Ghost className="h-4 w-4 text-primary" />
              Shadow inference gateway
            </CardTitle>
            <CardDescription>The Quant Foundry gateway is not configured.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState
              title="Quant Foundry is disabled"
              body="No shadow predictions are produced or stored while the gateway is absent or disabled."
            />
          </CardContent>
        </Card>
      ) : realError ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <Ghost className="h-4 w-4 text-primary" />
              Shadow inference gateway
            </CardTitle>
            <CardDescription>Unable to read shadow health.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState
              title="Unable to load shadow health"
              body={realError instanceof Error ? realError.message : "Unknown error"}
            />
          </CardContent>
        </Card>
      ) : healthQ.isLoading || !health ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <Ghost className="h-4 w-4 text-primary" />
              Shadow inference gateway
            </CardTitle>
            <CardDescription>Reading shadow prediction ledger.</CardDescription>
          </CardHeader>
          <CardContent>
            <EmptyState title="Loading shadow health" body="Aggregating predictions from the shadow ledger." />
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          <SummaryCard health={health} />
          <LatencyCard health={health} />
          <DriftCard health={health} />
        </div>
      )}
    </AppShell>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function SummaryCard({ health }: { readonly health: QuantFoundryShadowHealth }) {
  const anyPredictions = health.prediction_count > 0;
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Ghost className="h-4 w-4 text-primary" />
          Shadow prediction surface
        </CardTitle>
        <CardDescription>
          Aggregate counts from the durable shadow prediction ledger. Empty ledger is a safe state, not a failure.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <MetricRow label="Enabled" value={health.enabled ? "yes" : "no"} />
        <MetricRow label="Models running" value={String(health.models_running)} />
        <MetricRow label="Prediction count" value={String(health.prediction_count)} />
        <MetricRow label="Settled count" value={String(health.settled_count)} />
        <MetricRow
          label="Latest prediction ts"
          value={health.latest_prediction_ts === null ? "—" : formatTs(health.latest_prediction_ts)}
        />
        {!anyPredictions && (
          <p className="pt-2 text-xs text-muted-foreground">
            No predictions stored yet. Shadow inference produces ledger entries only when run with a wired model surface.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function LatencyCard({ health }: { readonly health: QuantFoundryShadowHealth }) {
  const featurePct = health.feature_availability === null ? null : health.feature_availability * 100;
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          Latency &amp; feature availability
        </CardTitle>
        <CardDescription>
          Linear-interpolation percentiles over stored prediction latencies. Null means the metric is not yet computable from durable state.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <MetricRow
          label="Latency p50 (ms)"
          value={health.latency_p50_ms === null ? "—" : health.latency_p50_ms.toFixed(2)}
        />
        <MetricRow
          label="Latency p95 (ms)"
          value={health.latency_p95_ms === null ? "—" : health.latency_p95_ms.toFixed(2)}
        />
        <MetricRow
          label="Feature availability"
          value={
            featurePct === null
              ? "—"
              : `${featurePct.toFixed(1)}%`
          }
        />
      </CardContent>
    </Card>
  );
}

function DriftCard({ health }: { readonly health: QuantFoundryShadowHealth }) {
  const settlement = health.settlement_lag_seconds;
  const rejection = health.callback_rejection_rate;
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          Drift &amp; circuit-breaker
        </CardTitle>
        <CardDescription>
          Read-only drift / settlement signals. Null values mean the source data is not yet durable (no synthetic fills invented).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">Circuit-breaker state</span>
          <StatusPill intent={circuitIntent(health.circuit_breaker_state)} label={health.circuit_breaker_state.toUpperCase()} compact />
        </div>
        <MetricRow
          label="Settlement lag (s)"
          value={settlement === null ? "—" : settlement.toFixed(2)}
        />
        <MetricRow
          label="Callback rejection rate"
          value={
            rejection === null
              ? `— (${REJECTION_TRACKING_NOTE})`
              : `${(rejection * 100).toFixed(2)}%`
          }
        />
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

function pillIntent(health: QuantFoundryShadowHealth | undefined, disabled: boolean): SemanticIntent {
  if (disabled || !health || !health.enabled) return "inactive";
  if (health.circuit_breaker_state === "open") return "critical";
  if (health.circuit_breaker_state === "half_open") return "degraded";
  return "verified";
}

function circuitIntent(state: QuantFoundryShadowHealth["circuit_breaker_state"]): SemanticIntent {
  if (state === "open") return "critical";
  if (state === "half_open") return "degraded";
  return "verified";
}

function formatTs(ts: number): string {
  // Shadow prediction timestamps are event-time ints (not necessarily
  // nanoseconds). Render as locale string when they look like seconds; as
  // nanoseconds when they exceed the seconds range.
  if (ts > 10_000_000_000) {
    return new Date(Math.floor(ts / 1_000_000)).toLocaleString();
  }
  return new Date(Math.floor(ts * 1000)).toLocaleString();
}