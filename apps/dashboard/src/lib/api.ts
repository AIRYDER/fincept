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
  BacktestRunDetailResponse,
  BacktestRunRequest,
  BacktestRunResponse,
  BacktestRunsListResponse,
  BacktestStrategiesResponse,
  Bar,
  ClearShadowResponse,
  FeatureImportanceResponse,
  ModelRecord,
  ModelsResponse,
  NewsResponse,
  OrderRecord,
  Position,
  PredictionsResponse,
  PredictionStatsResponse,
  PromoteResponse,
  PromotionStateResponse,
  RegimeResponse,
  RollbackResponse,
  ServicesResponse,
  ShadowResponse,
  StrategyRow,
  TrainModelBody,
  TrainingRun,
  TrainingRunsResponse,
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

  // --- news ---------------------------------------------------------------
  news: (
    token: string | null,
    args?: { limit?: number; only_book?: boolean },
  ) => {
    const q = new URLSearchParams();
    if (args?.limit) q.set("limit", String(args.limit));
    if (args?.only_book) q.set("only_book", "true");
    return request<NewsResponse>(
      `/news${q.size ? `?${q}` : ""}`,
      token,
    );
  },

  // --- services -----------------------------------------------------------
  services: (token: string | null) =>
    request<ServicesResponse>("/services", token),

  // --- models -------------------------------------------------------------
  models: (token: string | null) => request<ModelsResponse>("/models", token),
  modelDetail: (token: string | null, name: string) =>
    request<ModelRecord>(`/models/${encodeURIComponent(name)}`, token),
  modelFeatureImportance: (token: string | null, name: string) =>
    request<FeatureImportanceResponse>(
      `/models/${encodeURIComponent(name)}/feature-importance`,
      token,
    ),
  trainModel: (token: string | null, body: TrainModelBody) =>
    request<TrainingRun>("/models/train", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  modelRuns: (
    token: string | null,
    args?: { status?: string; limit?: number },
  ) => {
    const q = new URLSearchParams();
    if (args?.status) q.set("status", args.status);
    if (args?.limit) q.set("limit", String(args.limit));
    return request<TrainingRunsResponse>(
      `/models/runs${q.size ? `?${q}` : ""}`,
      token,
    );
  },
  modelRunDetail: (token: string | null, runId: string) =>
    request<TrainingRun>(
      `/models/runs/${encodeURIComponent(runId)}`,
      token,
    ),
  modelPromotionState: (
    token: string | null,
    args?: { agent_id?: string; history_limit?: number },
  ) => {
    const q = new URLSearchParams();
    if (args?.agent_id) q.set("agent_id", args.agent_id);
    if (args?.history_limit) q.set("history_limit", String(args.history_limit));
    return request<PromotionStateResponse>(
      `/models/promote/active${q.size ? `?${q}` : ""}`,
      token,
    );
  },
  promoteModel: (
    token: string | null,
    name: string,
    body: { agent_id?: string; promoted_by?: string } = {},
  ) =>
    request<PromoteResponse>(
      `/models/${encodeURIComponent(name)}/promote`,
      token,
      { method: "POST", body: JSON.stringify(body) },
    ),
  rollbackPromotion: (
    token: string | null,
    body: { agent_id?: string; promoted_by?: string } = {},
  ) =>
    request<RollbackResponse>("/models/promote/rollback", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  setShadow: (
    token: string | null,
    name: string,
    body: { agent_id?: string; promoted_by?: string } = {},
  ) =>
    request<ShadowResponse>(
      `/models/${encodeURIComponent(name)}/shadow`,
      token,
      { method: "POST", body: JSON.stringify(body) },
    ),
  clearShadow: (
    token: string | null,
    body: { agent_id?: string; promoted_by?: string } = {},
  ) =>
    request<ClearShadowResponse>("/models/promote/shadow/clear", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  modelPredictions: (
    token: string | null,
    name: string,
    args?: { agent_id?: string; limit?: number; since_ns?: number },
  ) => {
    const q = new URLSearchParams();
    if (args?.agent_id) q.set("agent_id", args.agent_id);
    if (args?.limit) q.set("limit", String(args.limit));
    if (args?.since_ns !== undefined) q.set("since_ns", String(args.since_ns));
    return request<PredictionsResponse>(
      `/models/${encodeURIComponent(name)}/predictions${q.size ? `?${q}` : ""}`,
      token,
    );
  },
  modelPredictionStats: (
    token: string | null,
    name: string,
    args?: { agent_id?: string; since_ns?: number },
  ) => {
    const q = new URLSearchParams();
    if (args?.agent_id) q.set("agent_id", args.agent_id);
    if (args?.since_ns !== undefined) q.set("since_ns", String(args.since_ns));
    return request<PredictionStatsResponse>(
      `/models/${encodeURIComponent(name)}/prediction-stats${q.size ? `?${q}` : ""}`,
      token,
    );
  },

  // --- regime -------------------------------------------------------------
  regime: (token: string | null, history = 0) =>
    request<RegimeResponse>(
      `/regime${history > 0 ? `?history=${history}` : ""}`,
      token,
    ),

  // --- backtest -----------------------------------------------------------
  backtestStrategies: (token: string | null) =>
    request<BacktestStrategiesResponse>("/backtest/strategies", token),
  backtestRuns: (token: string | null) =>
    request<BacktestRunsListResponse>("/backtest/runs", token),
  backtestRun: (token: string | null, runId: string) =>
    request<BacktestRunDetailResponse>(
      `/backtest/runs/${encodeURIComponent(runId)}`,
      token,
    ),
  runBacktest: (token: string | null, body: BacktestRunRequest) =>
    request<BacktestRunResponse>("/backtest/run", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),

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
