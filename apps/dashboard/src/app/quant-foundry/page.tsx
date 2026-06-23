"use client";

/**
 * TASK-0801: Quant Foundry Overview Page.
 *
 * Read-only overview of the Quant Foundry module surface. Shows:
 * - Module status cards (Gateway, Outbox, Callback Inbox, Feature Lake,
 *   Settlement, Dossier Registry, Tournament, RunPod Research, Shadow
 *   Inference).
 * - Global mode (disabled, local_mock, runpod_research, runpod_shadow,
 *   paper_bridge).
 * - Cost/budget state.
 * - Latest receipts.
 *
 * Acceptance criteria:
 * - Page loads in disabled mode (disabled is NOT shown as failure).
 * - No action can promote or trade from this overview.
 *
 * File-disjoint from all active builders. The route is a new file under
 * apps/dashboard/src/app/quant-foundry/. API client methods are additive
 * to apps/dashboard/src/lib/api.ts (TASK-0204 owner: Builder 1).
 */

import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Archive,
  Boxes,
  Brain,
  CircleDollarSign,
  FlaskConical,
  Gauge,
  Ghost,
  Inbox as InboxIcon,
  Layers,
  Send,
  ShieldCheck,
  Trophy,
} from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { StatusPill } from "@/components/widgets/status-pill";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { api, UnavailableError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SemanticIntent } from "@/lib/design-tokens";
import type {
  QuantFoundryHealthResponse,
  QuantFoundryJob,
} from "@/lib/types";

// ---------------------------------------------------------------------------
// Module catalog — the 9 Quant Foundry modules the overview tracks.
// ---------------------------------------------------------------------------

interface ModuleCardSpec {
  id: string;
  label: string;
  description: string;
  icon: typeof Gauge;
}

const MODULES: readonly ModuleCardSpec[] = [
  {
    id: "gateway",
    label: "Gateway",
    description: "HTTP facade for jobs, callbacks, health, heartbeats.",
    icon: Gauge,
  },
  {
    id: "outbox",
    label: "Outbox",
    description: "Durable local job outbox (idempotent, restart-safe).",
    icon: Send,
  },
  {
    id: "inbox",
    label: "Callback Inbox",
    description: "HMAC-signed callback inbox (tamper/replay rejection).",
    icon: InboxIcon,
  },
  {
    id: "feature_lake",
    label: "Feature Lake",
    description: "Dataset + feature manifest builder (fixture-backed MVP).",
    icon: Layers,
  },
  {
    id: "settlement",
    label: "Settlement",
    description: "Post-horizon prediction settlement ledger (net-of-cost).",
    icon: ShieldCheck,
  },
  {
    id: "dossier_registry",
    label: "Dossier Registry",
    description: "Reproducibility-complete model dossiers (immutable, hashed).",
    icon: Archive,
  },
  {
    id: "tournament",
    label: "Tournament",
    description: "Cost-adjusted model ranking (DSR + block bootstrap).",
    icon: Trophy,
  },
  {
    id: "runpod_research",
    label: "RunPod Research",
    description: "Remote training dispatch (not yet wired — Phase 5).",
    icon: FlaskConical,
  },
  {
    id: "shadow_inference",
    label: "Shadow Inference",
    description: "Shadow-only predictions (no sig.predict — Phase 6).",
    icon: Ghost,
  },
] as const;

const SUB_PAGES = [
  {
    href: "/quant-foundry/jobs",
    label: "Jobs",
    description: "Queued, running, retrying, failed, and completed jobs.",
    icon: Activity,
  },
  {
    href: "/quant-foundry/models",
    label: "Models",
    description: "Dossier registry, artifact hashes, and evidence completeness.",
    icon: Archive,
  },
  {
    href: "/quant-foundry/tournament",
    label: "Tournament",
    description: "Leaderboard, baseline deltas, and decay flags.",
    icon: Trophy,
  },
  {
    href: "/quant-foundry/promotion",
    label: "Promotion",
    description: "Review packets, confirmation preview, and rollback visibility.",
    icon: ShieldCheck,
  },
] as const;

// ---------------------------------------------------------------------------
// Mode helpers
// ---------------------------------------------------------------------------

type QFMode =
  | "disabled"
  | "local_mock"
  | "runpod_research"
  | "runpod_shadow"
  | "paper_bridge";

function classifyMode(health: QuantFoundryHealthResponse | undefined): QFMode {
  if (!health) return "disabled";
  if (!health.enabled) return "disabled";
  const mode = health.mode ?? "local_mock";
  if (mode === "local_mock") return "local_mock";
  if (mode === "runpod_research") return "runpod_research";
  if (mode === "runpod_shadow") return "runpod_shadow";
  if (mode === "paper_bridge") return "paper_bridge";
  return "local_mock";
}

function modeToIntent(mode: QFMode): SemanticIntent {
  if (mode === "disabled") return "inactive";
  if (mode === "local_mock") return "verified";
  return "degraded"; // runpod_* / paper_bridge are not yet wired
}

function modeLabel(mode: QFMode): string {
  switch (mode) {
    case "disabled":
      return "DISABLED";
    case "local_mock":
      return "LOCAL MOCK";
    case "runpod_research":
      return "RUNPOD RESEARCH";
    case "runpod_shadow":
      return "RUNPOD SHADOW";
    case "paper_bridge":
      return "PAPER BRIDGE";
  }
}

function modeDescription(mode: QFMode): string {
  switch (mode) {
    case "disabled":
      return "Quant Foundry is disabled by default (QUANT_FOUNDRY_ENABLED=false). No jobs are created or processed. This is the safe resting state — not a failure.";
    case "local_mock":
      return "Local mock mode: the full job loop (enqueue → dispatch → process) runs synchronously in-process. No external workers, no RunPod, no sig.predict writes. Safe for local dev.";
    case "runpod_research":
      return "RunPod research mode: training jobs are dispatched to RunPod workers. Not yet wired (Phase 5).";
    case "runpod_shadow":
      return "RunPod shadow mode: shadow inference against live features. Not yet wired (Phase 6).";
    case "paper_bridge":
      return "Paper bridge mode: paper-only model pointer bridge. Not yet wired (Phase 7).";
  }
}

// ---------------------------------------------------------------------------
// Module status derivation
// ---------------------------------------------------------------------------

interface ModuleStatus {
  id: string;
  label: string;
  description: string;
  icon: typeof Gauge;
  state: "active" | "configured" | "not_wired" | "disabled";
  intent: SemanticIntent;
  detail: string;
}

function deriveModuleStatuses(
  mode: QFMode,
  health: QuantFoundryHealthResponse | undefined,
): ModuleStatus[] {
  const enabled = mode !== "disabled";
  return MODULES.map((spec) => {
    let state: ModuleStatus["state"];
    let detail: string;

    if (!enabled) {
      state = "disabled";
      detail = "Gateway disabled — module is at rest.";
    } else if (
      spec.id === "runpod_research" ||
      spec.id === "shadow_inference"
    ) {
      // These modules are not yet wired in the current phase.
      state = "not_wired";
      detail = "Not yet wired in the current phase.";
    } else if (spec.id === "gateway") {
      state = "active";
      detail = `Mode: ${health?.mode ?? "local_mock"}. Shadow-only: ${health?.shadow_only ?? true}.`;
    } else if (spec.id === "outbox" || spec.id === "inbox") {
      state = "active";
      detail = `Job count: ${health?.job_count ?? 0}.`;
    } else {
      // feature_lake, settlement, dossier_registry, tournament
      // are Python-side modules that exist but are not yet exposed
      // through the gateway health endpoint.
      state = "configured";
      detail = "Module exists on the Python side; not yet reported in gateway health.";
    }

    const intent: SemanticIntent =
      state === "active"
        ? "verified"
        : state === "configured"
          ? "degraded"
          : state === "not_wired"
            ? "degraded"
            : "inactive";

    return { ...spec, state, intent, detail };
  });
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function QuantFoundryPage() {
  const token = useAuth((s) => s.token);

  const healthQ = useQuery({
    queryKey: ["quant-foundry", "health"],
    queryFn: () => api.quantFoundryHealth(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false, // don't retry — a 503 (disabled) is a valid state, not a transient error
  });

  const jobsQ = useQuery({
    queryKey: ["quant-foundry", "jobs"],
    queryFn: () => api.quantFoundryJobs(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });

  const mode = useMemo(() => classifyMode(healthQ.data), [healthQ.data]);
  const moduleStatuses = useMemo(
    () => deriveModuleStatuses(mode, healthQ.data),
    [mode, healthQ.data],
  );

  // A 503 (gateway not configured / disabled) is NOT an error — it's the
  // default safe state. Only surface real errors (network, auth, 5xx other
  // than 503).
  const healthError = healthQ.error instanceof UnavailableError
    ? null // 503 = disabled = valid
    : healthQ.error;

  const recentJobs = useMemo(() => {
    if (!jobsQ.data) return [];
    return [...jobsQ.data]
      .sort(
        (a, b) =>
          (b.created_at_ns ?? 0) - (a.created_at_ns ?? 0),
      )
      .slice(0, 5);
  }, [jobsQ.data]);

  return (
    <AppShell>
      <PageHeader
        title="Quant Foundry"
        description="Read-only overview of the Quant Foundry evidence loop: gateway, outbox, inbox, feature lake, settlement, dossiers, tournament, and shadow inference. No actions can promote or trade from this page."
        action={
          <div className="flex items-center gap-2">
            <StatusPill
              intent={modeToIntent(mode)}
              label={modeLabel(mode)}
            />
            {healthQ.data?.shadow_only !== false && (
              <Badge variant="outline" className="text-xs">
                SHADOW ONLY
              </Badge>
            )}
          </div>
        }
      />

      {/* Global mode banner */}
      <div className="mb-4 rounded-lg border border-border/40 bg-card/40 p-4">
        <div className="flex items-start gap-3">
          <Brain className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
          <div className="min-w-0">
            <p className="text-sm font-medium">{modeLabel(mode)}</p>
            <p className="mt-1 text-sm text-muted-foreground">
              {modeDescription(mode)}
            </p>
          </div>
        </div>
      </div>

      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {SUB_PAGES.map((page) => {
          const Icon = page.icon;
          return (
            <Link
              key={page.href}
              href={page.href}
              className="rounded-lg border border-border/40 bg-card/40 p-3 transition-colors hover:border-primary/50 hover:bg-card/70 focus:outline-none focus:ring-2 focus:ring-ring"
            >
              <div className="flex items-center gap-2 text-sm font-medium">
                <Icon className="h-4 w-4 text-primary" />
                {page.label}
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                {page.description}
              </p>
            </Link>
          );
        })}
      </div>

      {/* Error banner (only for real errors, not 503-disabled) */}
      {healthError && (
        <div className="mb-4 rounded-lg border border-short/30 bg-short/5 p-4">
          <p className="text-sm text-short">
            Unable to reach the Quant Foundry health endpoint. The API may be
            down or the route may not be registered.
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {healthError instanceof Error ? healthError.message : "Unknown error"}
          </p>
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
        {/* Left column: Module status cards */}
        <div className="space-y-4">
          <ModuleStatusCard modules={moduleStatuses} />
        </div>

        {/* Right column: Cost/budget + Jobs + Receipts */}
        <div className="space-y-4">
          <CostBudgetCard mode={mode} jobCount={healthQ.data?.job_count ?? 0} />
          <RecentJobsCard
            jobs={recentJobs}
            isLoading={jobsQ.isLoading}
            mode={mode}
          />
          <ReceiptsCard mode={mode} />
        </div>
      </div>
    </AppShell>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function ModuleStatusCard({ modules }: { modules: ModuleStatus[] }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Boxes className="h-4 w-4 text-primary" />
          Module status
        </CardTitle>
        <CardDescription>
          9 Quant Foundry modules. Disabled is the safe resting state — not a
          failure.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {modules.map((mod) => {
          const Icon = mod.icon;
          return (
            <div
              key={mod.id}
              className="flex items-start gap-3 rounded-md border border-border/30 bg-card/40 p-3"
            >
              <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{mod.label}</span>
                  <StatusPill
                    intent={mod.intent}
                    label={mod.state.toUpperCase().replace("_", " ")}
                    compact
                  />
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  {mod.detail}
                </p>
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

function CostBudgetCard({
  mode,
  jobCount,
}: {
  mode: QFMode;
  jobCount: number;
}) {
  // In local_mock mode, costs are zero (no external workers). In RunPod
  // modes, costs would come from the gateway — not yet wired.
  const hasCostData = mode !== "disabled" && mode !== "local_mock";

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <CircleDollarSign className="h-4 w-4 text-primary" />
          Cost &amp; budget
        </CardTitle>
        <CardDescription>
          Budget guard before heavy jobs. Zero cost in local mock mode.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">Mode</span>
          <span className="font-medium">{modeLabel(mode)}</span>
        </div>
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">Active jobs</span>
          <span className="font-medium">{jobCount}</span>
        </div>
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">Estimated spend</span>
          <span className="font-medium">
            {hasCostData ? "Not yet wired" : "$0.00"}
          </span>
        </div>
        {!hasCostData && (
          <p className="pt-2 text-xs text-muted-foreground">
            No external workers in this mode. RunPod cost tracking will be
            wired in Phase 5.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function RecentJobsCard({
  jobs,
  isLoading,
  mode,
}: {
  jobs: QuantFoundryJob[];
  isLoading: boolean;
  mode: QFMode;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Activity className="h-4 w-4 text-primary" />
          Recent jobs
        </CardTitle>
        <CardDescription>
          Latest 5 jobs from the outbox. Empty when disabled.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {mode === "disabled" ? (
          <p className="py-6 text-center text-xs text-muted-foreground">
            Gateway disabled — no jobs.
          </p>
        ) : isLoading ? (
          <p className="py-6 text-center text-xs text-muted-foreground">
            Loading jobs…
          </p>
        ) : jobs.length === 0 ? (
          <p className="py-6 text-center text-xs text-muted-foreground">
            No jobs yet. Create one via POST /quant-foundry/jobs.
          </p>
        ) : (
          <div className="space-y-2">
            {jobs.map((job) => (
              <div
                key={job.job_id}
                className="flex items-center justify-between rounded-md border border-border/30 bg-card/40 p-2 text-xs"
              >
                <div className="min-w-0">
                  <div className="truncate font-medium">{job.job_id}</div>
                  <div className="text-muted-foreground">{job.job_type}</div>
                </div>
                <Badge
                  variant={jobStatusVariant(job.status)}
                  className="text-xs"
                >
                  {job.status}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReceiptsCard({ mode }: { mode: QFMode }) {
  // Receipts are generated by the settlement ledger, dossier registry, and
  // tournament. In local_mock mode with no jobs, there are no receipts yet.
  // This card is a placeholder that will be wired to a receipts endpoint in
  // a later task.
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <ShieldCheck className="h-4 w-4 text-primary" />
          Latest receipts
        </CardTitle>
        <CardDescription>
          Evidence-loop receipts (settlement, dossier, tournament). Not yet
          exposed via a dedicated endpoint.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {mode === "disabled" ? (
          <p className="py-6 text-center text-xs text-muted-foreground">
            Gateway disabled — no receipts.
          </p>
        ) : (
          <p className="py-6 text-center text-xs text-muted-foreground">
            Receipts will appear here once the evidence-loop receipt endpoint
            is wired (Phase 4 completion).
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function jobStatusVariant(
  status: string,
): "long" | "warn" | "destructive" | "outline" {
  if (status === "completed") return "long";
  if (status === "running" || status === "queued") return "warn";
  if (status === "failed" || status === "rejected") return "destructive";
  return "outline";
}
