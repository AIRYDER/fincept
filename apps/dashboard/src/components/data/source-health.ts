import type {
  DataCoverageResponse,
  DataSourceDefinition,
  DataSourcesResponse,
  OpenBBHealthResponse,
  ProviderDataResponse,
  ServicesResponse,
} from "@/lib/types";

export type SourceHealthState = "ready" | "review" | "blocked";
export type SourceHealthSeverity = "pass" | "watch" | "fail";

export interface SourceHealthCheck {
  id: string;
  label: string;
  severity: SourceHealthSeverity;
  detail: string;
}

export interface SourceHealthSummary {
  state: SourceHealthState;
  score: number;
  headline: string;
  checks: SourceHealthCheck[];
  actions: string[];
  registryRows: Array<{
    id: string;
    name: string;
    category: string;
    safety: string;
    healthMode: string;
    callSurfaceCount: number;
    dataCount: number;
  }>;
  captureDetail: string;
  captureRows: Array<{
    id: string;
    provider: string;
    dataset: string;
    symbol: string | null;
    endpoint: string;
    rowCount: number;
    ok: boolean;
    errorType: string | null;
  }>;
}

export function buildSourceHealthSummary({
  sources,
  coverage,
  openbb,
  providerData,
  services,
}: {
  sources?: DataSourcesResponse | null;
  coverage?: DataCoverageResponse | null;
  openbb?: OpenBBHealthResponse | null;
  providerData?: ProviderDataResponse | null;
  services?: ServicesResponse | null;
}): SourceHealthSummary {
  const checks = buildChecks({ sources, coverage, openbb, providerData, services });
  const failed = checks.filter((check) => check.severity === "fail").length;
  const watches = checks.filter((check) => check.severity === "watch").length;
  const state: SourceHealthState = failed > 0 ? "blocked" : watches > 0 ? "review" : "ready";
  const score = clamp(100 - failed * 25 - watches * 9, 0, 100);

  return {
    state,
    score,
    headline: headlineFor(state, failed, watches),
    checks,
    actions: buildActions(state, checks),
    registryRows: buildRegistryRows(sources?.sources ?? []),
    captureDetail: providerCaptureDetail(providerData),
    captureRows: buildCaptureRows(providerData),
  };
}

function buildChecks({
  sources,
  coverage,
  openbb,
  providerData,
  services,
}: {
  sources?: DataSourcesResponse | null;
  coverage?: DataCoverageResponse | null;
  openbb?: OpenBBHealthResponse | null;
  providerData?: ProviderDataResponse | null;
  services?: ServicesResponse | null;
}): SourceHealthCheck[] {
  const expectedServices = services?.services.filter((service) => service.expected) ?? [];
  const downServices = expectedServices.filter((service) => service.status === "down");
  const staleServices = expectedServices.filter((service) => service.status === "stale");
  const coverageSummary = coverage?.summary ?? null;

  return [
    {
      id: "source-registry",
      label: "Source registry",
      severity: sources && sources.sources.length > 0 ? "pass" : "fail",
      detail: sources && sources.sources.length > 0 ? `${sources.sources.length} source definition(s) registered.` : "No source registry rows returned.",
    },
    {
      id: "coverage",
      label: "Bar coverage",
      severity: coverageSeverity(coverage),
      detail: coverageSummary
        ? `${coverageSummary.ok}/${coverageSummary.total} symbols ok · ${Math.round(coverageSummary.coverage_pct)}% coverage · ${coverageSummary.stale} stale · ${coverageSummary.empty} empty · ${coverageSummary.error} errors.`
        : "No coverage summary returned.",
    },
    {
      id: "openbb",
      label: "OpenBB probe",
      severity: openbb?.ok ? (openbb.warning ? "watch" : "pass") : "watch",
      detail: openbb?.ok
        ? `OpenBB reachable at ${openbb.url}${openbb.latency_ms != null ? ` · ${Math.round(openbb.latency_ms)}ms` : ""}${openbb.warning ? ` · ${openbb.warning}` : ""}.`
        : openbb?.error ?? openbb?.error_type ?? "OpenBB health probe unavailable.",
    },
    {
      id: "provider-capture",
      label: "Provider capture",
      severity: providerCaptureSeverity(providerData),
      detail: providerCaptureDetail(providerData),
    },
    {
      id: "services",
      label: "Core services",
      severity: downServices.length > 0 ? "fail" : staleServices.length > 0 ? "watch" : services ? "pass" : "watch",
      detail: services
        ? `${services.summary.up}/${services.summary.expected} expected services up · ${downServices.length} down · ${staleServices.length} stale.`
        : "Service heartbeat summary unavailable.",
    },
    {
      id: "safety-mix",
      label: "Safety mix",
      severity: safetyMixSeverity(sources?.sources ?? []),
      detail: safetyMixDetail(sources?.sources ?? []),
    },
  ];
}

function coverageSeverity(coverage?: DataCoverageResponse | null): SourceHealthSeverity {
  if (!coverage || coverage.summary.total === 0) return "fail";
  if (coverage.summary.error > 0 || coverage.summary.coverage_pct < 50) return "fail";
  if (coverage.summary.stale > 0 || coverage.summary.empty > 0 || coverage.summary.coverage_pct < 90) return "watch";
  return "pass";
}

function providerCaptureSeverity(providerData?: ProviderDataResponse | null): SourceHealthSeverity {
  if (!providerData) return "watch";
  if (!providerData.capture_enabled) return "watch";
  if (!providerData.ok) return "fail";
  if (providerData.summary.total_records === 0) return "watch";
  if (providerData.summary.error_records > 0) return "watch";
  return "pass";
}

function providerCaptureDetail(providerData?: ProviderDataResponse | null): string {
  if (!providerData) return "Provider capture ledger unavailable.";
  if (!providerData.capture_enabled) {
    return `Provider capture disabled: ${providerData.error_type ?? providerData.error ?? "database not configured"}.`;
  }
  if (!providerData.ok) {
    return `Provider capture read failed: ${providerData.error_type ?? providerData.error ?? "unknown error"}.`;
  }
  const summary = providerData.summary;
  if (summary.total_records === 0) return "Provider capture ledger is reachable but empty.";
  const providers = Object.entries(summary.providers)
    .map(([provider, count]) => `${provider}:${count}`)
    .join(", ");
  return `${summary.total_records} captured provider record(s) · ${summary.ok_records} ok · ${summary.error_records} errors${providers ? ` · ${providers}` : ""}.`;
}

function safetyMixSeverity(sources: DataSourceDefinition[]): SourceHealthSeverity {
  if (sources.length === 0) return "fail";
  return sources.some((source) => source.safety === "paper_first" || source.safety === "internal_state") ? "watch" : "pass";
}

function safetyMixDetail(sources: DataSourceDefinition[]): string {
  if (sources.length === 0) return "No registered source safety metadata.";
  const readOnly = sources.filter((source) => source.safety === "read_only" || source.safety === "experimental_read_only").length;
  const guarded = sources.length - readOnly;
  return `${readOnly} read-only source(s), ${guarded} guarded state/paper source(s).`;
}

function buildRegistryRows(sources: DataSourceDefinition[]): SourceHealthSummary["registryRows"] {
  return sources.map((source) => ({
    id: source.id,
    name: source.name,
    category: source.category,
    safety: source.safety,
    healthMode: source.health.mode,
    callSurfaceCount: source.call_surfaces.length,
    dataCount: source.data.length,
  }));
}

function buildCaptureRows(providerData?: ProviderDataResponse | null): SourceHealthSummary["captureRows"] {
  return (providerData?.records ?? []).slice(0, 6).map((record) => ({
    id: record.record_id,
    provider: record.provider,
    dataset: record.dataset,
    symbol: record.symbol,
    endpoint: record.endpoint,
    rowCount: record.row_count,
    ok: record.ok,
    errorType: record.error_type,
  }));
}

function buildActions(state: SourceHealthState, checks: SourceHealthCheck[]): string[] {
  if (state === "ready") return ["Source registry, coverage, OpenBB, and expected services are ready for operator review."];
  return checks
    .filter((check) => check.severity !== "pass")
    .map((check) => `${check.label}: ${check.detail}`)
    .slice(0, 5);
}

function headlineFor(state: SourceHealthState, failed: number, watches: number): string {
  if (state === "blocked") return `${failed} source health blocker${failed === 1 ? "" : "s"} require attention.`;
  if (state === "review") return `${watches} source watch item${watches === 1 ? "" : "s"}; verify before relying on data products.`;
  return "Source health and data coverage are ready.";
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
