/**
 * Typed REST client for services/api FastAPI app.
 *
 * Every method matches a route in:
 *   services/api/src/api/routes/{data,positions,orders,strategies,control}.py
 *
 * Errors throw a typed ``ApiError`` (or subclass) carrying status + body so
 * panels can distinguish "not authenticated" (401 -> redirect to /login) from
 * "endpoint unavailable" (5xx -> show inline error toast) from "timeout"
 * (AbortController fires -> show "slow backend" message, not "no data").
 *
 * TASK-0204: every ``fetch`` call is wrapped with an ``AbortController`` that
 * fires after ``DEFAULT_TIMEOUT_MS``.  Callers can override per-call via the
 * ``timeoutMs`` option on ``request``.  A timeout throws ``TimeoutError``
 * (subclass of ``ApiError``) so UI panels can render a precise message instead
 * of confusing a slow backend with "no data."
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
  NewsImpactSignalsResponse,
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
  ModulesListResponse,
  ModuleDetailResponse,
  ModuleStartResponse,
  ModuleControlResponse,
  ModuleStopAllResponse,
  ModuleSweepIdleResponse,
  ModuleReceiptsResponse,
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

/**
 * Default fetch timeout (8 s).  Chosen to be longer than a healthy backend
 * round-trip (~50-200 ms) but short enough that an operator sees a clear
 * "slow backend" message before assuming "no data."  Override per-call via
 * ``request(..., { timeoutMs })`` for known-slow endpoints (e.g. backtest
 * run, model train).
 */
export const DEFAULT_TIMEOUT_MS = 8_000;

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message?: string) {
    super(message ?? messageFromBody(status, body));
    this.status = status;
    this.body = body;
  }
}

/**
 * Typed error subclasses so UI panels can render precise operator messages
 * instead of a generic "something went wrong."  Each maps to a distinct
 * operator-visible state (TASK-0204 acceptance criterion: "Backend
 * unavailable is not confused with 'no data'").
 */
export class UnauthorizedError extends ApiError {
  constructor(body: unknown) {
    super(401, body, "Session expired — please sign in again.");
  }
}

export class UnavailableError extends ApiError {
  constructor(status: number, body: unknown) {
    super(status, body, "Backend unavailable — check service status.");
  }
}

export class TimeoutError extends ApiError {
  timeoutMs: number;
  constructor(timeoutMs: number) {
    super(0, null, `Request timed out after ${timeoutMs} ms — backend may be slow or down.`);
    this.timeoutMs = timeoutMs;
  }
}

export class ValidationError extends ApiError {
  constructor(body: unknown) {
    super(422, body, "Validation failed — check the form inputs.");
  }
}

export class StaleError extends ApiError {
  constructor(body: unknown) {
    super(409, body, "Data is stale — refresh to try again.");
  }
}

/**
 * Classify an HTTP status into the most specific typed error subclass.
 * Falls back to ``ApiError`` for unhandled status codes.
 */
function classifyError(status: number, body: unknown): ApiError {
  if (status === 401) return new UnauthorizedError(body);
  if (status === 422) return new ValidationError(body);
  if (status === 409) return new StaleError(body);
  if (status >= 500) return new UnavailableError(status, body);
  return new ApiError(status, body);
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

/**
 * Options for ``request``.  Extends ``RequestInit`` with a per-call timeout
 * override (defaults to ``DEFAULT_TIMEOUT_MS``).
 */
export interface RequestOptions extends RequestInit {
  /** Override the default 8 s timeout for known-slow endpoints. */
  timeoutMs?: number;
}

async function request<T>(
  path: string,
  token: string | null,
  init: RequestOptions = {},
): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...fetchInit } = init;
  const headers = new Headers(fetchInit.headers);
  headers.set("Accept", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (fetchInit.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  // TASK-0204: AbortController timeout — prevents a slow backend from
  // hanging the UI indefinitely.  The operator sees a TimeoutError message
  // ("backend may be slow or down") instead of a frozen page.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...fetchInit,
      headers,
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new TimeoutError(timeoutMs);
    }
    // Network error (backend unreachable) — classify as unavailable so the
    // UI doesn't confuse it with "no data."
    throw new UnavailableError(0, null);
  }
  clearTimeout(timer);

  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) throw classifyError(res.status, body);
  return body as T;
}

export const api = {
  // --- public --------------------------------------------------------------
  health: (token: string | null = null) =>
    request<{ ok: boolean; version: string }>("/health", token),

  /**
   * Server-side unified readiness (TASK-0202).
   * Returns categorized states (pass/warn/fail/skipped/disabled/stale).
   * No secrets or stacks are ever returned.
   */
  readiness: (token: string | null = null) =>
    request<{
      overall: string;
      checks: Array<{ id: string; label: string; state: string; detail: string }>;
      receipt_url?: string;
      generated_at_unix?: number;
      note?: string;
    }>("/health/readiness", token),

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
  newsImpactSignals: (
    token: string | null,
    args?: { limit?: number },
  ) => {
    const q = new URLSearchParams();
    if (args?.limit) q.set("limit", String(args.limit));
    return request<NewsImpactSignalsResponse>(
      `/news-impact/signals${q.size ? `?${q}` : ""}`,
      token,
    );
  },

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

  // --- modules (TASK-0203: on-demand module control) ---------------------
  modules: (token: string | null) =>
    request<ModulesListResponse>("/modules", token),
  moduleDetail: (token: string | null, moduleId: string) =>
    request<ModuleDetailResponse>(
      `/modules/${encodeURIComponent(moduleId)}`,
      token,
    ),
  startModule: (token: string | null, moduleId: string) =>
    request<ModuleStartResponse>(
      `/modules/${encodeURIComponent(moduleId)}/start`,
      token,
      { method: "POST" },
    ),
  stopModule: (token: string | null, moduleId: string) =>
    request<ModuleControlResponse>(
      `/modules/${encodeURIComponent(moduleId)}/stop`,
      token,
      { method: "POST" },
    ),
  restartModule: (token: string | null, moduleId: string) =>
    request<ModuleControlResponse>(
      `/modules/${encodeURIComponent(moduleId)}/restart`,
      token,
      { method: "POST" },
    ),
  stopAllModules: (token: string | null) =>
    request<ModuleStopAllResponse>("/modules/stop-all", token, {
      method: "POST",
    }),
  sweepIdleModules: (token: string | null) =>
    request<ModuleSweepIdleResponse>("/modules/sweep-idle", token, {
      method: "POST",
    }),
  moduleReceipts: (token: string | null) =>
    request<ModuleReceiptsResponse>("/modules/receipts", token),

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
      timeoutMs: 30_000, // model training can take a while
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
      timeoutMs: 60_000, // backtest can take up to a minute
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
