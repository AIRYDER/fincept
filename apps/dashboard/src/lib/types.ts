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
export type OrderStatus =
  | "pending_new"
  | "new"
  | "partially_filled"
  | "filled"
  | "cancelled"
  | "rejected"
  | "expired";
export type Venue = "binance" | "alpaca" | "sim";
export type AssetClass = "crypto" | "equity" | "fx" | "future" | "option";
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
  venue: Venue;
  active: boolean;
  base_ccy?: string;
  quote_ccy?: string;
  tick_size?: string;
  lot_size?: string;
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

export interface StrategyRow {
  strategy_id: string;
  position_count: number;
  open_positions: number;
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
}

export interface NewsResponse {
  impact: NewsArticle[];
  universe: NewsArticle[];
  book_symbols: string[];
  book_total_impact: string;
}

export interface ServiceStatus {
  name: string;
  status: "up" | "stale" | "down";
  last_beat_unix: number | null;
  age_sec: number | null;
  expected: boolean;
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
  /** Newest first.  Bounded by ``history_limit`` query (default 10). */
  history: ActiveBinding[];
}

export interface PromoteResponse {
  agent_id: string;
  active: ActiveBinding;
  /** Always true today; the api never restarts the agent itself. */
  restart_required: boolean;
}

export interface RollbackResponse {
  agent_id: string;
  /** ``null`` when rollback cleared the only history entry. */
  active: ActiveBinding | null;
  history: ActiveBinding[];
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
