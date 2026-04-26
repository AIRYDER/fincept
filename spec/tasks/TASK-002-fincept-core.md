# TASK-002 · `fincept-core` library

**Phase:** F · **Depends on:** TASK-001 · **Blocks:** everything else

## Goal

Implement the canonical schemas, config, logging, tracing, clock, IDs, errors, and leader election used by every other package.

## Files to create

```
libs/fincept-core/
├── pyproject.toml
├── src/fincept_core/
│   ├── __init__.py
│   ├── schemas.py           # ALL schemas from spec/CONTRACTS.md §1–§5
│   ├── events.py            # Redis Stream envelope
│   ├── config.py            # Settings(BaseSettings) reading env
│   ├── logging.py           # structlog JSON setup
│   ├── tracing.py           # OpenTelemetry OTLP setup
│   ├── clock.py             # ns-precision clock helpers
│   ├── ids.py               # ULID generator, idempotency keys
│   ├── leadership.py        # Redis-based leader election
│   └── errors.py            # exception hierarchy
└── tests/
    ├── test_schemas.py
    ├── test_config.py
    ├── test_clock.py
    ├── test_ids.py
    └── test_leadership.py
```

## `pyproject.toml`

```toml
[project]
name = "fincept-core"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "structlog>=24.4",
    "opentelemetry-api>=1.27",
    "opentelemetry-sdk>=1.27",
    "opentelemetry-exporter-otlp>=1.27",
    "python-ulid>=3.0",
    "redis>=5.1",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fincept_core"]
```

## Contract (MUST match exactly)

### `schemas.py`

Copy verbatim from `spec/CONTRACTS.md` sections 1 through 5. No additions, no deletions. If you think something is missing, STOP and open a PR against `CONTRACTS.md` first.

### `events.py`

```python
from typing import TypeVar, Generic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

class StreamEnvelope(BaseModel, Generic[T]):
    """Wraps a domain event with transport metadata when published to Redis Streams."""
    event_id: str              # ULID
    published_at: int          # nanos
    payload: T

def serialize(envelope: StreamEnvelope) -> dict[str, str]:
    """Return a dict safe for XADD (all values as strings)."""
    return {
        "event_id": envelope.event_id,
        "published_at": str(envelope.published_at),
        "payload_type": envelope.payload.__class__.__name__,
        "payload_json": envelope.payload.model_dump_json(),
    }

def deserialize(fields: dict[bytes, bytes], model_cls: type[T]) -> StreamEnvelope[T]:
    """Inverse of serialize."""
    decoded = {k.decode(): v.decode() for k, v in fields.items()}
    payload = model_cls.model_validate_json(decoded["payload_json"])
    return StreamEnvelope(
        event_id=decoded["event_id"],
        published_at=int(decoded["published_at"]),
        payload=payload,
    )
```

### `config.py`

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL", default="redis://localhost:6379/0")

    # Observability
    otel_exporter_otlp_endpoint: str | None = Field(alias="OTEL_EXPORTER_OTLP_ENDPOINT", default=None)
    log_level: str = Field(alias="LOG_LEVEL", default="INFO")

    # Secrets (sensitive; never logged)
    binance_api_key: str | None = Field(alias="BINANCE_API_KEY", default=None)
    binance_api_secret: str | None = Field(alias="BINANCE_API_SECRET", default=None)
    openai_api_key: str | None = Field(alias="OPENAI_API_KEY", default=None)
    anthropic_api_key: str | None = Field(alias="ANTHROPIC_API_KEY", default=None)
    polygon_api_key: str | None = Field(alias="POLYGON_API_KEY", default=None)

    # Behavior
    trading_mode: str = Field(alias="TRADING_MODE", default="paper")
    universe: list[str] = Field(alias="UNIVERSE", default_factory=lambda: ["BTC-USD"])
    default_venue: str = Field(alias="DEFAULT_VENUE", default="binance")
    max_notional_usd_per_symbol: float = Field(alias="MAX_NOTIONAL_USD_PER_SYMBOL", default=10000)
    max_gross_notional_usd: float = Field(alias="MAX_GROSS_NOTIONAL_USD", default=50000)
    max_daily_loss_usd: float = Field(alias="MAX_DAILY_LOSS_USD", default=2000)

def get_settings() -> Settings:
    """Cached settings singleton. Do not call at import time."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings

_settings: Settings | None = None
```

**Notes:**

- `universe` is a comma-separated env var; pydantic-settings parses it automatically if you declare it as `list[str]`.
- `get_settings()` is the ONLY way to read config. Never call `Settings()` directly outside this module.
- Never log a `Settings` instance — secrets would leak.

### `clock.py`

```python
import time

def now_ns() -> int:
    """Wall-clock nanoseconds since UNIX epoch."""
    return time.time_ns()

def ns_to_iso(ns: int) -> str:
    """ns since epoch → ISO 8601 UTC string with microsecond precision."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ns / 1e9, tz=_dt.UTC).isoformat()

def iso_to_ns(iso: str) -> int:
    """Inverse of ns_to_iso."""
    import datetime as _dt
    return int(_dt.datetime.fromisoformat(iso).timestamp() * 1e9)
```

### `ids.py`

```python
from ulid import ULID

def new_id() -> str:
    """Monotonic lexicographically sortable identifier."""
    return str(ULID())

def idempotency_key(*parts: str) -> str:
    """Stable hash for idempotency. Same parts → same key."""
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
```

### `logging.py`

```python
import logging
import sys
import structlog
from .config import get_settings

def configure_logging() -> None:
    """Call once at service startup. Emits JSON lines to stdout."""
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
    )

def get_logger(name: str) -> "structlog.stdlib.BoundLogger":
    return structlog.get_logger(name)
```

### `tracing.py`

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from .config import get_settings

def configure_tracing(service_name: str) -> None:
    """Call once at service startup."""
    settings = get_settings()
    if settings.otel_exporter_otlp_endpoint is None:
        return  # local dev — no exporter
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

def tracer(name: str):
    return trace.get_tracer(name)
```

### `errors.py`

```python
class FinceptError(Exception):
    """Base for all domain errors."""

class ConfigError(FinceptError):
    """Missing or invalid configuration."""

class SchemaError(FinceptError):
    """Event did not match expected schema."""

class RiskBreach(FinceptError):
    """Risk limit was violated."""

class KillSwitchActive(FinceptError):
    """Trading globally suspended."""

class VenueError(FinceptError):
    """Upstream venue returned an error."""

class RetryableError(FinceptError):
    """Transient; caller should retry with backoff."""
```

### `leadership.py`

```python
import asyncio
from redis.asyncio import Redis
from .ids import new_id
from .logging import get_logger

log = get_logger(__name__)

class Leader:
    """Redis-based leader election. Used by singleton services (orchestrator, risk, oms)."""

    def __init__(self, redis: Redis, role: str, ttl_seconds: int = 15) -> None:
        self.redis = redis
        self.key = f"leader:{role}"
        self.ttl = ttl_seconds
        self.token = new_id()
        self._task: asyncio.Task[None] | None = None
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._is_leader:
            # release if we still hold it
            lua = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
            await self.redis.eval(lua, 1, self.key, self.token)

    async def _loop(self) -> None:
        while True:
            if self._is_leader:
                # renew
                lua = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"
                ok = await self.redis.eval(lua, 1, self.key, self.token, self.ttl * 1000)
                if not ok:
                    log.warning("leader.lost", role=self.key)
                    self._is_leader = False
            else:
                # try acquire
                got = await self.redis.set(self.key, self.token, nx=True, ex=self.ttl)
                if got:
                    self._is_leader = True
                    log.info("leader.acquired", role=self.key)
            await asyncio.sleep(self.ttl / 3)
```

## Tests (MUST pass)

### `tests/test_schemas.py`

```python
from decimal import Decimal
from fincept_core.schemas import (
    TradeEvent, Venue, AssetClass, Side, OrderType, OrderIntent, TimeInForce
)

def test_trade_event_is_frozen():
    ev = TradeEvent(venue=Venue.BINANCE, symbol="BTC-USD", asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=1, ts_recv=2, price=Decimal("100"), size=Decimal("0.5"))
    try:
        ev.price = Decimal("101")  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("TradeEvent should be frozen")

def test_order_intent_defaults():
    o = OrderIntent(order_id="X", decision_id="D", ts_event=0, strategy_id="s", symbol="BTC-USD",
                    venue=Venue.BINANCE, side=Side.BUY, order_type=OrderType.MARKET, quantity=Decimal("1"))
    assert o.time_in_force == TimeInForce.GTC
    assert o.limit_price is None
```

### `tests/test_clock.py`

```python
from fincept_core.clock import now_ns, ns_to_iso, iso_to_ns

def test_clock_roundtrip():
    t = now_ns()
    assert abs(iso_to_ns(ns_to_iso(t)) - t) < 1_000  # <1μs drift from ISO precision
```

### `tests/test_ids.py`

```python
from fincept_core.ids import new_id, idempotency_key

def test_new_id_unique_and_sortable():
    a, b = new_id(), new_id()
    assert a != b
    assert a < b  # ULID is monotonic in one process

def test_idempotency_key_stable():
    assert idempotency_key("a", "b") == idempotency_key("a", "b")
    assert idempotency_key("a", "b") != idempotency_key("a", "c")
```

### `tests/test_leadership.py`

```python
import pytest, asyncio
from redis.asyncio import Redis
from fincept_core.leadership import Leader

@pytest.mark.asyncio
async def test_only_one_leader():
    r = Redis.from_url("redis://localhost:6379/15")
    await r.delete("leader:test")
    a = Leader(r, "test", ttl_seconds=5)
    b = Leader(r, "test", ttl_seconds=5)
    await a.start(); await b.start()
    await asyncio.sleep(1)
    assert a.is_leader ^ b.is_leader  # exactly one
    await a.stop(); await b.stop()
    await r.aclose()
```

## Out of scope

- Do NOT implement Redis Streams producer/consumer here. That's TASK-003.
- Do NOT implement DB access. That's TASK-004.

## Done when

- [ ] All files exist
- [ ] `pytest libs/fincept-core/tests` is green
- [ ] `mypy libs/fincept-core` is green
- [ ] `ruff check libs/fincept-core` is green
