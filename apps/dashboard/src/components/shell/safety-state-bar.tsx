"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { useFinceptStream } from "@/lib/ws";

type SafetyTone = "ok" | "warn" | "critical" | "muted";

const DAY_NS = 24 * 60 * 60 * 1_000_000_000;
const FIFTEEN_MIN_NS = 15 * 60 * 1_000_000_000;

function toneClass(tone: SafetyTone) {
  if (tone === "ok") return "border-long/40 bg-long/10 text-long";
  if (tone === "warn") return "border-warn/40 bg-warn/10 text-warn";
  if (tone === "critical") return "border-short/50 bg-short/10 text-short";
  return "border-border/70 bg-background/30 text-muted-foreground";
}

function StatusChip({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: string;
  tone: SafetyTone;
  title?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 border px-2 py-[1px] text-[10px] font-semibold uppercase tracking-wider",
        toneClass(tone),
      )}
      title={title}
    >
      <span className="text-muted-foreground">{label}</span>
      <span>{value}</span>
    </span>
  );
}

export function SafetyStateBar() {
  const token = useAuth((s) => s.token);
  const { status: wsStatus } = useFinceptStream({ topics: ["alerts"] });

  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(token),
    refetchInterval: 15_000,
    retry: 0,
  });
  const killSwitch = useQuery({
    queryKey: ["kill-switch", "state"],
    queryFn: () => api.killSwitchState(token),
    enabled: !!token,
    refetchInterval: 5_000,
    retry: 0,
  });
  const services = useQuery({
    queryKey: ["services"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 15_000,
    retry: 0,
  });
  const coverage = useQuery({
    queryKey: ["data", "coverage", "safety", "1m"],
    queryFn: () =>
      api.dataCoverage(token, {
        freq: "1m",
        lookback_ns: DAY_NS,
        stale_after_ns: FIFTEEN_MIN_NS,
      }),
    enabled: !!token,
    refetchInterval: 30_000,
    retry: 0,
  });
  const openbb = useQuery({
    queryKey: ["openbb", "health"],
    queryFn: () => api.openbbHealth(token),
    enabled: !!token,
    refetchInterval: 30_000,
    retry: 0,
  });

  const apiOk = !!health.data?.ok && !health.isError;
  const killEngaged = !!killSwitch.data?.engaged;
  const coreDown =
    services.data?.services.filter((s) => s.expected && s.status === "down") ?? [];
  const coreStale =
    services.data?.services.filter((s) => s.expected && s.status === "stale") ?? [];
  const serviceWarn = services.isError || coreDown.length > 0 || coreStale.length > 0;
  const coverageSummary = coverage.data?.summary;
  const coverageDegraded =
    coverage.isError ||
    !coverageSummary ||
    coverageSummary.total === 0 ||
    coverageSummary.stale > 0 ||
    coverageSummary.empty > 0 ||
    coverageSummary.error > 0;
  const openbbOk = !!openbb.data?.ok && !openbb.isError;
  const openbbWarn = !openbbOk || !!openbb.data?.warning;
  const wsWarn = wsStatus !== "open";
  const critical = killEngaged || !apiOk || coreDown.length > 0;
  const warn =
    !critical &&
    (serviceWarn ||
      coverageDegraded ||
      openbbWarn ||
      wsWarn ||
      coreStale.length > 0);
  const overallTone: SafetyTone = critical ? "critical" : warn ? "warn" : "ok";
  const overallLabel = critical ? "DEGRADED" : warn ? "WATCH" : "READY";
  const serviceValue = services.data
    ? `${services.data.summary.up}/${services.data.summary.expected}`
    : services.isError
      ? "ERR"
      : "—";
  const coverageValue = coverageSummary
    ? `${Math.round(coverageSummary.coverage_pct)}%`
    : coverage.isError
      ? "ERR"
      : "—";

  return (
    <div className="flex h-7 shrink-0 items-center gap-2 overflow-hidden border-b border-border bg-card/80 px-2 text-[10px] uppercase tracking-wider print:hidden">
      <Link
        href="/risk"
        className={cn("border px-2 py-[1px] font-bold", toneClass(overallTone))}
      >
        Safety {overallLabel}
      </Link>
      <StatusChip
        label="Mode"
        value="Paper"
        tone="warn"
        title="Execution is paper-first; live capital remains gated."
      />
      <StatusChip
        label="Kill"
        value={killEngaged ? "Engaged" : killSwitch.isError ? "Unknown" : "Clear"}
        tone={killEngaged ? "critical" : killSwitch.isError ? "warn" : "ok"}
        title={killSwitch.data?.reason ?? "Kill-switch state"}
      />
      <StatusChip
        label="API"
        value={apiOk ? health.data?.version ?? "OK" : "Offline"}
        tone={apiOk ? "ok" : "critical"}
      />
      <Link href="/reconciliation">
        <StatusChip
          label="Core"
          value={serviceValue}
          tone={coreDown.length > 0 ? "critical" : serviceWarn ? "warn" : "ok"}
          title={
            services.data?.services.map((s) => `${s.name}:${s.status}`).join(" · ") ??
            "Service heartbeat state"
          }
        />
      </Link>
      <Link href="/markets">
        <StatusChip
          label="Data"
          value={coverageValue}
          tone={coverageDegraded ? "warn" : "ok"}
          title={
            coverageSummary
              ? `${coverageSummary.ok} ok · ${coverageSummary.stale} stale · ${coverageSummary.empty} empty · ${coverageSummary.error} error`
              : "Data coverage"
          }
        />
      </Link>
      <Link href="/research">
        <StatusChip
          label="OpenBB"
          value={openbbOk ? `${openbb.data?.latency_ms ?? 0}ms` : "Down"}
          tone={openbbWarn ? "warn" : "ok"}
          title={
            openbb.data?.error ??
            openbb.data?.warning ??
            openbb.data?.url ??
            "OpenBB health"
          }
        />
      </Link>
      <StatusChip label="WS" value={wsStatus} tone={wsWarn ? "warn" : "ok"} />
      <span className="min-w-0 flex-1 truncate text-muted-foreground">
        P0 guardrail: AI and optimizer outputs are read-only planning until readiness gates and receipts pass.
      </span>
    </div>
  );
}
