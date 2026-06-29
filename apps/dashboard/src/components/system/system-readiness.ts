/**
 * system-readiness — pure read-only analyzer for the Local Dev / Operator
 * Launch Experience page (/system).
 *
 * Aggregates:
 *   - API reachability (proved by the fact that any other query worked)
 *   - Service heartbeat table (from /services)
 *   - Kill-switch state (from /kill-switch)
 *   - OpenBB API status (from /research/openbb/health)
 *   - Receipt center coverage (from buildProofReceiptCenter)
 *   - Env var presence (names only, never values)
 *   - Copyable PowerShell commands
 *
 * Acceptance criteria:
 *   - New users see exactly what is running and what is missing.
 *   - No secrets are displayed.
 *   - Copyable commands use Windows PowerShell defaults.
 *   - Route smoke and proof status are visible.
 */

import type {
  KillSwitchState,
  OpenBBHealthResponse,
  ServicesResponse,
} from "@/lib/types";

import { buildProofReceiptCenter } from "@/components/receipts/proof-receipts";
import type { ProofReceiptCenterSummary } from "@/components/receipts/proof-receipts";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ReadinessState =
  | "ready"
  | "review"
  | "blocked"
  // New states from server readiness endpoint (TASK-0202)
  | "pass"
  | "warn"
  | "fail"
  | "skipped"
  | "disabled"
  | "stale";

export interface ReadinessCheck {
  id: string;
  label: string;
  state: ReadinessState;
  detail: string;
}

export interface EnvVarSpec {
  /** Variable name only — never expose the value */
  name: string;
  /** Optional human description */
  description: string;
  /** Whether this var is required for core operation vs optional */
  required: boolean;
  /** Whether the var is set (presence only — value is never read here) */
  present: boolean;
}

export interface CopyableCommand {
  id: string;
  label: string;
  description: string;
  /** PowerShell-formatted command */
  command: string;
  /** Whether this command is safe to run without confirmation */
  safe: boolean;
}

export interface ServiceHeartbeatRow {
  name: string;
  status: "up" | "stale" | "down" | "unknown";
  age_sec: number | null;
  expected: boolean;
}

export interface SystemReadinessPacket {
  /** Overall readiness state */
  state: ReadinessState;
  /** Readiness score 0-100 */
  score: number;
  /** Headline summary */
  headline: string;
  /** Individual readiness checks */
  checks: ReadinessCheck[];
  /** Service heartbeat rows */
  services: ServiceHeartbeatRow[];
  /** Service summary stats */
  serviceSummary: {
    up: number;
    stale: number;
    down: number;
    expected: number;
    total: number;
  };
  /** Env var presence list */
  envVars: EnvVarSpec[];
  /** Copyable PowerShell commands */
  commands: CopyableCommand[];
  /** Proof receipt center summary */
  receipts: {
    state: ReadinessState;
    total: number;
    dashboardExports: number;
    localScripts: number;
    liveScripts: number;
  };
  /** API connectivity state */
  api: {
    reachable: boolean;
    detail: string;
  };
  /** Kill-switch state */
  killSwitch: {
    state: "clear" | "engaged" | "unknown";
    detail: string;
  };
  /** OpenBB status */
  openbb: {
    state: "ok" | "degraded" | "down" | "unknown";
    detail: string;
  };
}

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

export interface SystemReadinessInput {
  /** /services response, or null if query failed */
  servicesData: ServicesResponse | null | undefined;
  /** Whether the services query errored (distinct from no-data) */
  servicesError: boolean;
  /** /kill-switch response, or null */
  killSwitch: KillSwitchState | null | undefined;
  /** /research/openbb/health response, or null */
  openbb: OpenBBHealthResponse | null | undefined;
  /** Dashboard NEXT_PUBLIC_API_URL (read at build/runtime) */
  apiUrl: string | null;
  /** Env var presence map — true if name is in process.env, false otherwise */
  envVarPresence: Record<string, boolean>;
}

// ---------------------------------------------------------------------------
// Required + optional env vars
// ---------------------------------------------------------------------------

export const REQUIRED_ENV_VARS: Array<{ name: string; description: string }> = [
  { name: "NEXT_PUBLIC_API_URL", description: "Fincept API base URL (default http://127.0.0.1:8010)" },
  { name: "REDIS_URL", description: "Redis Streams URL (default redis://127.0.0.1:6379)" },
];

export const OPTIONAL_ENV_VARS: Array<{ name: string; description: string }> = [
  { name: "FINCEPT_DB_URL", description: "Postgres/Timescale URL for audit/training metadata" },
  { name: "OPENBB_API_URL", description: "Local OpenBB API URL (default http://127.0.0.1:6900)" },
  { name: "OPENBB_PAT", description: "OpenBB personal access token (read-only research)" },
  { name: "ALPACA_KEY_ID", description: "Alpaca paper trading key ID" },
  { name: "ALPACA_SECRET_KEY", description: "Alpaca paper trading secret" },
  { name: "EXA_API_KEY", description: "Exa research API key" },
  { name: "FRED_API_KEY", description: "FRED macro data API key (for regime agent)" },
  { name: "GPT_API_KEY", description: "GPT-5.5 portfolio committee key" },
  { name: "CLAUDE_API_KEY", description: "Claude Opus 4.7 portfolio committee key" },
];

// ---------------------------------------------------------------------------
// PowerShell command catalog
// ---------------------------------------------------------------------------

export const POWERSHELL_COMMANDS: CopyableCommand[] = [
  {
    id: "start-all",
    label: "Start full stack",
    description: "Launch Redis, API, ingestor, features, agents, OMS, portfolio, jobs, strategy-host.",
    command: ".\\scripts\\start.ps1",
    safe: false,
  },
  {
    id: "status",
    label: "Service status",
    description: "Check heartbeat for all running services.",
    command: ".\\scripts\\status.ps1",
    safe: true,
  },
  {
    id: "stop-all",
    label: "Stop full stack",
    description: "Gracefully stop all running Fincept services.",
    command: ".\\scripts\\stop.ps1",
    safe: false,
  },
  {
    id: "start-feature",
    label: "Start single feature",
    description: "Start an individual feature without the full stack.",
    command: ".\\scripts\\start_feature.ps1 -Feature <feature_id>",
    safe: false,
  },
  {
    id: "stop-feature",
    label: "Stop single feature",
    description: "Stop a specific feature.",
    command: ".\\scripts\\stop_feature.ps1 -Feature <feature_id>",
    safe: false,
  },
  {
    id: "paper-spine",
    label: "Paper-spine replay (proof)",
    description: "Deterministic data->feature->signal->decision->risk->order->fill->portfolio receipt.",
    command: "uv run python scripts/paper_spine_replay.py",
    safe: true,
  },
  {
    id: "openbb-proof",
    label: "OpenBB live proof",
    description: "Generate live OpenBB readiness + quote + dispatcher receipt (requires OpenBB API running).",
    command: "uv run python scripts/openbb_live_proof.py",
    safe: true,
  },
  {
    id: "route-smoke",
    label: "Route smoke test",
    description: "Probe all API routes for reachability (requires API on port 8010).",
    command: "uv run python scripts/route_smoke.py",
    safe: true,
  },
  {
    id: "preflight",
    label: "Task check (lint + typecheck)",
    description: "Run repo-wide lint + typecheck before committing.",
    command: ".\\scripts\\task-check.ps1",
    safe: true,
  },
  {
    id: "dashboard-dev",
    label: "Dashboard dev server",
    description: "Start the Next.js dashboard on port 3000.",
    command: "pnpm --dir apps/dashboard dev",
    safe: true,
  },
  {
    id: "dashboard-typecheck",
    label: "Dashboard typecheck",
    description: "Run TypeScript compiler in noEmit mode.",
    command: "pnpm --dir apps/dashboard exec tsc --noEmit --pretty false",
    safe: true,
  },
  {
    id: "alembic-upgrade",
    label: "Run database migrations",
    description: "Apply Alembic migrations to FINCEPT_DB_URL (requires Postgres/Timescale).",
    command: "uv run alembic -c libs/fincept-db/alembic.ini upgrade head",
    safe: false,
  },
];

// ---------------------------------------------------------------------------
// Main analyzer
// ---------------------------------------------------------------------------

export function buildSystemReadinessPacket(input: SystemReadinessInput): SystemReadinessPacket {
  const checks: ReadinessCheck[] = [];

  // -------------------------------------------------------------------------
  // API reachability
  // -------------------------------------------------------------------------
  const apiReachable = input.servicesData !== null && input.servicesData !== undefined;
  const apiCheck: ReadinessCheck = {
    id: "api",
    label: "API reachable",
    state: apiReachable ? "ready" : "blocked",
    detail: apiReachable
      ? `API responded at ${input.apiUrl ?? "default URL"}.`
      : input.servicesError
        ? "API is not responding. Check that the server is running on the configured URL."
        : "API status unknown. Awaiting first response.",
  };
  checks.push(apiCheck);

  // -------------------------------------------------------------------------
  // Service heartbeat
  // -------------------------------------------------------------------------
  const services: ServiceHeartbeatRow[] = (input.servicesData?.services ?? []).map((s) => ({
    name: s.name,
    status: s.status,
    age_sec: s.age_sec,
    expected: s.expected,
  }));
  const expectedServices = services.filter((s) => s.expected);
  const up = expectedServices.filter((s) => s.status === "up").length;
  const stale = expectedServices.filter((s) => s.status === "stale").length;
  const down = expectedServices.filter((s) => s.status === "down").length;
  const serviceSummary = {
    up,
    stale,
    down,
    expected: expectedServices.length,
    total: services.length,
  };
  const serviceState: ReadinessState =
    expectedServices.length === 0
      ? "review"
      : down > 0
        ? "blocked"
        : stale > 0
          ? "review"
          : "ready";
  checks.push({
    id: "services",
    label: "Service heartbeat",
    state: serviceState,
    detail:
      expectedServices.length === 0
        ? "No expected services reported. Start the stack with scripts/start.ps1."
        : `${up} up, ${stale} stale, ${down} down of ${expectedServices.length} expected.`,
  });

  // -------------------------------------------------------------------------
  // Kill switch
  // -------------------------------------------------------------------------
  const killSwitchState: "clear" | "engaged" | "unknown" =
    input.killSwitch === null || input.killSwitch === undefined
      ? "unknown"
      : input.killSwitch.engaged
        ? "engaged"
        : "clear";
  checks.push({
    id: "kill-switch",
    label: "Kill switch",
    state: killSwitchState === "engaged" ? "blocked" : killSwitchState === "clear" ? "ready" : "review",
    detail:
      killSwitchState === "engaged"
        ? `Kill switch is ENGAGED. ${input.killSwitch?.reason ?? "No reason provided."} Clear via Risk page.`
        : killSwitchState === "clear"
          ? "Kill switch is clear. Paper trading flow allowed."
          : "Kill switch state unknown. API may be unreachable.",
  });

  // -------------------------------------------------------------------------
  // OpenBB
  // -------------------------------------------------------------------------
  const openbbState: "ok" | "degraded" | "down" | "unknown" =
    input.openbb === null || input.openbb === undefined
      ? "unknown"
      : input.openbb.ok
        ? "ok"
        : "down";
  checks.push({
    id: "openbb",
    label: "OpenBB API",
    state: openbbState === "ok" ? "ready" : openbbState === "unknown" ? "review" : "review",
    detail:
      openbbState === "ok"
        ? `OpenBB API reachable at ${input.openbb?.url ?? "default URL"}.`
        : openbbState === "down"
          ? `OpenBB API is not reachable. ${input.openbb?.error ?? "No error detail."}`
          : "OpenBB status not yet checked. Optional for paper trading.",
  });

  // -------------------------------------------------------------------------
  // Env vars
  // -------------------------------------------------------------------------
  const envVars: EnvVarSpec[] = [
    ...REQUIRED_ENV_VARS.map((v) => ({
      ...v,
      required: true,
      present: Boolean(input.envVarPresence[v.name]),
    })),
    ...OPTIONAL_ENV_VARS.map((v) => ({
      ...v,
      required: false,
      present: Boolean(input.envVarPresence[v.name]),
    })),
  ];
  const missingRequired = envVars.filter((v) => v.required && !v.present);
  checks.push({
    id: "env-vars",
    label: "Required env vars",
    state: missingRequired.length === 0 ? "ready" : "blocked",
    detail:
      missingRequired.length === 0
        ? `All ${REQUIRED_ENV_VARS.length} required env vars present (names only — values never read).`
        : `Missing required: ${missingRequired.map((v) => v.name).join(", ")}.`,
  });

  // -------------------------------------------------------------------------
  // Receipts
  // -------------------------------------------------------------------------
  const center: ProofReceiptCenterSummary = buildProofReceiptCenter();
  const receipts = {
    state: center.state,
    total: center.stats.total,
    dashboardExports: center.stats.dashboardExports,
    localScripts: center.stats.localScripts,
    liveScripts: center.stats.liveScripts,
  };
  checks.push({
    id: "receipts",
    label: "Proof receipt catalog",
    state: center.state,
    detail: `${center.stats.total} receipts in catalog (${center.stats.dashboardExports} dashboard exports, ${center.stats.localScripts} local scripts, ${center.stats.liveScripts} live scripts).`,
  });

  // -------------------------------------------------------------------------
  // Score + state aggregation
  // -------------------------------------------------------------------------
  const weights: Record<string, number> = {
    api: 25,
    services: 20,
    "kill-switch": 10,
    openbb: 10,
    "env-vars": 25,
    receipts: 10,
  };
  let score = 0;
  for (const check of checks) {
    const w = weights[check.id] ?? 0;
    if (check.state === "ready") score += w;
    else if (check.state === "review") score += w * 0.5;
  }
  const totalWeight = Object.values(weights).reduce((a, b) => a + b, 0);
  const normalized = Math.round((score / totalWeight) * 100);

  // Receipts is informational — its "review" state shouldn't downgrade an
  // otherwise-ready system, because the receipt catalog always includes live
  // scripts that require running infra.
  const runtimeChecks = checks.filter((c) => c.id !== "receipts");
  const blockedCount = runtimeChecks.filter((c) => c.state === "blocked").length;
  const reviewCount = runtimeChecks.filter((c) => c.state === "review").length;
  const overallState: ReadinessState =
    blockedCount > 0 ? "blocked" : reviewCount > 0 ? "review" : "ready";

  const headline =
    overallState === "ready"
      ? "All required checks pass. The stack is ready for paper trading."
      : overallState === "review"
        ? `${reviewCount} item(s) need attention. Stack is partially operational.`
        : `${blockedCount} blocker(s) prevent normal operation. See checks below.`;

  // -------------------------------------------------------------------------
  // Output
  // -------------------------------------------------------------------------
  return {
    state: overallState,
    score: normalized,
    headline,
    checks,
    services,
    serviceSummary,
    envVars,
    commands: POWERSHELL_COMMANDS,
    receipts,
    api: {
      reachable: apiReachable,
      detail: apiCheck.detail,
    },
    killSwitch: {
      state: killSwitchState,
      detail: checks.find((c) => c.id === "kill-switch")?.detail ?? "",
    },
    openbb: {
      state: openbbState,
      detail: checks.find((c) => c.id === "openbb")?.detail ?? "",
    },
  };
}
