from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Venue(StrEnum):
    BINANCE = "binance"
    COINBASE = "coinbase"
    KRAKEN = "kraken"
    NASDAQ = "nasdaq"
    NYSE = "nyse"
    PAPER = "paper"


class AssetClass(StrEnum):
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_PERP = "crypto_perp"
    EQUITY = "equity"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    DAY = "day"


class OrderStatus(StrEnum):
    PENDING_NEW = "pending_new"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MarketEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    venue: Venue
    symbol: str
    asset_class: AssetClass
    ts_event: int
    ts_recv: int
    seq: int | None = None


class TradeEvent(MarketEvent):
    event_type: str = "trade"
    price: Decimal
    size: Decimal
    side: Side | None = None


class BookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)
    price: Decimal
    size: Decimal


class BookDeltaEvent(MarketEvent):
    event_type: str = "book_delta"
    bids_add: list[BookLevel] = Field(default_factory=list)
    bids_remove: list[Decimal] = Field(default_factory=list)
    asks_add: list[BookLevel] = Field(default_factory=list)
    asks_remove: list[Decimal] = Field(default_factory=list)


class BookSnapshotEvent(MarketEvent):
    event_type: str = "book_snapshot"
    bids: list[BookLevel]
    asks: list[BookLevel]


class BarEvent(MarketEvent):
    event_type: str = "bar"
    freq: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int
    vwap: Decimal | None = None


class Prediction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    agent_id: str
    symbol: str
    horizon_ns: int
    ts_event: int
    direction: float
    magnitude: float | None = None
    confidence: float
    calibration_tag: str | None = None


class SentimentSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = 1
    agent_id: str
    symbol: str
    ts_event: int
    score: float
    confidence: float
    event_type: str | None = None
    source_url: str | None = None
    source_excerpt: str | None = None
    entities: list[str] = Field(default_factory=list)


class RegimeSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = 1
    agent_id: str
    ts_event: int
    regime: str
    confidence: float


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    decision_id: str
    ts_event: int
    strategy_id: str
    symbol: str
    side: Side
    target_notional_usd: Decimal
    urgency: float
    rationale: str
    source_signals: list[str]
    expires_at: int | None = None


class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    order_id: str
    decision_id: str
    ts_event: int
    strategy_id: str
    symbol: str
    venue: Venue
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    tags: dict[str, str] = Field(default_factory=dict)


class Order(OrderIntent):
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_qty: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    venue_order_id: str | None = None
    created_at: int
    updated_at: int


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    fill_id: str
    order_id: str
    ts_event: int
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal = Decimal(0)
    fee_currency: str = "USD"
    is_maker: bool | None = None
    venue_exec_id: str | None = None


class RiskCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    approved: bool
    reduced_notional_usd: Decimal | None = None
    reasons: list[str] = Field(default_factory=list)
    checked_at: int


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
