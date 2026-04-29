/**
 * Typed REST client for services/api FastAPI app.
 *
 * Every method matches a route in:
 *   services/api/src/api/routes/{data,positions,orders,strategies,control}.py
 *
 * Errors throw an ``ApiError`` carrying status + body so panels can
 * distinguish "not authenticated" (401 -> redirect to /login) from
 * "endpoint unavailable" (5xx -> show inline error toast).
 */

import type {
  Bar,
  OrderRecord,
  Position,
  StrategyRow,
  UniverseRow,
} from "@/lib/types";

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `API error ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  token: string | null,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });
  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, body);
  return body as T;
}

export const api = {
  // --- public --------------------------------------------------------------
  health: (token: string | null = null) =>
    request<{ ok: boolean; version: string }>("/health", token),

  // --- data ---------------------------------------------------------------
  universe: (token: string | null, params?: { asset_class?: string }) => {
    const q = new URLSearchParams();
    if (params?.asset_class) q.set("asset_class", params.asset_class);
    return request<UniverseRow[]>(
      `/data/universe${q.size ? `?${q}` : ""}`,
      token,
    );
  },
  bars: (
    token: string | null,
    symbol: string,
    args: { start: number; end: number; freq?: string; venue?: string },
  ) => {
    const q = new URLSearchParams();
    q.set("start", String(args.start));
    q.set("end", String(args.end));
    if (args.freq) q.set("freq", args.freq);
    if (args.venue) q.set("venue", args.venue);
    return request<Bar[]>(`/data/bars/${symbol}?${q}`, token);
  },

  // --- positions ----------------------------------------------------------
  positions: (token: string | null, includeFlat = false) =>
    request<Position[]>(
      `/positions${includeFlat ? "?include_flat=true" : ""}`,
      token,
    ),
  strategyPositions: (
    token: string | null,
    strategyId: string,
    includeFlat = false,
  ) =>
    request<Position[]>(
      `/positions/${encodeURIComponent(strategyId)}${
        includeFlat ? "?include_flat=true" : ""
      }`,
      token,
    ),

  // --- orders -------------------------------------------------------------
  orders: (
    token: string | null,
    args?: { strategy_id?: string; status?: string; limit?: number },
  ) => {
    const q = new URLSearchParams();
    if (args?.strategy_id) q.set("strategy_id", args.strategy_id);
    if (args?.status) q.set("status", args.status);
    if (args?.limit) q.set("limit", String(args.limit));
    return request<OrderRecord[]>(
      `/orders${q.size ? `?${q}` : ""}`,
      token,
    );
  },

  // --- strategies ---------------------------------------------------------
  strategies: (token: string | null) =>
    request<StrategyRow[]>("/strategies", token),

  // --- control ------------------------------------------------------------
  tripKillSwitch: (token: string | null, reason: string) =>
    request<{ ok: boolean; alert_id: string }>("/kill-switch", token, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  clearKillSwitch: (token: string | null) =>
    request<{ ok: boolean; alert_id: string }>("/kill-switch", token, {
      method: "DELETE",
    }),
};
