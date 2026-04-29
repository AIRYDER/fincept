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
  schema_version?: number;
  position_id: string;
  strategy_id: string;
  symbol: string;
  ts_event: number;
  quantity: string;
  avg_entry_price: string;
  realized_pnl_usd: string;
  unrealized_pnl_usd: string;
  current_mark_price?: string | null;
  fees_paid_usd: string;
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

/** Server-sent envelope for any topic frame. */
export type WsFrame =
  | { topic: "positions"; event: { type: "position"; payload: Position } }
  | { topic: "fills"; event: { type: "fill"; payload: Fill } }
  | {
      topic: "predictions";
      event: { type: "prediction"; payload: Prediction };
    }
  | { topic: "alerts"; event: { type: "alert"; payload: AlertEvent } };
