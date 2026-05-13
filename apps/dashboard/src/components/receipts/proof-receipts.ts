export type ProofReceiptChannel = "dashboard_export" | "local_script" | "live_script";
export type ProofReceiptRuntime = "browser" | "local_python" | "live_stack";
export type ProofReceiptSeverity = "pass" | "watch" | "fail";
export type ProofReceiptState = "ready" | "review" | "blocked";

export interface ProofReceiptDefinition {
  id: string;
  title: string;
  description: string;
  channel: ProofReceiptChannel;
  runtime: ProofReceiptRuntime;
  producer: string;
  route?: string;
  command?: string;
  reportPath?: string;
  schema: string;
  scope: string[];
  liveDependencies: string[];
}

export interface ProofReceiptCheck {
  id: string;
  label: string;
  severity: ProofReceiptSeverity;
  detail: string;
}

export interface ProofReceiptCenterSummary {
  state: ProofReceiptState;
  score: number;
  headline: string;
  checks: ProofReceiptCheck[];
  actions: string[];
  receipts: ProofReceiptDefinition[];
  stats: {
    total: number;
    dashboardExports: number;
    localScripts: number;
    liveScripts: number;
    liveDependencyCount: number;
  };
}

export const PROOF_RECEIPTS: ProofReceiptDefinition[] = [
  {
    id: "portfolio-cockpit",
    title: "Portfolio cockpit receipt",
    description: "Deterministic optimizer readiness packet exported from the portfolio builder cockpit.",
    channel: "dashboard_export",
    runtime: "browser",
    producer: "PortfolioOptimizerCockpit",
    route: "/portfolio-builder",
    schema: "fincept.portfolio_cockpit_receipt.v1",
    scope: ["optimizer readiness", "budget rails", "operator actions", "audit payload"],
    liveDependencies: [],
  },
  {
    id: "scenario-war-room",
    title: "Scenario war room receipt",
    description: "Stress-regime receipt exported from the portfolio builder scenario war room.",
    channel: "dashboard_export",
    runtime: "browser",
    producer: "ScenarioWarRoomPanel",
    route: "/portfolio-builder",
    schema: "scenario_war_room_receipt",
    scope: ["stress regime", "portfolio impact", "constraints", "warnings"],
    liveDependencies: [],
  },
  {
    id: "paper-spine-replay",
    title: "Paper spine replay receipt",
    description: "Deterministic local proof of data → feature → signal → decision → risk → order → fill → portfolio audit flow.",
    channel: "local_script",
    runtime: "local_python",
    producer: "scripts/paper_spine_replay.py",
    command: "uv run python scripts/paper_spine_replay.py",
    reportPath: "reports/paper-spine/latest.json",
    schema: "paper_spine_replay.schema_version_1",
    scope: ["paper spine", "risk approved path", "risk rejected path", "audit trail"],
    liveDependencies: [],
  },
  {
    id: "openbb-live-proof",
    title: "OpenBB live proof receipt",
    description: "Live OpenBB route proof across health, readiness, quote, and dispatcher endpoints.",
    channel: "live_script",
    runtime: "live_stack",
    producer: "scripts/openbb_live_proof.py",
    command: "uv run python scripts/openbb_live_proof.py --symbol NVDA",
    reportPath: "reports/openbb-live/openbb-live-*.json",
    schema: "openbb_live_proof.schema_version_1",
    scope: ["OpenBB API health", "provider readiness", "quote", "dispatcher"],
    liveDependencies: ["Fincept API on port 8010", "OpenBB API on 127.0.0.1:6900"],
  },
  {
    id: "route-smoke",
    title: "Route smoke receipt",
    description: "Live Fincept API route smoke receipt with latency and status-code evidence.",
    channel: "live_script",
    runtime: "live_stack",
    producer: "scripts/route_smoke.py",
    command: "uv run python scripts/route_smoke.py",
    reportPath: "reports/route-smoke/route-smoke-*.json",
    schema: "route_smoke.schema_version_1",
    scope: ["API route availability", "auth path", "latency", "status checks"],
    liveDependencies: ["Fincept API on port 8010"],
  },
];

export function buildProofReceiptCenter(
  receipts: ProofReceiptDefinition[] = PROOF_RECEIPTS,
): ProofReceiptCenterSummary {
  const checks = buildChecks(receipts);
  const failed = checks.filter((check) => check.severity === "fail").length;
  const watches = checks.filter((check) => check.severity === "watch").length;
  const state: ProofReceiptState = failed > 0 ? "blocked" : watches > 0 ? "review" : "ready";
  const stats = buildStats(receipts);

  return {
    state,
    score: clamp(100 - failed * 25 - watches * 8, 0, 100),
    headline: headlineFor(state, failed, watches),
    checks,
    actions: buildActions(receipts, checks),
    receipts,
    stats,
  };
}

function buildChecks(receipts: ProofReceiptDefinition[]): ProofReceiptCheck[] {
  const missingSchema = receipts.filter((receipt) => !receipt.schema.trim());
  const missingProducer = receipts.filter((receipt) => !receipt.producer.trim());
  const liveReceipts = receipts.filter((receipt) => receipt.runtime === "live_stack");
  const dashboardExports = receipts.filter((receipt) => receipt.channel === "dashboard_export");
  const scriptReceipts = receipts.filter((receipt) => receipt.channel !== "dashboard_export");

  return [
    {
      id: "catalog",
      label: "Receipt catalog",
      severity: receipts.length > 0 ? "pass" : "fail",
      detail: receipts.length > 0 ? `${receipts.length} receipt producer(s) cataloged.` : "No receipt producers cataloged.",
    },
    {
      id: "schemas",
      label: "Schema labels",
      severity: missingSchema.length === 0 ? "pass" : "fail",
      detail: missingSchema.length === 0 ? "Every receipt has a schema or kind label." : `${missingSchema.length} receipt(s) missing schema labels.`,
    },
    {
      id: "producers",
      label: "Producer links",
      severity: missingProducer.length === 0 ? "pass" : "fail",
      detail: missingProducer.length === 0 ? "Every receipt maps to a dashboard component or script producer." : `${missingProducer.length} receipt(s) missing producer metadata.`,
    },
    {
      id: "dashboard-exports",
      label: "Dashboard exports",
      severity: receipts.length > 0 ? "pass" : "fail",
      detail: `${dashboardExports.length} browser-export receipt(s) available from dashboard review flows.`,
    },
    {
      id: "script-proofs",
      label: "Script proofs",
      severity: receipts.length > 0 ? "pass" : "fail",
      detail: `${scriptReceipts.length} script-generated receipt(s) available for local/live verification.`,
    },
    {
      id: "live-boundary",
      label: "Live boundary",
      severity: liveReceipts.length > 0 ? "watch" : "pass",
      detail: liveReceipts.length > 0 ? `${liveReceipts.length} receipt(s) require a running local/live stack.` : "All receipts are offline/browser-only.",
    },
  ];
}

function buildStats(receipts: ProofReceiptDefinition[]): ProofReceiptCenterSummary["stats"] {
  const dashboardExports = receipts.filter((receipt) => receipt.channel === "dashboard_export").length;
  const localScripts = receipts.filter((receipt) => receipt.channel === "local_script").length;
  const liveScripts = receipts.filter((receipt) => receipt.channel === "live_script").length;
  const liveDependencyCount = receipts.reduce((count, receipt) => count + receipt.liveDependencies.length, 0);
  return {
    total: receipts.length,
    dashboardExports,
    localScripts,
    liveScripts,
    liveDependencyCount,
  };
}

function buildActions(receipts: ProofReceiptDefinition[], checks: ProofReceiptCheck[]): string[] {
  const failing = checks.filter((check) => check.severity === "fail");
  if (failing.length > 0) return failing.map((check) => `${check.label}: ${check.detail}`);
  const commands = receipts.filter((receipt) => receipt.command).map((receipt) => `${receipt.title}: ${receipt.command}`);
  return [
    "Use dashboard export buttons for browser-generated review receipts.",
    ...commands,
  ].slice(0, 6);
}

function headlineFor(state: ProofReceiptState, failed: number, watches: number): string {
  if (state === "blocked") return `${failed} proof receipt catalog blocker${failed === 1 ? "" : "s"} require attention.`;
  if (state === "review") return `${watches} proof receipt watch item${watches === 1 ? "" : "s"}; live proofs require local services.`;
  return "Proof receipt catalog is ready.";
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
