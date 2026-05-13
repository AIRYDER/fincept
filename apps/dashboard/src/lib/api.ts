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
  AlpacaDataDemoResponse,
  Bar,
  ClearShadowResponse,
  CreateStrategyConfigBody,
  DataCoverageResponse,
  DataSourcesResponse,
  ExaResearchRequest,
  ExaResearchResponse,
  FeatureControlResponse,
  FeatureId,
  FeatureLogsResponse,
  FeatureStartResponse,
  FeatureImportanceResponse,
  KillSwitchState,
  ModelRecord,
  ModelsResponse,
  NewsAlphaCandidateReportResponse,
  NewsImpactOptimizeResponse,
  NewsImpactPredictBody,
  NewsImpactPredictResponse,
  NewsImpactStatus,
  NewsResponse,
  OpenBBCallRequest,
  OpenBBCallResponse,
  OpenBBHealthHistoryResponse,
  OpenBBHealthResponse,
  OpenBBQuoteRequest,
  OpenBBQuoteResponse,
  OrderRecord,
  PlaceOrderBody,
  PlaceOrderResponse,
  Position,
  ProviderDataResponse,
  PredictionsResponse,
  PredictionStatsResponse,
  PromoteResponse,
  PromotionStateResponse,
  RegimeResponse,
  RollbackResponse,
  ServicesResponse,
  ShadowResponse,
  StrategyConfigRow,
  StrategyRow,
  SymbolMatch,
  TrainModelBody,
  TrainingRun,
  TrainingRunsResponse,
  UniverseSeedFromPositionsResponse,
  UniverseRow,
  UpdateStrategyConfigBody,
} from "@/lib/types";

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message?: string) {
    super(message ?? messageFromBody(status, body));
    this.status = status;
    this.body = body;
  }
}

function messageFromBody(status: number, body: unknown): string {
  if (typeof body === "string" && body.trim()) return body;
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const detail = record.detail;
    if (typeof record.error === "string" && record.error.trim()) return record.error;
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (detail && typeof detail === "object") {
      const detailRecord = detail as Record<string, unknown>;
      if (typeof detailRecord.message === "string" && detailRecord.message.trim()) {
        return detailRecord.message;
      }
      if (typeof detailRecord.error === "string" && detailRecord.error.trim()) {
        return detailRecord.error;
      }
      if (typeof detailRecord.error_type === "string" && detailRecord.error_type.trim()) {
        return detailRecord.error_type;
      }
    }
    if (typeof record.error_type === "string" && record.error_type.trim()) return record.error_type;
  }
  return `API error ${status}`;
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
  seedUniverseFromPositions: (token: string | null) =>
    request<UniverseSeedFromPositionsResponse>(
      "/data/universe/seed-from-positions",
      token,
      { method: "POST" },
    ),
  /**
   * Typeahead matcher backed by the universe + curated well-known list.
   * Use this for ticker autocomplete on the strategy + manual-order
   * forms.  Throttle the caller (debounce 150-200ms is plenty); the
   * endpoint itself is sub-millisecond on a fakeredis but doesn't
   * need to be hammered on every keystroke.
   */
  searchSymbols: (
    token: string | null,
    q: string,
    args?: { limit?: number },
  ) => {
    const params = new URLSearchParams();
    params.set("q", q);
    if (args?.limit) params.set("limit", String(args.limit));
    return request<SymbolMatch[]>(
      `/data/symbols/search?${params}`,
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
  dataCoverage: (
    token: string | null,
    args?: {
      asset_class?: string;
      freq?: string;
      venue?: string;
      lookback_ns?: number;
      stale_after_ns?: number;
    },
  ) => {
    const q = new URLSearchParams();
    if (args?.asset_class) q.set("asset_class", args.asset_class);
    if (args?.freq) q.set("freq", args.freq);
    if (args?.venue) q.set("venue", args.venue);
    if (args?.lookback_ns !== undefined) q.set("lookback_ns", String(args.lookback_ns));
    if (args?.stale_after_ns !== undefined) q.set("stale_after_ns", String(args.stale_after_ns));
    return request<DataCoverageResponse>(
      `/data/coverage${q.size ? `?${q}` : ""}`,
      token,
    );
  },
  dataSources: (token: string | null) =>
    request<DataSourcesResponse>("/data/sources", token),
  alpacaDataDemo: (
    token: string | null,
    args?: { symbols?: string; news_limit?: number; bar_limit?: number },
  ) => {
    const q = new URLSearchParams();
    if (args?.symbols) q.set("symbols", args.symbols);
    if (args?.news_limit) q.set("news_limit", String(args.news_limit));
    if (args?.bar_limit) q.set("bar_limit", String(args.bar_limit));
    return request<AlpacaDataDemoResponse>(
      `/data/alpaca/demo${q.size ? `?${q}` : ""}`,
      token,
    );
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
  placeOrder: (token: string | null, body: PlaceOrderBody) =>
    request<PlaceOrderResponse>("/orders", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  openbbCall: (token: string | null, body: OpenBBCallRequest) =>
    request<OpenBBCallResponse>("/research/openbb", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  openbbHealth: (token: string | null) =>
    request<OpenBBHealthResponse>("/research/openbb/health", token),
  openbbHealthHistory: (token: string | null, limit = 120) =>
    request<OpenBBHealthHistoryResponse>(
      `/research/openbb/health/history?limit=${encodeURIComponent(String(limit))}`,
      token,
    ),
  providerData: (
    token: string | null,
    args?: { provider?: string; dataset?: string; symbol?: string; limit?: number },
  ) => {
    const q = new URLSearchParams();
    if (args?.provider) q.set("provider", args.provider);
    if (args?.dataset) q.set("dataset", args.dataset);
    if (args?.symbol) q.set("symbol", args.symbol);
    if (args?.limit) q.set("limit", String(args.limit));
    return request<ProviderDataResponse>(
      `/research/provider-data${q.size ? `?${q}` : ""}`,
      token,
    );
  },

  // --- strategies ---------------------------------------------------------
  strategies: (token: string | null) =>
    request<StrategyRow[]>("/strategies", token),

  // Phase F: StrategyConfig CRUD + lifecycle.
  strategyConfigs: (token: string | null) =>
    request<StrategyConfigRow[]>("/strategies/configs", token),
  strategyConfig: (token: string | null, id: string) =>
    request<StrategyConfigRow>(
      `/strategies/configs/${encodeURIComponent(id)}`,
      token,
    ),
  createStrategyConfig: (
    token: string | null,
    body: CreateStrategyConfigBody,
  ) =>
    request<StrategyConfigRow>("/strategies/configs", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  adoptStrategyConfig: (token: string | null, id: string) =>
    request<StrategyConfigRow>(
      `/strategies/configs/${encodeURIComponent(id)}/adopt`,
      token,
      { method: "POST" },
    ),
  updateStrategyConfig: (
    token: string | null,
    id: string,
    body: UpdateStrategyConfigBody,
  ) =>
    request<StrategyConfigRow>(
      `/strategies/configs/${encodeURIComponent(id)}`,
      token,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
  deleteStrategyConfig: (token: string | null, id: string) =>
    request<null>(`/strategies/configs/${encodeURIComponent(id)}`, token, {
      method: "DELETE",
    }),
  startStrategy: (token: string | null, id: string) =>
    request<StrategyConfigRow>(
      `/strategies/configs/${encodeURIComponent(id)}/start`,
      token,
      { method: "POST" },
    ),
  stopStrategy: (token: string | null, id: string) =>
    request<StrategyConfigRow>(
      `/strategies/configs/${encodeURIComponent(id)}/stop`,
      token,
      { method: "POST" },
    ),
  strategyHistory: (token: string | null, id: string, limit = 50) =>
    request<StrategyConfigRow[]>(
      `/strategies/configs/${encodeURIComponent(id)}/history?limit=${limit}`,
      token,
    ),

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
  newsImpactStatus: (token: string | null) =>
    request<NewsImpactStatus>("/news-impact/status", token),
  newsImpactPredict: (token: string | null, body: NewsImpactPredictBody) =>
    request<NewsImpactPredictResponse>("/news-impact/predict", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  newsImpactOptimize: (
    token: string | null,
    body: {
      horizon: string;
      mode?: "leave-one-out" | "walk-forward";
      min_train_events?: number;
      top_k?: number;
    },
  ) =>
    request<NewsImpactOptimizeResponse>("/news-impact/optimize", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- research ----------------------------------------------------------
  exaResearch: (token: string | null, body: ExaResearchRequest) =>
    request<ExaResearchResponse>("/research/exa", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  openbbQuote: (token: string | null, body: OpenBBQuoteRequest) =>
    request<OpenBBQuoteResponse>("/research/openbb/quote", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- services -----------------------------------------------------------
  services: (token: string | null) =>
    request<ServicesResponse>("/services", token),
  startFeature: (token: string | null, featureId: FeatureId) =>
    request<FeatureStartResponse>(
      `/features/${encodeURIComponent(featureId)}/start`,
      token,
      { method: "POST" },
    ),
  stopFeature: (token: string | null, featureId: FeatureId) =>
    request<FeatureControlResponse>(
      `/features/${encodeURIComponent(featureId)}/stop`,
      token,
      { method: "POST" },
    ),
  restartFeature: (token: string | null, featureId: FeatureId) =>
    request<FeatureControlResponse>(
      `/features/${encodeURIComponent(featureId)}/restart`,
      token,
      { method: "POST" },
    ),
  featureLogs: (token: string | null, featureId: FeatureId) =>
    request<FeatureLogsResponse>(
      `/features/${encodeURIComponent(featureId)}/logs`,
      token,
    ),

  // --- models -------------------------------------------------------------
  models: (token: string | null) => request<ModelsResponse>("/models", token),
  modelDetail: (token: string | null, name: string) =>
    request<ModelRecord>(`/models/${encodeURIComponent(name)}`, token),
  modelFeatureImportance: (token: string | null, name: string) =>
    request<FeatureImportanceResponse>(
      `/models/${encodeURIComponent(name)}/feature-importance`,
      token,
    ),
  newsAlphaCandidateReport: (token: string | null) =>
    request<NewsAlphaCandidateReportResponse>(
      "/models/news-alpha/candidate-report",
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
  killSwitchState: (token: string | null) =>
    request<KillSwitchState>("/kill-switch", token),
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
