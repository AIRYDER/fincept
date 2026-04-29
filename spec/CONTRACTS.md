# Contracts — The Only Source of Truth for Data Types and Interfaces

> **Version:** 1.0.0
> **Rule:** any change requires a version bump and a migration note. No exceptions.

These are the **exact** definitions. When a task says "match the contract," it means copy these verbatim. All field names, types, and defaults are pinned.

---

## 1. Core enums

```python
# libs/fincept-core/src/fincept_core/schemas.py
from enum import StrEnum

class Venue(StrEnum):
    BINANCE = "binance"
    COINBASE = "coinbase"
    KRAKEN = "kraken"
    NASDAQ = "nasdaq"
    NYSE = "nyse"
    ALPACA = "alpaca"        # primary brokerage; paper + live via ALPACA_BASE_URL
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
```

## 2. Market data events

All prices and sizes are `Decimal` (not float — precision matters). All timestamps are integer nanoseconds since UNIX epoch (UTC).

```python
from decimal import Decimal
from pydantic import BaseModel, Field, ConfigDict

class MarketEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    venue: Venue
    symbol: str                       # canonical form: BTC-USD, AAPL
    asset_class: AssetClass
    ts_event: int                     # nanoseconds since epoch (venue clock)
    ts_recv: int                      # nanoseconds since epoch (our clock)
    seq: int | None = None            # venue sequence number if available

class TradeEvent(MarketEvent):
    event_type: str = "trade"
    price: Decimal
    size: Decimal
    side: Side | None = None          # None if venue does not disclose

class BookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)
    price: Decimal
    size: Decimal

class BookDeltaEvent(MarketEvent):
    event_type: str = "book_delta"
    bids_add: list[BookLevel] = Field(default_factory=list)
    bids_remove: list[Decimal] = Field(default_factory=list)  # prices to remove
    asks_add: list[BookLevel] = Field(default_factory=list)
    asks_remove: list[Decimal] = Field(default_factory=list)

class BookSnapshotEvent(MarketEvent):
    event_type: str = "book_snapshot"
    bids: list[BookLevel]
    asks: list[BookLevel]

class BarEvent(MarketEvent):
    event_type: str = "bar"
    freq: str                         # "1m", "1h", "1d"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int
    vwap: Decimal | None = None
```

## 3. Signals and predictions

```python
class Prediction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    agent_id: str                     # e.g. "gbm_predictor.v3"
    symbol: str
    horizon_ns: int                   # forecast horizon
    ts_event: int                     # feature-set-as-of time
    direction: float                  # in [-1.0, +1.0]
    magnitude: float | None = None    # expected return, unitless
    confidence: float                 # in [0.0, 1.0]
    calibration_tag: str | None = None  # identifies calibration curve used

class SentimentSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = 1
    agent_id: str
    symbol: str
    ts_event: int
    score: float                      # in [-1, +1]
    confidence: float
    event_type: str | None = None     # "earnings", "guidance", "m&a", "macro", ...
    source_url: str | None = None
    source_excerpt: str | None = None
    entities: list[str] = Field(default_factory=list)

class RegimeSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = 1
    agent_id: str
    ts_event: int
    regime: str                       # "trend_up", "trend_down", "mean_revert", "high_vol", "low_liq"
    confidence: float

class AlertEvent(BaseModel):
    """Operational alerts emitted by ingestor.quality, risk gate, and other watchdogs.

    Consumers (PagerDuty bridge, dashboard, audit log) branch on `code` and `severity`.
    `tags` are free-form key-value pairs for context (always str on the wire — Decimals
    must be stringified by the producer).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    event_type: str = "alert"
    alert_id: str                     # ULID
    ts_event: int                     # nanoseconds since epoch
    severity: str                     # "info" | "warning" | "critical"
    source: str                       # e.g. "ingestor.quality", "risk.gate"
    code: str                         # machine-readable: "seq_gap", "stale", "cross_spread", "clock_skew"
    message: str                      # human-readable
    tags: dict[str, str] = Field(default_factory=dict)
```

## 4. Decisions, orders, fills

```python
class Decision(BaseModel):
    """Output of orchestrator. Input to risk gate."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    decision_id: str                  # ULID
    ts_event: int
    strategy_id: str
    symbol: str
    side: Side
    target_notional_usd: Decimal      # positive = open, 0 = flatten, negative = reverse
    urgency: float                    # in [0, 1]; drives execution schedule
    rationale: str                    # human/machine-readable, for audit
    source_signals: list[str]         # IDs of agents/signals that fed this decision
    expires_at: int | None = None

class OrderIntent(BaseModel):
    """Output of risk gate. Input to OMS."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    order_id: str                     # ULID (parent order)
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
    created_at: int                   # nanos
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
```

## 5. Risk

```python
class RiskCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    approved: bool
    reduced_notional_usd: Decimal | None = None  # if risk reshapes the size
    reasons: list[str] = Field(default_factory=list)
    checked_at: int                   # nanos

class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str
    symbol: str
    quantity: Decimal                 # signed; negative = short
    avg_cost: Decimal
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    updated_at: int
```

## 6. Stream names (Redis Streams)

```python
# libs/fincept-bus/src/fincept_bus/streams.py

# Market data (ephemeral, 1-day retention, maxlen ~1M)
STREAM_MD_TRADES   = "md.trades"          # TradeEvent
STREAM_MD_BOOKS    = "md.books"           # BookDeltaEvent | BookSnapshotEvent
STREAM_MD_BARS_1M  = "md.bars.1m"         # BarEvent

# Signals (30-day retention)
STREAM_SIG_PREDICT = "sig.predict"        # Prediction
STREAM_SIG_SENT    = "sig.sentiment"      # SentimentSignal
STREAM_SIG_REGIME  = "sig.regime"         # RegimeSignal

# Decisions & orders (WORM — never delete)
STREAM_DECISIONS   = "ord.decisions"      # Decision
STREAM_ORDERS      = "ord.orders"         # Order (state transitions)
STREAM_FILLS       = "ord.fills"          # Fill
STREAM_POSITIONS   = "ord.positions"      # Position (snapshots after fills)

# Operational events (200k retention)
STREAM_ALERTS      = "events.alerts"      # AlertEvent

# Online features (5M retention; ~14 days of 1m bars across a small universe)
STREAM_FEATURES_ONLINE = "features.online"  # FeatureFrame
```

## 7. Agent interface

Every agent inherits this. Do not add methods to the base; add capabilities by publishing new signal types.

```python
# services/agents/src/agents/base.py
from abc import ABC, abstractmethod
from typing import AsyncIterator

class Agent(ABC):
    """One agent process = one Agent subclass instance."""

    agent_id: str                                  # class attribute, e.g. "gbm_predictor.v3"

    @abstractmethod
    async def setup(self) -> None:
        """Load models, warm caches, open connections. Called once."""

    @abstractmethod
    async def run(self) -> AsyncIterator[BaseModel]:
        """Yield signals (Prediction / SentimentSignal / RegimeSignal). Infinite loop."""

    @abstractmethod
    async def teardown(self) -> None:
        """Flush, close, persist. Called on graceful shutdown."""
```

## 8. Tool protocol (MCP-style, for LLM agents)

```python
# libs/fincept-tools/src/fincept_tools/protocol.py
from typing import Any, Protocol

class ToolInput(BaseModel):
    """Per-tool input schema; subclasses define fields. extra='forbid'."""

class ToolOutput(BaseModel):
    """Per-tool output schema; subclasses define fields. extra='forbid'."""
    ok: bool = True
    error: str | None = None
    error_type: str | None = None   # class name of typed error on failure

class Tool(Protocol):
    name: str                              # unique, e.g. "data.get_bars"
    description: str                       # one-line, human readable
    input_model: type[ToolInput]
    output_model: type[ToolOutput]

    async def __call__(self, payload: ToolInput) -> ToolOutput: ...

class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool: ...
    def list(self) -> list[dict[str, Any]]:
        """Returns JSON-schema descriptions suitable for LLM function-calling."""
```

Every tool in `libs/fincept-tools/src/fincept_tools/` declares its `ToolInput`/`ToolOutput` subclass. The registry exposes OpenAI / Anthropic function-call JSON schemas automatically.

**Typed errors.** Tool implementations raise subclasses of `fincept_tools.errors.ToolError` (itself a `FinceptError`) for known failure modes — e.g. `NotInUniverse`, `PaperOnlyExec`, `ToolBackendError`. The `BaseTool` runner catches them and returns `ToolOutput(ok=False, error=str(exc), error_type=type(exc).__name__)`. Untyped exceptions propagate (programming errors must be visible). Callers branch on `error_type` to recover from specific failure modes without parsing strings.

**Cost tracking.** `BaseTool.__call__` opens an OTel span `tool.<name>` for every invocation, with attributes `tool.args_size`, `tool.result_size`, `tool.duration_ns`, `tool.ok`, and `tool.error_type`. The orchestrator aggregates these for per-tool cost accounting.

## 9. Strategy interface (for research + backtesting)

```python
# libs/fincept-sdk/src/fincept_sdk/strategy.py

class Strategy(ABC):
    strategy_id: str
    symbols: list[str]

    def on_start(self, ctx: "StrategyContext") -> None: ...
    def on_bar(self, ctx: "StrategyContext", bar: BarEvent) -> None: ...
    def on_tick(self, ctx: "StrategyContext", trade: TradeEvent) -> None: ...
    def on_fill(self, ctx: "StrategyContext", fill: Fill) -> None: ...
    def on_signal(self, ctx: "StrategyContext", sig: BaseModel) -> None: ...
    def on_stop(self, ctx: "StrategyContext") -> None: ...

class StrategyContext(Protocol):
    now_ns: int
    positions: dict[str, Position]
    def submit(self, intent: OrderIntent) -> str: ...      # returns order_id
    def cancel(self, order_id: str) -> None: ...
    def get_feature(self, name: str, symbol: str) -> float | None: ...
    def log(self, msg: str, **kwargs: Any) -> None: ...
```

## 10. Config contract

Every service reads config from env via `fincept_core.config.Settings`. The env vars are:

```bash
# Storage
DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/fincept
REDIS_URL=redis://host:6379/0

# Observability
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.grafana.com/...
LOG_LEVEL=INFO

# Secrets (never logged)
BINANCE_API_KEY=
BINANCE_API_SECRET=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
ALPACA_API_KEY=
ALPACA_API_SECRET=
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # paper-api.alpaca.markets for paper, api.alpaca.markets for live
JWT_SECRET=dev-only-change-me                       # HS256 signing key for API bearer tokens; MUST be overridden in production
POLYGON_API_KEY=

# Behavior
TRADING_MODE=paper                    # paper | live (paper-only until Gate 5)
UNIVERSE=BTC-USD,ETH-USD,SOL-USD
DEFAULT_VENUE=binance
MAX_NOTIONAL_USD_PER_SYMBOL=10000
MAX_GROSS_NOTIONAL_USD=50000
MAX_DAILY_LOSS_USD=2000
```

Any service that requires an env var absent from this list must add it to `.env.example` and `spec/CONTRACTS.md §10` in the same PR.

## 11. HTTP API shape

Exposed by `services/api`. Each route returns JSON matching the schemas above (serialized with `model_dump(mode='json')`). All mutating routes require `X-Idempotency-Key` header.

| Route | Method | Body | Returns |
|---|---|---|---|
| `/health` | GET | — | `{ok: bool, version: str}` |
| `/universe` | GET | — | `{symbols: [str]}` |
| `/bars/{symbol}` | GET | query: `freq, start, end` | `[BarEvent]` |
| `/positions` | GET | — | `[Position]` |
| `/orders` | GET | query: `strategy_id, status` | `[Order]` |
| `/strategies` | GET | — | `[{strategy_id, status, p_and_l}]` |
| `/strategies/{id}/start` | POST | — | `{ok: bool}` |
| `/strategies/{id}/stop` | POST | — | `{ok: bool}` |
| `/kill-switch` | POST | `{reason: str}` | `{ok: bool}` |
| `/ws/stream` | WS | — | Streams: positions, fills, predictions (by subscription) |

## Versioning

Every schema has `schema_version: int = 1`. When a breaking change is required:

1. Create `schemas_v2.py` alongside `schemas.py`.
2. Update producers to emit v2.
3. Consumers handle both v1 and v2 during migration.
4. Once all producers emit v2, delete v1.

This is the ONLY supported evolution path. Never mutate a v1 field.
