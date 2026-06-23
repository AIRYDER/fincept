/**
 * TypeScript mirrors of the Pydantic schemas in
 * libs/fincept-core/src/fincept_core/schemas.py.
 *
 * Decimals come over the wire as strings (Pydantic mode='json'
 * preserves Decimal precision), so all amount/quantity fields are
 * typed as `string` here; convert at the edge where you need numeric
 * math (formatUsd / Number()).
 */

export type Side = "buy" | "sell";
export type OrderType = "market" | "limit" | "stop" | "stop_limit";
export type TimeInForce = "gtc" | "ioc" | "fok" | "day";
export type OrderStatus =
  | "pending_new"
  | "new"
  | "partially_filled"
  | "filled"
  | "cancelled"
  | "rejected"
  | "expired";
export type Venue =
  | "binance"
  | "coinbase"
  | "kraken"
  | "nasdaq"
  | "nyse"
  | "alpaca"
  | "paper"
  | "sim";
export type AssetClass =
  | "crypto"
  | "crypto_spot"
  | "crypto_perp"
  | "equity"
  | "fx"
  | "future"
  | "option";
export type AlertSeverity = "info" | "warning" | "critical";

export interface Position {
  /** Mirrors libs/fincept-core/.../schemas.py::Position. */
  strategy_id: string;
  symbol: string;
  quantity: string;
  avg_cost: string;
  realized_pnl: string;
  unrealized_pnl: string;
  updated_at: number;
  /**
   * Live mark price, attached by the API from md:last:{symbol} when the
   * Alpaca scheduler has seen a quote.  Undefined if no mark is cached.
   */
  mark_px?: string;
}

export interface OrderRecord {
  schema_version?: number;
  order_id: string;
  decision_id: string;
  ts_event: number;
  strategy_id: string;
  symbol: string;
  venue: Venue;
  side: Side;
  order_type: OrderType;
  quantity: string;
  limit_price?: string | null;
  stop_price?: string | null;
  time_in_force?: string;
  tags?: Record<string, string>;
  status: OrderStatus;
  filled_qty: string;
  avg_fill_price?: string | null;
  venue_order_id?: string | null;
  created_at: number;
  updated_at: number;
}

export interface PlaceOrderBody {
  symbol: string;
  side: Side;
  order_type: OrderType;
  quantity: string;
  limit_price?: string | null;
  stop_price?: string | null;
  time_in_force?: TimeInForce;
  venue?: Venue;
  strategy_id?: string;
  tags?: Record<string, string>;
}

export interface PlaceOrderResponse {
  ok: boolean;
  order_id: string;
  decision_id: string;
  ts_event: number;
  strategy_id: string;
}

export interface Fill {
  fill_id: string;
  order_id: string;
  ts_event: number;
  strategy_id: string;
  symbol: string;
  venue: Venue;
  side: Side;
  quantity: string;
  price: string;
  fee_usd: string;
}

export interface Prediction {
  agent_id: string;
  symbol: string;
  ts_event: number;
  horizon_ns: number;
  direction: number;
  confidence: number;
  calibration_tag?: string;
}

export interface Decision {
  decision_id: string;
  ts_event: number;
  strategy_id: string;
  symbol: string;
  side: Side;
  target_notional_usd: string;
  urgency: number;
  rationale: string;
  source_signals: string[];
  expires_at?: number | null;
}

export interface AlertEvent {
  alert_id: string;
  ts_event: number;
  severity: AlertSeverity;
  source: string;
  code: string;
  message: string;
  tags?: Record<string, string>;
}

export interface UniverseRow {
  symbol: string;
  asset_class: AssetClass;
  venue_default: Venue;
  /** Temporary backward-compatible alias for older callers. Prefer venue_default. */
  venue: Venue;
  active: boolean;
  base_ccy?: string;
  quote_ccy?: string;
  tick_size?: string;
  lot_size?: string;
}

export interface UniverseSeedFromPositionsResponse {
  seeded: number;
  symbols: string[];
  universe: UniverseRow[];
}

export interface Bar {
  symbol: string;
  freq: string;
  ts_event: number;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
}

export type DataCoverageStatus = "ok" | "stale" | "empty" | "error";

export interface DataCoverageRow {
  symbol: string;
  asset_class?: string | null;
  venue?: string | null;
  venue_default?: string | null;
  freq: string;
  status: DataCoverageStatus;
  bar_count: number;
  last_ts_event: number | null;
  age_ns: number | null;
  error_type?: string;
  error?: string;
  debug?: string;
}

export interface DataCoverageResponse {
  freq: string;
  venue?: string | null;
  as_of_ns: number;
  lookback_ns: number;
  stale_after_ns: number;
  summary: {
    total: number;
    ok: number;
    stale: number;
    empty: number;
    error: number;
    coverage_pct: number;
  };
  rows: DataCoverageRow[];
}

export type DataSourceSafety =
  | "read_only"
  | "paper_first"
  | "internal_state"
  | "experimental_read_only";

export interface DataSourceHealth {
  mode: string;
  checks: string[];
}

export interface DataSourceDefinition {
  id: string;
  name: string;
  area: string;
  category: string;
  safety: DataSourceSafety;
  status: string;
  call_surfaces: string[];
  data: string[];
  return_format: string;
  latency: string;
  health: DataSourceHealth;
  config: string[];
}

export interface DataSourcesResponse {
  sources: DataSourceDefinition[];
  summary: {
    total: number;
    by_category: Record<string, number>;
  };
}

export interface AlpacaDataDemoResponse {
  ok: boolean;
  provider: "alpaca";
  base_url: string;
  symbols: string[];
  feed: string;
  timeframe: string;
  window: {
    start: string;
    end: string;
  };
  summary: {
    news_count: number;
    symbols_with_bars: number;
    bar_count: number;
  };
  news: Array<Record<string, unknown>>;
  bars: Record<string, Array<Record<string, unknown>>>;
  next_page_token?: string | null;
}

export interface StrategyRow {
  strategy_id: string;
  position_count: number;
  open_positions: number;
}

/**
 * Mirrors ``fincept_core.strategy_config.StrategyConfig.to_dict``.
 *
 * One persistent strategy instance config as served by the Phase F
 * ``/strategies/configs`` endpoints.  Timestamps are wall-clock
 * seconds (``time.time()`` on the server).
 */
export interface StrategyConfigRow {
  strategy_id: string;
  class_name: string;
  symbols: string[];
  params: Record<string, unknown>;
  model_binding: string | null;
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

/** Body for ``POST /strategies/configs``. */
export interface CreateStrategyConfigBody {
  strategy_id: string;
  class_name: string;
  symbols: string[];
  params?: Record<string, unknown>;
  model_binding?: string | null;
  enabled?: boolean;
}

/** Body for ``PATCH /strategies/configs/{id}``.  All fields optional. */
export interface UpdateStrategyConfigBody {
  class_name?: string;
  symbols?: string[];
  params?: Record<string, unknown>;
  model_binding?: string | null;
  enabled?: boolean;
}

export interface NewsSymbolImpact {
  symbol: string;
  in_book: boolean;
  price_at_publish: string | null;
  mark: string | null;
  pct_change: number | null;
  dollar_impact: string | null;
  /** Compact close-price series from publish → now (float, length <= 60). */
  sparkline: number[];
}

/** One scored ticker suggestion from `GET /data/symbols/search`. */
export interface SymbolMatch {
  symbol: string;
  name: string;
  asset_class: string;
  /** Match score: higher = more relevant.  Tier ranges:
   * 1000=exact symbol, 800=exact name, 500+=prefix, 300=word-prefix,
   * 200=symbol-substring, 100=name-substring, 50=1-edit fuzzy. */
  score: number;
  /** Where the candidate originated. */
  source: "universe" | "well_known";
}

export type NewsTier = "alert" | "impact" | "universe";

export interface NewsArticle {
  id: string;
  headline: string;
  summary: string;
  source: string;
  url: string;
  author: string;
  created_at: string;
  ts_event_ns: number;
  symbols: NewsSymbolImpact[];
  touched_book: boolean;
  /** True when at least one symbol had bars from the data feed, so
   * pct_change / dollar_impact reflect a real market reaction (not
   * the structurally-zero fallback). */
  has_impact_math: boolean;
  /** Aggregate signed dollar impact across all in-book symbols. */
  total_dollar_impact: string | null;
  /** True when the signed total impact is negative — the story is
   * hurting our position direction.  Drives the "adverse" boost in
   * server scoring + UI highlight. */
  is_adverse: boolean;
  /** Hours since publish at the time of this response. */
  age_hours: number;
  /** Composite priority: |$ impact| × decay × adverse-boost.  Higher
   * is more urgent.  0 for stories without impact math. */
  score: number;
  /** |$ impact| / gross book equity, as a Decimal string (e.g. "0.012"
   * = 1.2%).  Null when has_impact_math is false. */
  pct_of_book: string | null;
  /** Server-classified lane: "alert" | "impact" | "universe". */
  tier: NewsTier;
}

export interface NewsResponse {
  /** Top-priority lane: book stories above the alert threshold. */
  alert: NewsArticle[];
  /** Other book-touching stories, sorted by composite score. */
  impact: NewsArticle[];
  /** Non-book stories, time-sorted. */
  universe: NewsArticle[];
  book_symbols: string[];
  /** Net signed $ impact summed across alert + impact lanes. */
  book_total_impact: string;
  /** Gross notional book equity (Σ |qty| × mark) used for pct_of_book. */
  book_equity_usd: string;
  /** Threshold (decimal, e.g. 0.005 = 0.5%) above which a book story
   * is promoted to the alert lane. */
  alert_pct_of_book: number;
  /** Half-life used for the recency decay component, in hours. */
  recency_half_life_h: number;
}

// --- /news-impact ---------------------------------------------------------
// Experimental bridge for experiments/news-impact-model.  This is separate
// from the production /news feed and does not emit trading signals.

export interface NewsImpactDatasetProfile {
  path: string;
  event_count: number;
  horizons: string[];
  sources: Record<string, number>;
  event_types: Record<string, number>;
  symbols: Record<string, number>;
}

export interface NewsImpactStatus {
  app: string;
  dataset_loaded: boolean;
  profile: NewsImpactDatasetProfile;
  last_optimization: NewsImpactOptimization | null;
  experiment_root: string;
  sample_data: string;
  mode: "experimental_demo";
}

export interface NewsImpactHorizon {
  expected_return: number;
  p_up: number;
  q10: number;
  q50: number;
  q90: number;
  sample_size: number;
}

export interface NewsImpactSimilarEvent {
  event_id: string;
  source: string;
  headline: string;
  event_type: string;
  score: number;
  abnormal_returns: Record<string, number>;
}

export interface NewsImpactPrediction {
  event_id: string;
  symbol: string;
  event_type: string;
  horizons: Record<string, NewsImpactHorizon>;
  volatility_impact: number;
  volume_impact: number;
  confidence: number;
  similar_events: NewsImpactSimilarEvent[];
  model_version: string;
}

export interface NewsImpactPredictBody {
  event: {
    event_id?: string;
    source: string;
    headline: string;
    body?: string;
    symbols: string[];
    event_type: string;
    language?: string;
    available_at_ns?: number;
  };
  context: {
    symbol: string;
    market_regime?: string;
    pre_event_return?: number | null;
    realized_volatility?: number | null;
    relative_volume?: number | null;
    spread_bps?: number | null;
    liquidity_score?: number | null;
  };
  horizons: string[];
  top_k?: number;
  weights?: Record<string, number> | null;
}

export interface NewsImpactPredictResponse {
  prediction: NewsImpactPrediction;
  dataset_profile: NewsImpactDatasetProfile;
  mode: "experimental_demo";
}

export interface NewsImpactOptimization {
  mode: string;
  horizon: string;
  n_predictions: number;
  metrics: {
    mae: number | null;
    directional_accuracy: number | null;
  };
  candidates_tested: number;
  weights: Record<string, number>;
  folds: Array<{
    target_event_id: string;
    train_events: number;
    predicted: number;
    actual: number;
    abs_error: number;
    direction_hit: boolean;
  }>;
}

export interface NewsImpactOptimizeResponse {
  optimization: NewsImpactOptimization;
  dataset_profile: NewsImpactDatasetProfile;
  mode: "experimental_demo";
}

// --- /research/exa -------------------------------------------------------

export type ExaSearchType =
  | "auto"
  | "fast"
  | "instant"
  | "deep-lite"
  | "deep"
  | "deep-reasoning";

export interface ExaResearchRequest {
  query: string;
  symbol?: string | null;
  search_type?: ExaSearchType;
  num_results?: number;
  max_age_hours?: number | null;
}

export interface ExaResearchBrief {
  headline: string;
  summary: string;
  bull_case: string[];
  bear_case: string[];
  catalysts: string[];
  risks: string[];
  watch_items: string[];
}

export interface ExaResearchCitation {
  url: string;
  title?: string | null;
}

export interface ExaResearchGrounding {
  field: string;
  citations: ExaResearchCitation[];
  confidence?: "low" | "medium" | "high" | null;
}

export interface ExaResearchResponse {
  ok: boolean;
  error?: string | null;
  error_type?: string | null;
  request_id?: string | null;
  brief: ExaResearchBrief;
  grounding: ExaResearchGrounding[];
  sources: ExaResearchCitation[];
  cost_dollars?: number | null;
}

// --- /research/openbb ----------------------------------------------------

export interface OpenBBQuoteRequest {
  symbol: string;
  provider?: string;
}

export interface OpenBBQuoteResponse {
  ok: boolean;
  error?: string | null;
  error_type?: string | null;
  provider: string;
  results: Array<Record<string, unknown>>;
}

export interface OpenBBCallRequest {
  path: string;
  params?: Record<string, string>;
}

export interface OpenBBCallResponse {
  ok: boolean;
  error?: string | null;
  error_type?: string | null;
  path: string;
  provider?: string | null;
  results: Array<Record<string, unknown>>;
}

export interface OpenBBHealthResponse {
  ok: boolean;
  url: string;
  latency_ms?: number;
  warning?: string;
  error?: string;
  error_type?: string;
}

export interface OpenBBHealthEntry {
  id: string;
  ts_ms: number;
  ok: boolean;
  latency_ms: number | null;
  url: string | null;
  error_type: string | null;
  error: string | null;
  warning: string | null;
}

export interface OpenBBHealthSummary {
  samples: number;
  uptime_pct: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  last_error_type: string | null;
}

export interface OpenBBHealthHistoryResponse {
  entries: OpenBBHealthEntry[];
  summary: OpenBBHealthSummary;
}


export interface ProviderDataSummary {
  total_records: number;
  ok_records: number;
  error_records: number;
  latest_ts_event: number | null;
  providers: Record<string, number>;
  datasets: Record<string, number>;
}

export interface ProviderDataRecord {
  record_id: string;
  schema_version: string;
  provider: string;
  source: string;
  dataset: string;
  endpoint: string;
  symbol: string | null;
  ts_event: number;
  ts_observed: number | null;
  request_hash: string;
  row_count: number;
  ok: boolean;
  error_type: string | null;
  normalized: Record<string, unknown>;
}

export interface ProviderDataResponse {
  ok: boolean;
  capture_enabled: boolean;
  error?: string | null;
  error_type?: string | null;
  summary: ProviderDataSummary;
  records: ProviderDataRecord[];
}

export interface ServiceStatus {
  name: string;
  status: "up" | "stale" | "down";
  last_beat_unix: number | null;
  age_sec: number | null;
  expected: boolean;
}

export type FeatureId =
  | "market_data"
  | "news_learning"
  | "jobs"
  | "gbm_predictor"
  | "news_alpha_predictor"
  | "sentiment"
  | "regime"
  | "openbb";

export interface FeatureControlLastAction {
  feature_id: FeatureId;
  action?: "start" | "stop" | "restart";
  status?: string;
  output?: string;
  ts_unix?: number;
}

export interface FeatureStartResponse {
  ok: boolean;
  feature_id: FeatureId;
  action?: "start";
  started: boolean;
  status: "already_running" | "launch_requested";
  services: string[];
  fresh_services: string[];
  output?: string;
}

export interface FeatureControlResponse {
  ok: boolean;
  feature_id: FeatureId;
  action?: "stop" | "restart";
  status: "stop_requested" | "restart_requested";
  services: string[];
  fresh_services: string[];
  output?: string;
}

export interface FeatureLogsResponse {
  ok: boolean;
  feature_id: FeatureId;
  services: string[];
  fresh_services: string[];
  last_control: FeatureControlLastAction | null;
}

export interface ServicesResponse {
  services: ServiceStatus[];
  summary: {
    up: number;
    expected: number;
    stale_after_sec: number;
    ttl_sec: number;
  };
}

export interface KillSwitchState {
  engaged: boolean;
  actor: string | null;
  reason: string | null;
  alert_id: string | null;
  ts_unix: number | null;
}

// --- /models -------------------------------------------------------------
// Mirrors services/api/src/api/routes/models.py response shape.

export interface ModelCvSummary {
  /** Total folds attempted (including any single-class skips). */
  n_folds: number | null;
  /** Folds that produced a valid AUC. */
  n_scored: number | null;
  /** Folds skipped (single-class etc). */
  n_skipped: number | null;
  /** Median best_iter across scored folds; used for the final refit. */
  median_best_iter: number | null;
  mean_auc: number | null;
  std_auc: number | null;
  min_auc: number | null;
  max_auc: number | null;
}

export interface ModelCvFold {
  fold: number | null;
  train_rows: number | null;
  val_rows: number | null;
  best_iter: number | null;
  /** null on a degenerate (single-class) fold. */
  best_auc: number | null;
  /** Populated only when the fold was skipped. */
  reason_skipped: string | null;
}

export interface ModelRecord {
  name: string;
  path: string;
  model_file_exists: boolean;
  trained_at_unix: number | null;
  age_seconds: number | null;
  /** "walk_forward" | "holdout_80_20" | null when meta missing. */
  eval_mode: string | null;
  horizon_bars: number | null;
  horizon_ns: number | null;
  bar_seconds: number | null;
  features: string[];
  feature_count: number;
  cv_summary: ModelCvSummary | null;
  /**
   * Per-fold breakdown.  Always present in the detail endpoint when
   * eval_mode=walk_forward; null otherwise (legacy holdout, no CV).
   */
  cv_folds: ModelCvFold[] | null;
  purge_bars: number | null;
  embargo_bars: number | null;
  final_train_rows: number | null;
  final_num_boost_round: number | null;
  /** Legacy 80/20 holdout AUC; null when eval_mode=walk_forward. */
  holdout_auc: number | null;
  holdout_rows: number | null;
  training_input_path: string | null;
  training_request: TrainingRunRequest | null;
  warnings: string[];
}

export interface ModelsResponse {
  models: ModelRecord[];
  summary: {
    count: number;
    with_cv: number;
    with_holdout: number;
    with_warnings: number;
    models_dir: string;
  };
}

export interface FeatureImportanceRow {
  feature: string;
  /** Number of tree splits that used this feature (always present). */
  split_count: number;
  /**
   * Gain-based importance.  Null when only the model.txt fallback was
   * used; populated when a feature_importance.json sidecar is present.
   */
  gain: number | null;
  /** 1 = most important, ascending.  Stable across reloads. */
  rank: number;
}

export interface FeatureImportanceResponse {
  model: string;
  importances: FeatureImportanceRow[];
  /** "split_count" or "gain_and_split". */
  importance_type: "split_count" | "gain_and_split";
  /** "model_text" (parsed) or "sidecar" (trainer-provided). */
  source: "model_text" | "sidecar";
  warnings: string[];
}

// --- /models/train + /models/runs ----------------------------------------
// Mirrors services/api/src/api/training.py and routes/models.py.

export type TrainingRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed";

export interface TrainingRunRequest {
  model_name: string;
  input_path: string;
  horizon_bars: number;
  bar_seconds: number;
  cv_folds: number;
  /** -1 means "use horizon_bars" on the trainer side. */
  purge_bars: number;
  embargo_bars: number;
  num_boost_round: number;
  early_stopping_rounds: number;
}

export interface TrainingRun {
  run_id: string;
  status: TrainingRunStatus;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  duration_seconds: number | null;
  exit_code: number | null;
  pid: number | null;
  out_dir: string;
  log_path: string;
  error: string | null;
  request: TrainingRunRequest;
  /** Populated only by the detail endpoint, not the listing. */
  log_tail?: string[];
}

export interface TrainingRunsResponse {
  runs: TrainingRun[];
  summary: {
    count: number;
    running: number;
    queued: number;
    completed: number;
    failed: number;
  };
}

/** Body shape for POST /models/train.  The api fills in defaults that
 * mirror the trainer CLI's defaults; only ``model_name`` and
 * ``input_path`` are strictly required.
 */
export interface TrainModelBody {
  model_name: string;
  input_path: string;
  horizon_bars?: number;
  bar_seconds?: number;
  cv_folds?: number;
  purge_bars?: number;
  embargo_bars?: number;
  num_boost_round?: number;
  early_stopping_rounds?: number;
}

// --- /models/promote/* ---------------------------------------------------
// Mirrors services/api/src/api/promotions.py and routes/models.py.

export interface ActiveBinding {
  agent_id: string;
  model_name: string;
  promoted_at: number;
  promoted_by: string;
}

export interface PromotionStateResponse {
  agent_id: string;
  /** ``null`` when no model has been promoted (or rollback cleared it). */
  active: ActiveBinding | null;
  /** Phase E1: shadow candidate.  ``null`` when no shadow is bound. */
  shadow: ActiveBinding | null;
  /** Newest first.  Bounded by ``history_limit`` query (default 10). */
  history: ActiveBinding[];
}

export interface PromoteResponse {
  agent_id: string;
  active: ActiveBinding;
  /** Legacy field; agents hot-reload now (Phase D1) so always ignored. */
  restart_required: boolean;
}

export interface RollbackResponse {
  agent_id: string;
  /** ``null`` when rollback cleared the only history entry. */
  active: ActiveBinding | null;
  /** Shadow is preserved across rollback. */
  shadow: ActiveBinding | null;
  history: ActiveBinding[];
}

/** Response from ``POST /models/{name}/shadow`` (Phase E1). */
export interface ShadowResponse {
  agent_id: string;
  /** May be ``null`` if no model has ever been promoted active. */
  active: ActiveBinding | null;
  /** Always non-null on success; the binding that was just set. */
  shadow: ActiveBinding;
}

/** Response from ``POST /models/promote/shadow/clear`` (Phase E1). */
export interface ClearShadowResponse {
  agent_id: string;
  /** ``true`` if a file was removed; ``false`` if shadow was already clear. */
  cleared: boolean;
  active: ActiveBinding | null;
  shadow: null;
}

export interface NewsAlphaCandidateReport {
  approved: boolean;
  reasons: string[];
  candidate_model_name: string;
  candidate_dir: string;
  candidate_meta: Record<string, unknown>;
  active_model_name: string | null;
  active_meta: Record<string, unknown> | null;
  policy: Record<string, unknown>;
  generated_at: number;
  promotion_hint: Record<string, unknown>;
}

export interface NewsAlphaCandidateReportResponse {
  exists: boolean;
  report_path: string;
  report: NewsAlphaCandidateReport | null;
}

// --- /models/{name}/predictions, /prediction-stats -----------------------
// Mirrors services/api/src/api/routes/models.py (Phase D2).

export interface PredictionRow {
  /** uuid hex generated at write time. */
  id: string;
  /** Wall-clock ns when the row hit disk. */
  ts_recorded: number;
  /** Original Prediction.ts_event. */
  ts_event: number;
  /** Prediction horizon in nanoseconds (echoed from the model). */
  horizon_ns: number;
  symbol: string;
  /** -1.0 .. +1.0 directional signal. */
  direction: number;
  /** 0.0 .. 1.0; |direction| in the gbm calibration. */
  confidence: number;
}

export interface PredictionsResponse {
  model: string;
  agent_id: string;
  count: number;
  predictions: PredictionRow[];
}

export interface PredictionStats {
  count: number;
  mean_confidence: number;
  long_count: number;
  short_count: number;
  flat_count: number;
}

export interface PredictionStatsResponse {
  model: string;
  agent_id: string;
  stats: PredictionStats;
}

// --- /regime -------------------------------------------------------------
// Mirrors services/api/src/api/routes/regime.py response shape.

export type RegimeLabel = "risk_on" | "risk_off" | "high_vol" | "neutral";

export interface RegimeSnapshot {
  agent_id: string;
  ts_event: number;
  regime: RegimeLabel | string;
  confidence: number;
  vix: number | null;
  yield_spread: number | null;
  fed_funds: number | null;
  rationale: string;
  /** Mapped from REGIME_DIRECTION; the consensus tilt for risk assets. */
  direction_bias: number;
  /** Wall-clock seconds since the snapshot was written. */
  age_seconds: number | null;
}

export interface RegimeHistoryEntry {
  stream_id: string;
  agent_id: string | null;
  ts_event: number | null;
  regime: string | null;
  confidence: number | null;
}

export interface RegimeResponse {
  status: "ok" | "unavailable";
  snapshot: RegimeSnapshot | null;
  history: RegimeHistoryEntry[];
  direction_map: Record<string, number>;
}

// --- /backtest -----------------------------------------------------------
// Mirrors services/api/src/api/routes/backtest.py + backtester.report.

export interface BacktestStrategyInfo {
  key: string;
  class_name: string;
  strategy_id: string;
  description: string;
}

export interface BacktestStrategiesResponse {
  strategies: BacktestStrategyInfo[];
}

export interface BacktestEquityPoint {
  ts_event: number;
  equity_usd: number;
}

export interface BacktestPerSymbolStats {
  symbol: string;
  fills: number;
  bought_qty: number;
  sold_qty: number;
  notional_traded: number;
  fees_paid: number;
}

export interface BacktestTradeRow {
  fill_id: string;
  order_id: string;
  ts_event: number;
  symbol: string;
  side: string;
  price: number;
  quantity: number;
  fee: number;
  is_maker: boolean | null;
}

export interface BacktestReport {
  starting_cash: number;
  final_equity: number;
  total_return_pct: number;
  n_bars: number;
  n_fills: number;
  fees_paid_total: number;
  sharpe: number | null;
  max_drawdown_pct: number | null;
  longest_drawdown_bars: number | null;
  bars_per_year: number;
  per_symbol: BacktestPerSymbolStats[];
  equity_curve: BacktestEquityPoint[];
  trades: BacktestTradeRow[];
}

export interface BacktestManifest {
  run_id: string;
  status: string;
  started_at: number;
  finished_at: number;
  parquet_path: string;
  strategy_name: string;
  strategy_params: Record<string, unknown>;
  starting_cash: number;
  freq: string;
  venue: string;
  asset_class: string;
  bars_per_year: number;
  symbols: string[];
  start_ns: number;
  end_ns: number;
  n_bars: number;
  n_fills: number;
  final_equity: number;
  total_return_pct: number;
  sharpe: number | null;
  max_drawdown_pct: number | null;
}

export interface BacktestRunResponse {
  run_id: string;
  manifest: BacktestManifest;
  report: BacktestReport;
}

export interface BacktestRunsListResponse {
  runs: BacktestManifest[];
  summary: { count: number; reports_root: string };
}

export interface BacktestRunDetailResponse {
  run_id: string;
  manifest: BacktestManifest | null;
  report: BacktestReport;
}

export interface BacktestRunRequest {
  bars_path: string;
  strategy: string;
  strategy_params?: Record<string, unknown>;
  starting_cash?: number;
  freq?: string;
  venue?: string;
  asset_class?: string;
}

/** Server-sent envelope for any topic frame. */
export type WsFrame =
  | { topic: "positions"; event: { type: "position"; payload: Position } }
  | { topic: "fills"; event: { type: "fill"; payload: Fill } }
  | {
      topic: "predictions";
      event: { type: "prediction"; payload: Prediction };
    }
  | { topic: "alerts"; event: { type: "alert"; payload: AlertEvent } };

// ---------------------------------------------------------------------------
// On-demand module control (TASK-0203)
// ---------------------------------------------------------------------------

export type ModuleStatus =
  | "running"
  | "stopped"
  | "idle"
  | "degraded"
  | "unknown";

export type ModuleCostClass = "low" | "medium" | "high";

export interface ModuleSummary {
  module_id: string;
  display_name: string;
  description: string;
  cost_class: ModuleCostClass;
  idle_timeout_sec: number;
  allowed_environments: string[];
  services: string[];
  status: ModuleStatus;
  started_at_unix: number | null;
  last_activity_unix: number | null;
  idle_seconds: number;
  idle_countdown_sec: number;
  fresh_services: string[];
}

export interface ModulesListResponse {
  ok: boolean;
  modules: ModuleSummary[];
}

export interface ModuleDetailResponse {
  ok: boolean;
  module: ModuleSummary;
}

export interface ModuleStartResponse {
  ok: boolean;
  module_id: string;
  action: "start";
  started: boolean;
  status: "already_running" | "launch_requested";
  services: string[];
  fresh_services: string[];
  output?: string;
}

export interface ModuleControlResponse {
  ok: boolean;
  module_id: string;
  action: "stop" | "restart";
  status: "stop_requested" | "restart_requested";
  services: string[];
  fresh_services: string[];
  output?: string;
}

export interface ModuleStopAllResponse {
  ok: boolean;
  stopped: string[];
  ts_unix: number;
}

export interface ModuleSweepIdleResponse {
  ok: boolean;
  stopped: string[];
  ts_unix: number;
}

export interface ModuleReceipt {
  module_id: string;
  action: "start" | "stop" | "restart" | "auto_stop";
  status: string;
  actor: string;
  output?: string;
  ts_unix: number;
}

export interface ModuleReceiptsResponse {
  ok: boolean;
  receipts: ModuleReceipt[];
}

// ---------------------------------------------------------------------------
// TASK-0801: Quant Foundry overview page types.
// Mirrors services/api/src/api/routes/quant_foundry.py + gateway.health().
// ---------------------------------------------------------------------------

export interface QuantFoundryHealthResponse {
  enabled: boolean;
  mode: string;
  shadow_only?: boolean;
  job_count?: number;
  detail?: string;
}

export interface QuantFoundryHeartbeat {
  worker_id: string;
  ts_unix: number;
  status?: string;
}

export interface QuantFoundryJob {
  job_id: string;
  job_type: string;
  status: string;
  idempotency_key?: string;
  priority?: number;
  budget_cents?: number | null;
  created_at_ns?: number;
  updated_at_ns?: number;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// TASK-0802: Quant Foundry dossier, tournament, and promotion read models.
// Mirrors quant_foundry.dossier, leaderboard_expanded, and promotion to_dict().
// ---------------------------------------------------------------------------

export interface QuantFoundryDossier {
  readonly schema_version: number;
  readonly model_id: string;
  readonly artifact_manifest_id: string;
  readonly artifact_sha256: string;
  readonly dataset_manifest_id: string;
  readonly dataset_manifest_ref?: string | null;
  readonly feature_schema_hash: string;
  readonly label_schema_hash: string;
  readonly code_git_sha?: string | null;
  readonly lockfile_hash?: string | null;
  readonly container_image_digest?: string | null;
  readonly random_seed?: number | null;
  readonly hardware_class?: string | null;
  readonly trial_count: number;
  readonly training_metrics: Record<string, number>;
  readonly status: string;
  readonly settlement_evidence_refs: readonly string[];
  readonly shadow_prediction_refs: readonly string[];
  readonly blocking_issues: readonly Record<string, unknown>[];
  readonly registered_at_ns?: number | null;
  readonly content_hash: string;
}

export interface QuantFoundryLeaderboardSlice {
  readonly horizon?: string;
  readonly regime?: string;
  readonly cluster?: string;
  readonly score: number;
}

export interface QuantFoundryBaselineDelta {
  readonly baseline_model_id: string;
  readonly delta: number;
  readonly baseline_score: number;
}

export interface QuantFoundryCalibrationSummary {
  readonly brier_score: number;
  readonly reliability: number;
  readonly n_bins: number;
}

export interface QuantFoundryDecayIndicator {
  readonly decay_score: number;
  readonly is_stale: boolean;
  readonly is_decayed: boolean;
  readonly days_since_last_settlement: number;
}

export interface QuantFoundryTournamentEntry {
  readonly model_id: string;
  readonly total_score: number;
  readonly settled_count: number;
  readonly horizon_slices: readonly QuantFoundryLeaderboardSlice[];
  readonly regime_slices: readonly QuantFoundryLeaderboardSlice[];
  readonly symbol_cluster_slices: readonly QuantFoundryLeaderboardSlice[];
  readonly baseline_delta: QuantFoundryBaselineDelta | null;
  readonly calibration_summary: QuantFoundryCalibrationSummary | null;
  readonly decay_indicator: QuantFoundryDecayIndicator | null;
}

export interface QuantFoundryPromotionRequest {
  readonly model_id: string;
  readonly target_level: string;
  readonly review_note: string;
  readonly waivers: readonly {
    readonly issue_code: string;
    readonly waived_by: string;
    readonly reason: string;
  }[];
}

export interface QuantFoundryPromotionEvidence {
  readonly dossier: QuantFoundryDossier | null;
  readonly tournament_result: Record<string, unknown> | null;
  readonly sentinel_receipt: Record<string, unknown> | null;
  readonly blocking_issues: readonly {
    readonly code: string;
    readonly severity: string;
    readonly message: string;
  }[];
}

export interface QuantFoundryPromotionQueueEntry {
  readonly request: QuantFoundryPromotionRequest;
  readonly evidence: QuantFoundryPromotionEvidence;
}

export interface QuantFoundryPromotionReview {
  readonly decision: string;
  readonly request: QuantFoundryPromotionRequest;
  readonly review_note: string;
  readonly rejection_reason: string | null;
  readonly decided_at_ns: number;
}
