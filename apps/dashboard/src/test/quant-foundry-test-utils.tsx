/**
 * Shared test utilities for Quant Foundry page tests.
 *
 * These helpers wrap page components (which use @tanstack/react-query
 * and @/lib/auth hooks) with a QueryClientProvider and pre-populate
 * the query cache so renderToStaticMarkup can render controlled
 * states (loading, disabled, error, empty, populated) without
 * actually hitting the API.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { useAuth } from "@/lib/auth";
import { UnavailableError } from "@/lib/api";

/** Set the auth token so useAuth returns a non-null token (queries enabled). */
export function setAuthToken(token: string | null = "test-token"): void {
  useAuth.setState({ token });
}

/** Create a QueryClient with retry disabled and no refetch. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchInterval: false,
        staleTime: Infinity,
        gcTime: 0,
      },
    },
  });
}

/**
 * Pre-populate the query cache with data for the given query key.
 * The query will be in "success" status with isLoading = false.
 */
export function setQueryData<TData>(
  queryClient: QueryClient,
  key: readonly unknown[],
  data: TData,
): void {
  queryClient.setQueryData(key, data);
}

/**
 * Pre-populate the query cache with a loading state for the given key.
 * The query will be in "pending" status with fetchStatus "fetching",
 * so isLoading = isPending && isFetching = true.
 */
export function setQueryLoading(
  queryClient: QueryClient,
  key: readonly unknown[],
): void {
  // Use a placeholder to ensure the query entry is created.
  queryClient.setQueryData(key, null);
  const query = queryClient.getQueryCache().find({ queryKey: [...key] });
  if (query) {
    query.setState({
      status: "pending",
      fetchStatus: "fetching",
      data: undefined,
      error: null,
    } as never);
  }
}

/**
 * Pre-populate the query cache with an error for the given query key.
 * The query will be in "error" status with isLoading = false.
 */
export function setQueryError(
  queryClient: QueryClient,
  key: readonly unknown[],
  error: Error,
): void {
  // Use a placeholder to ensure the query entry is created.
  queryClient.setQueryData(key, null);
  const query = queryClient.getQueryCache().find({ queryKey: [...key] });
  if (query) {
    query.setState({
      status: "error",
      error,
      data: undefined,
      fetchStatus: "idle",
    } as never);
  }
}

/** Create an UnavailableError with status 503 (disabled state). */
export function createUnavailableError(): UnavailableError {
  return new UnavailableError(503, { detail: "Quant Foundry is disabled" });
}

/** Create a generic Error (error state). */
export function createGenericError(message = "Network error"): Error {
  return new Error(message);
}

/**
 * Render a page component wrapped in QueryClientProvider.
 * Returns the static HTML string.
 */
export function renderPage(page: React.ReactElement): string {
  const queryClient = createQueryClient();
  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>{page}</QueryClientProvider>,
  );
}

/**
 * Render a page component with a specific QueryClient (for pre-populated
 * cache states).  Returns the static HTML string.
 */
export function renderPageWithClient(
  queryClient: QueryClient,
  page: React.ReactElement,
): string {
  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>{page}</QueryClientProvider>,
  );
}
