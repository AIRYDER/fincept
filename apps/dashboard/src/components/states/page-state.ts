/**
 * page-state — shared state framework for consistent degraded-state UX.
 *
 * 8 states from the roadmap:
 *   1. Empty      — no data yet
 *   2. Loading    — skeleton / spinner
 *   3. Auth       — need login
 *   4. Provider   — external provider unavailable
 *   5. Stale      — data present but outdated
 *   6. Partial    — some data available, some missing
 *   7. Fatal      — API unreachable / hard error
 *   8. Demo       — using demo/sample data
 *
 * Acceptance criteria:
 *   - External provider failures are never shown as generic crashes.
 *   - Demo data is always labeled.
 *   - Stale data includes last timestamp and likely remediation.
 *   - Pages with partial data still render usable summaries.
 */

export type PageStateType =
  | "empty"
  | "loading"
  | "auth"
  | "provider"
  | "stale"
  | "partial"
  | "fatal"
  | "demo"
  | "ok";

export interface PageState {
  type: PageStateType;
  /** Human-readable label for the state */
  label: string;
  /** Longer description */
  description: string;
  /** Suggested remediation action */
  remediation: string | null;
  /** Provider name (for provider state) */
  provider?: string;
  /** Last known good timestamp (for stale state) */
  lastOkAt?: number | null;
  /** Which parts are missing (for partial state) */
  missingParts?: string[];
  /** Error message (for fatal state) */
  errorDetail?: string | null;
}

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

export function emptyState(description?: string): PageState {
  return {
    type: "empty",
    label: "No data",
    description: description ?? "No data available yet.",
    remediation: "Submit a query or wait for data to arrive.",
  };
}

export function loadingState(label?: string): PageState {
  return {
    type: "loading",
    label: "Loading",
    description: label ?? "Fetching data…",
    remediation: null,
  };
}

export function authState(): PageState {
  return {
    type: "auth",
    label: "Authentication required",
    description: "You must be logged in to view this data.",
    remediation: "Sign in to access this page.",
  };
}

export function providerState(provider: string, error?: string): PageState {
  return {
    type: "provider",
    label: "Provider unavailable",
    description: `${provider} is not responding or returned an error.`,
    remediation: `Check ${provider} connectivity and credentials. Try again in a few seconds.`,
    provider,
    errorDetail: error ?? null,
  };
}

export function staleState(lastOkAt: number | null, ageSec?: number): PageState {
  const ageLabel = ageSec !== undefined
    ? `Data is ${Math.round(ageSec)}s old`
    : "Data may be outdated";
  return {
    type: "stale",
    label: "Stale data",
    description: `${ageLabel}. Last confirmed fresh at ${lastOkAt ? new Date(lastOkAt * 1000).toISOString() : "unknown"}.`,
    remediation: "Check data source connectivity. Refresh or wait for next poll cycle.",
    lastOkAt,
  };
}

export function partialState(available: string[], missing: string[]): PageState {
  return {
    type: "partial",
    label: "Partial data",
    description: `Available: ${available.join(", ")}. Missing: ${missing.join(", ")}.`,
    remediation: missing.length > 0 ? `Check connectivity for: ${missing.join(", ")}` : null,
    missingParts: missing,
  };
}

export function fatalState(errorDetail?: string): PageState {
  return {
    type: "fatal",
    label: "API unreachable",
    description: "The backend API is not responding. This is not a provider issue.",
    remediation: "Check that the API server is running. Verify the API URL in settings.",
    errorDetail: errorDetail ?? null,
  };
}

export function demoState(provider?: string): PageState {
  return {
    type: "demo",
    label: "Demo data",
    description: `Showing sample data${provider ? ` from ${provider}` : ""}. This is not live production data.`,
    remediation: "Connect a real data source to see live data.",
    provider,
  };
}

export function okState(): PageState {
  return {
    type: "ok",
    label: "OK",
    description: "Data is live and fresh.",
    remediation: null,
  };
}

// ---------------------------------------------------------------------------
// React Query → PageState mapper
// ---------------------------------------------------------------------------

export interface QueryStateInput {
  isLoading: boolean;
  isError: boolean;
  error?: Error | null;
  data: unknown;
  /** Is this a demo/sample response? */
  isDemo?: boolean;
  /** Data age in seconds (for stale detection) */
  dataAgeSec?: number | null;
  /** Stale threshold in seconds */
  staleAfterSec?: number;
  /** Provider name (for provider-specific error detection) */
  provider?: string;
  /** Whether auth is missing */
  noAuth?: boolean;
  /** Which sub-queries are present vs missing (for partial) */
  partial?: { available: string[]; missing: string[] };
}

/**
 * Maps a React Query result to a PageState.
 * Returns "ok" when data is present and fresh.
 */
export function queryToPageState(input: QueryStateInput): PageState {
  // Auth gate
  if (input.noAuth) return authState();

  // Loading
  if (input.isLoading && input.data === undefined) return loadingState();

  // Fatal API error
  if (input.isError) {
    const msg = input.error?.message ?? "Unknown error";
    // Distinguish provider errors from fatal API errors
    if (input.provider) return providerState(input.provider, msg);
    return fatalState(msg);
  }

  // No data at all
  if (input.data === null || input.data === undefined || input.data === "" || (Array.isArray(input.data) && input.data.length === 0)) {
    return emptyState();
  }

  // Demo data
  if (input.isDemo) return demoState(input.provider);

  // Stale data
  if (input.dataAgeSec !== null && input.dataAgeSec !== undefined && input.staleAfterSec) {
    if (input.dataAgeSec > input.staleAfterSec) {
      return staleState(null, input.dataAgeSec);
    }
  }

  // Partial data
  if (input.partial && input.partial.missing.length > 0) {
    return partialState(input.partial.available, input.partial.missing);
  }

  return okState();
}
