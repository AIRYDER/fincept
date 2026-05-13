"use client";

import { type UseQueryResult } from "@tanstack/react-query";
import { useMemo } from "react";

import { useAuth } from "@/lib/auth";

import {
  type PageState,
  queryToPageState,
  type QueryStateInput,
} from "./page-state";

/**
 * useQueryPageState — maps a React Query result to a PageState.
 *
 * Usage:
 *   const positionsQ = useQuery({ ... });
 *   const pageState = useQueryPageState(positionsQ);
 *   // Then: <PageStatePanel state={pageState} />
 */
export function useQueryPageState(
  query: UseQueryResult<unknown, Error>,
  options?: Partial<QueryStateInput>,
): PageState {
  const token = useAuth((s) => s.token);

  return useMemo(() => {
    return queryToPageState({
      isLoading: query.isLoading,
      isError: query.isError,
      error: query.error,
      data: query.data,
      noAuth: !token,
      ...options,
    });
  }, [query.isLoading, query.isError, query.error, query.data, token, options]);
}

/**
 * useMultiQueryPageState — maps multiple queries to a single PageState.
 * Handles partial data when some queries succeed and others fail.
 */
export function useMultiQueryPageState(
  queries: Array<{ name: string; query: UseQueryResult<unknown, Error> }>,
  options?: Partial<QueryStateInput>,
): PageState {
  const token = useAuth((s) => s.token);

  return useMemo(() => {
    if (!token) return { type: "auth", label: "Authentication required", description: "You must be logged in.", remediation: "Sign in." };

    const loading = queries.some((q) => q.query.isLoading && q.query.data === undefined);
    if (loading) return { type: "loading", label: "Loading", description: "Fetching data…", remediation: null };

    const errors = queries.filter((q) => q.query.isError);
    if (errors.length === queries.length) {
      // All failed — fatal or provider
      const first = errors[0];
      if (options?.provider) return { type: "provider", label: "Provider unavailable", description: `${options.provider} is not responding.`, remediation: `Check ${options.provider}.`, provider: options.provider, errorDetail: first.query.error?.message ?? null };
      return { type: "fatal", label: "API unreachable", description: "All data sources failed.", remediation: "Check API server.", errorDetail: first.query.error?.message ?? null };
    }

    if (errors.length > 0) {
      // Some failed — partial
      const available = queries.filter((q) => !q.query.isError).map((q) => q.name);
      const missing = errors.map((q) => q.name);
      return { type: "partial", label: "Partial data", description: `Available: ${available.join(", ")}. Missing: ${missing.join(", ")}.`, remediation: `Check: ${missing.join(", ")}`, missingParts: missing };
    }

    // All succeeded — check demo/stale
    if (options?.isDemo) return { type: "demo", label: "Demo data", description: "Showing sample data.", remediation: "Connect real data source.", provider: options.provider };

    if (options?.dataAgeSec !== null && options?.dataAgeSec !== undefined && options?.staleAfterSec && options.dataAgeSec > options.staleAfterSec) {
      return { type: "stale", label: "Stale data", description: `Data is ${Math.round(options.dataAgeSec)}s old.`, remediation: "Refresh or wait for next poll.", lastOkAt: null };
    }

    return { type: "ok", label: "OK", description: "Data is live and fresh.", remediation: null };
  }, [queries, token, options]);
}
