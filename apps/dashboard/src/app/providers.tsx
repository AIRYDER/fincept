"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { useAuth } from "@/lib/auth";

/**
 * Root providers tree.  Hydrates the JWT token on mount so SSR/hydration
 * mismatch doesn't blow away the user's session, and configures
 * react-query with sane finance-dashboard defaults: short stale time,
 * aggressive refetch on focus, infinite retries on background refetch
 * (network blips during a trade are common).
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const hydrate = useAuth((s) => s.hydrate);
  useEffect(() => {
    hydrate();
  }, [hydrate]);

  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5_000,
            refetchOnWindowFocus: true,
            retry: (failureCount, error: unknown) => {
              // Don't retry on 401 - the user needs to log in.
              const status = (error as { status?: number } | null)?.status;
              if (status === 401) return false;
              return failureCount < 3;
            },
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
