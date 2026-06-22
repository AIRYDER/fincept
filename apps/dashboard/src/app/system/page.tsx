"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Check,
  CheckCircle2,
  Circle,
  CircleAlert,
  Copy,
  Database,
  KeyRound,
  Network,
  Power,
  Server,
  ShieldCheck,
  Terminal,
  XCircle,
} from "lucide-react";
import { useMemo, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { ModuleControlPanel } from "@/components/modules/module-control-panel";
import { StatusPill } from "@/components/widgets/status-pill";
import {
  buildSystemReadinessPacket,
  REQUIRED_ENV_VARS,
  OPTIONAL_ENV_VARS,
  type CopyableCommand,
  type ReadinessCheck,
  type ReadinessState,
} from "@/components/system/system-readiness";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SemanticIntent } from "@/lib/design-tokens";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Env var presence detection (names only — values are never read).
// Next.js inlines NEXT_PUBLIC_* at build time. For server-set vars we cannot
// see them from the client, so we mark them as "unknown" rather than missing.
// ---------------------------------------------------------------------------

function readEnvPresence(): Record<string, boolean> {
  const presence: Record<string, boolean> = {};
  const names = [...REQUIRED_ENV_VARS, ...OPTIONAL_ENV_VARS].map((v) => v.name);
  for (const name of names) {
    // NEXT_PUBLIC_ vars are inlined at build time
    const publicName = `NEXT_PUBLIC_${name}`;
    const publicVal = process.env[publicName];
    const directVal = process.env[name];
    presence[name] = Boolean(publicVal ?? directVal);
  }
  // The dashboard always knows its own API URL
  presence["FINCEPT_API_URL"] = Boolean(
    process.env.NEXT_PUBLIC_FINCEPT_API_URL ?? process.env.NEXT_PUBLIC_API_URL,
  );
  return presence;
}

// ---------------------------------------------------------------------------
// State → intent mapping
// ---------------------------------------------------------------------------

function readinessToIntent(state: ReadinessState): SemanticIntent {
  if (state === "ready" || state === "pass") return "verified";
  if (state === "review" || state === "warn" || state === "stale") return "degraded";
  if (state === "disabled" || state === "skipped") return "inactive";
  return "critical"; // blocked / fail
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function SystemPage() {
  const token = useAuth((s) => s.token);

  const servicesQ = useQuery({
    queryKey: ["services", "system"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });

  const killQ = useQuery({
    queryKey: ["kill-switch", "system"],
    queryFn: () => api.killSwitchState(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });

  const openbbQ = useQuery({
    queryKey: ["openbb-health", "system"],
    queryFn: () => api.openbbHealth(token),
    enabled: !!token,
    refetchInterval: 60_000,
  });

  // Server unified readiness (TASK-0202). Preferred source for categorized states.
  const readinessQ = useQuery({
    queryKey: ["readiness", "system"],
    queryFn: () => api.readiness(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });

  const packet = useMemo(
    () =>
      buildSystemReadinessPacket({
        servicesData: servicesQ.data,
        servicesError: servicesQ.isError,
        killSwitch: killQ.data,
        openbb: openbbQ.data,
        apiUrl:
          process.env.NEXT_PUBLIC_FINCEPT_API_URL ??
          process.env.NEXT_PUBLIC_API_URL ??
          null,
        envVarPresence: readEnvPresence(),
      }),
    [servicesQ.data, servicesQ.isError, killQ.data, openbbQ.data],
  );

  // Prefer server readiness checks (unified, includes Redis/Timescale probes etc).
  const serverChecks = readinessQ.data?.checks?.map((c) => ({
    id: c.id,
    label: c.label,
    state: c.state as ReadinessState,
    detail: c.detail,
  })) ?? packet.checks;

  const overallIntent = readinessToIntent(packet.state);

  return (
    <AppShell>
      <PageHeader
        title="System Readiness"
        description="Local dev / operator launch experience. Aggregates API, services, kill-switch, OpenBB, env vars, and proof receipts. No secrets are displayed — only names."
        action={
          <div className="flex items-center gap-2">
            <StatusPill intent={overallIntent} label={packet.state.toUpperCase()} />
            <Badge variant="outline" className="text-xs">
              Score {packet.score}/100
            </Badge>
          </div>
        }
      />

      <div className="mb-4 rounded-lg border border-border/40 bg-card/40 p-4">
        <p className="text-sm text-muted-foreground">{packet.headline}</p>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
        {/* Left column: Readiness checks + Services + Modules */}
        <div className="space-y-4">
          <ReadinessChecksCard checks={serverChecks} />
          <ServiceHeartbeatCard
            services={packet.services}
            summary={packet.serviceSummary}
            isLoading={servicesQ.isLoading}
          />
          <ModuleControlPanel />
        </div>

        {/* Right column: Env vars + Receipts */}
        <div className="space-y-4">
          <EnvVarCard envVars={packet.envVars} />
          <ReceiptStatusCard receipts={packet.receipts} />
          <ConnectivityCard
            apiUrl={
              process.env.NEXT_PUBLIC_FINCEPT_API_URL ??
              process.env.NEXT_PUBLIC_API_URL ??
              "(not set)"
            }
            api={packet.api}
            killSwitch={packet.killSwitch}
            openbb={packet.openbb}
          />
        </div>
      </div>

      {/* Full-width: Commands */}
      <div className="mt-4">
        <CommandsCard commands={packet.commands} />
      </div>
    </AppShell>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function ReadinessChecksCard({ checks }: { checks: ReadinessCheck[] }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <ShieldCheck className="h-4 w-4 text-primary" />
          Readiness checks
        </CardTitle>
        <CardDescription>
          Each check is read-only and derived from existing API responses.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {checks.map((check) => (
          <div
            key={check.id}
            className="flex items-start gap-3 rounded-md border border-border/30 bg-card/40 p-3"
          >
            <StateIcon state={check.state} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{check.label}</span>
                <StatusPill intent={readinessToIntent(check.state)} label={check.state.toUpperCase()} compact />
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{check.detail}</p>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function StateIcon({ state }: { state: ReadinessState }) {
  if (state === "ready" || state === "pass") return <CheckCircle2 className="h-4 w-4 shrink-0 text-long" />;
  if (state === "review" || state === "warn" || state === "stale") return <CircleAlert className="h-4 w-4 shrink-0 text-warn" />;
  if (state === "disabled" || state === "skipped") return <Circle className="h-4 w-4 shrink-0 text-muted-foreground" />;
  return <XCircle className="h-4 w-4 shrink-0 text-short" />;
}

function ServiceHeartbeatCard({
  services,
  summary,
  isLoading,
}: {
  services: Array<{ name: string; status: "up" | "stale" | "down" | "unknown"; age_sec: number | null; expected: boolean }>;
  summary: { up: number; stale: number; down: number; expected: number; total: number };
  isLoading: boolean;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <Server className="h-4 w-4 text-primary" />
              Service heartbeat
            </CardTitle>
            <CardDescription>
              Polled every 15s from /services. UP is fresh, STALE is late, DOWN is absent.
            </CardDescription>
          </div>
          <div className="flex gap-1">
            <Badge variant="long" className="text-xs">{summary.up} up</Badge>
            <Badge variant="warn" className="text-xs">{summary.stale} stale</Badge>
            <Badge variant="destructive" className="text-xs">{summary.down} down</Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="py-6 text-center text-xs text-muted-foreground">Loading heartbeat…</p>
        ) : services.length === 0 ? (
          <p className="py-6 text-center text-xs text-muted-foreground">
            No services reported. Start the stack with .\scripts\start.ps1
          </p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            {services.map((s) => (
              <ServiceRow key={s.name} svc={s} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ServiceRow({
  svc,
}: {
  svc: { name: string; status: "up" | "stale" | "down" | "unknown"; age_sec: number | null; expected: boolean };
}) {
  const intent: SemanticIntent =
    svc.status === "up" ? "verified" : svc.status === "stale" ? "degraded" : svc.status === "down" ? "critical" : "inactive";
  return (
    <div className="flex items-start justify-between gap-2 rounded-md border border-border/30 bg-card/40 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <StatusPill intent={intent} label={svc.status.toUpperCase()} compact />
          <span className="break-words font-mono text-xs leading-4">{svc.name}</span>
          {!svc.expected && (
            <Badge variant="outline" className="text-[9px]">rogue</Badge>
          )}
        </div>
      </div>
      <span className="shrink-0 text-[10px] text-muted-foreground">
        {svc.age_sec !== null ? `${Math.round(svc.age_sec)}s` : "—"}
      </span>
    </div>
  );
}

function EnvVarCard({
  envVars,
}: {
  envVars: Array<{ name: string; description: string; required: boolean; present: boolean }>;
}) {
  const required = envVars.filter((v) => v.required);
  const optional = envVars.filter((v) => !v.required);
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <KeyRound className="h-4 w-4 text-primary" />
          Environment variables
        </CardTitle>
        <CardDescription>
          Names only — values are <strong>never</strong> displayed. Server-side vars may show as missing in the browser context.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div>
          <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Required ({required.filter((v) => v.present).length}/{required.length})
          </h4>
          <div className="space-y-1.5">
            {required.map((v) => (
              <EnvVarRow key={v.name} env={v} />
            ))}
          </div>
        </div>
        <div>
          <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Optional ({optional.filter((v) => v.present).length}/{optional.length})
          </h4>
          <div className="space-y-1.5">
            {optional.map((v) => (
              <EnvVarRow key={v.name} env={v} />
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function EnvVarRow({
  env,
}: {
  env: { name: string; description: string; required: boolean; present: boolean };
}) {
  return (
    <div className="flex items-start gap-2 rounded border border-border/30 bg-card/30 px-2.5 py-1.5">
      {env.present ? (
        <Check className="mt-0.5 h-3 w-3 shrink-0 text-long" />
      ) : (
        <Circle className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <code className="text-[11px] font-medium">{env.name}</code>
          {env.required && !env.present && (
            <Badge variant="destructive" className="text-[9px]">missing</Badge>
          )}
        </div>
        <p className="text-xs leading-5 text-muted-foreground">{env.description}</p>
      </div>
    </div>
  );
}

function ReceiptStatusCard({
  receipts,
}: {
  receipts: { state: ReadinessState; total: number; dashboardExports: number; localScripts: number; liveScripts: number };
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Database className="h-4 w-4 text-primary" />
          Proof receipts
        </CardTitle>
        <CardDescription>Catalog status across dashboard exports and local/live proof scripts.</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Stat label="Total" value={receipts.total} />
          <Stat label="Dashboard" value={receipts.dashboardExports} />
          <Stat label="Local scripts" value={receipts.localScripts} />
          <Stat label="Live scripts" value={receipts.liveScripts} />
        </div>
        <a href="/receipts" className="mt-3 inline-flex text-xs text-primary hover:underline">
          → View receipt catalog
        </a>
      </CardContent>
    </Card>
  );
}

function ConnectivityCard({
  apiUrl,
  api,
  killSwitch,
  openbb,
}: {
  apiUrl: string;
  api: { reachable: boolean; detail: string };
  killSwitch: { state: "clear" | "engaged" | "unknown"; detail: string };
  openbb: { state: "ok" | "degraded" | "down" | "unknown"; detail: string };
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Network className="h-4 w-4 text-primary" />
          Connectivity
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-xs">
        <div>
          <div className="text-muted-foreground">API URL</div>
          <code className="break-all text-[11px]">{apiUrl}</code>
        </div>
        <div className="flex items-center justify-between border-t border-border/30 pt-2">
          <span>API</span>
          <StatusPill
            intent={api.reachable ? "verified" : "critical"}
            label={api.reachable ? "REACHABLE" : "UNREACHABLE"}
            compact
          />
        </div>
        <div className="flex items-center justify-between">
          <span>Kill switch</span>
          <StatusPill
            intent={killSwitch.state === "clear" ? "verified" : killSwitch.state === "engaged" ? "critical" : "inactive"}
            label={killSwitch.state.toUpperCase()}
            compact
          />
        </div>
        <div className="flex items-center justify-between">
          <span>OpenBB</span>
          <StatusPill
            intent={openbb.state === "ok" ? "verified" : openbb.state === "down" ? "critical" : "degraded"}
            label={openbb.state.toUpperCase()}
            compact
          />
        </div>
      </CardContent>
    </Card>
  );
}

function CommandsCard({ commands }: { commands: CopyableCommand[] }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Terminal className="h-4 w-4 text-primary" />
          Operator commands
        </CardTitle>
        <CardDescription>
          Copy-pasteable Windows PowerShell commands. Commands marked <span className="text-warn">unsafe</span> mutate system state (start/stop services, migrations).
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid gap-2 md:grid-cols-2">
          {commands.map((cmd) => (
            <CommandRow key={cmd.id} cmd={cmd} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function CommandRow({ cmd }: { cmd: CopyableCommand }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(cmd.command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard not available
    }
  };
  return (
    <div className="flex items-start gap-2 rounded-md border border-border/30 bg-card/40 p-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium">{cmd.label}</span>
          {!cmd.safe && <Badge variant="warn" className="text-[9px]">unsafe</Badge>}
        </div>
        <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{cmd.description}</p>
        <code className="mt-1 block break-all rounded bg-muted/40 px-2 py-1 text-[10px]">{cmd.command}</code>
      </div>
      <Button
        size="sm"
        variant="ghost"
        className={cn("h-7 w-7 shrink-0 p-0", copied && "text-long")}
        onClick={copy}
        title="Copy to clipboard"
      >
        {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      </Button>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded border border-border/30 bg-card/30 px-2.5 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="text-base font-semibold">{value}</div>
    </div>
  );
}
