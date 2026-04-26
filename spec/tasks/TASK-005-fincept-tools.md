# TASK-005 · `fincept-tools` — MCP-style tool protocol + data/analytics/exec tools

**Phase:** F · **Depends on:** TASK-002, TASK-004 · **Blocks:** TASK-061 (LLM agents), TASK-064 (LLM orchestrator)

## Goal

Tool protocol (`Tool`, `ToolInput`, `ToolOutput`), a global `ToolRegistry`, and a baseline set of tools that LLM agents will call. Output JSON-schema descriptions match OpenAI / Anthropic function-calling format. All exec tools target paper-only in v1; live exec is gated until Phase H.

## Files to create

```
libs/fincept-tools/
├── pyproject.toml
├── src/fincept_tools/
│   ├── __init__.py
│   ├── protocol.py           # Tool protocol, ToolInput, ToolOutput, base classes
│   ├── registry.py           # ToolRegistry singleton + json_schemas() helper
│   ├── data.py               # data.get_bars, data.get_quote, data.get_trades, entity.resolve
│   ├── analytics.py          # analytics.compute_vwap, analytics.compute_vol, analytics.compute_corr
│   └── exec.py               # exec.submit_order, exec.cancel_order (paper only)
└── tests/
    ├── test_protocol.py
    ├── test_registry.py
    ├── test_data_tools.py
    ├── test_analytics_tools.py
    └── test_exec_tools.py
```

## `pyproject.toml`

```toml
[project]
name = "fincept-tools"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.9",
    "fincept-core",
    "fincept-db",
    "fincept-bus",
    "numpy>=2.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fincept_tools"]
```

## Contracts (MUST match `spec/CONTRACTS.md §8`)

### `protocol.py`

```python
from typing import Any, ClassVar
from pydantic import BaseModel, ConfigDict

class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool = True
    error: str | None = None

class BaseTool:
    """Concrete tools subclass this and override __call__. The Tool typing protocol in
    CONTRACTS.md §8 is a structural type; this is the concrete base."""
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[ToolInput]]
    output_model: ClassVar[type[ToolOutput]]

    async def __call__(self, payload: ToolInput) -> ToolOutput:  # noqa: D401
        raise NotImplementedError
```

### `registry.py`

```python
from typing import Any
from .protocol import BaseTool

class ToolRegistry:
    """Singleton in process. Tools register themselves at import time."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"no such tool: {name}")
        return self._tools[name]

    def list(self) -> list[dict[str, Any]]:
        """Returns OpenAI / Anthropic function-call JSON schema for each tool."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_model.model_json_schema(),
                },
            }
            for t in self._tools.values()
        ]

REGISTRY: ToolRegistry = ToolRegistry()

def register(tool: BaseTool) -> BaseTool:
    """Decorator-friendly: @register applied to a tool instance registers it."""
    REGISTRY.register(tool)
    return tool
```

### `data.py`

Implements: `data.get_bars`, `data.get_quote`, `data.get_trades`, `entity.resolve`.

```python
from decimal import Decimal
from pydantic import Field
from fincept_core.schemas import BarEvent
from fincept_db.bars import read_bars
from fincept_db.ticks import read_trades
from .protocol import BaseTool, ToolInput, ToolOutput
from .registry import register

class GetBarsInput(ToolInput):
    symbol: str
    freq: str = Field(pattern=r"^(1m|1h|1d)$")
    start_ns: int
    end_ns: int

class GetBarsOutput(ToolOutput):
    bars: list[dict] = Field(default_factory=list)

class GetBarsTool(BaseTool):
    name = "data.get_bars"
    description = "Fetch OHLCV bars for a symbol over [start_ns, end_ns)."
    input_model = GetBarsInput
    output_model = GetBarsOutput

    async def __call__(self, payload: GetBarsInput) -> GetBarsOutput:  # type: ignore[override]
        bars = await read_bars(payload.symbol, payload.freq, payload.start_ns, payload.end_ns)
        return GetBarsOutput(bars=[b.model_dump(mode="json") for b in bars])

register(GetBarsTool())

class ResolveEntityInput(ToolInput):
    """Validates that a free-text symbol/company-name maps to an in-universe symbol."""
    query: str

class ResolveEntityOutput(ToolOutput):
    symbol: str | None = None
    in_universe: bool = False

class ResolveEntityTool(BaseTool):
    name = "entity.resolve"
    description = (
        "Resolve a free-text query (ticker or company name) to a canonical in-universe symbol. "
        "Returns in_universe=False if the symbol is unknown — agents MUST check this before "
        "emitting signals to prevent hallucination."
    )
    input_model = ResolveEntityInput
    output_model = ResolveEntityOutput

    async def __call__(self, payload: ResolveEntityInput) -> ResolveEntityOutput:  # type: ignore[override]
        # v1: simple case-folded lookup against the universe table.
        # Future: fuzzy match + LLM-disambiguate for company names.
        from fincept_db.engine import session_scope
        from fincept_db.models import UniverseSymbol
        from sqlalchemy import select
        async with session_scope() as s:
            q = select(UniverseSymbol).where(UniverseSymbol.symbol == payload.query.upper())
            row = (await s.execute(q)).scalar_one_or_none()
            if row and row.active:
                return ResolveEntityOutput(symbol=row.symbol, in_universe=True)
            return ResolveEntityOutput(symbol=None, in_universe=False)

register(ResolveEntityTool())

# get_quote, get_trades follow the same pattern; omitted for brevity here but MUST be implemented.
```

### `analytics.py`

```python
import numpy as np
from pydantic import Field
from .protocol import BaseTool, ToolInput, ToolOutput
from .registry import register

class ComputeVolInput(ToolInput):
    symbol: str
    lookback_bars: int = Field(ge=2, le=10000)
    freq: str = Field(default="1m", pattern=r"^(1m|1h|1d)$")
    end_ns: int  # PIT cutoff

class ComputeVolOutput(ToolOutput):
    realized_vol_annualized: float | None = None  # None if insufficient data

class ComputeVolTool(BaseTool):
    name = "analytics.compute_vol"
    description = "Realized vol over the last N bars ending at end_ns (PIT-safe). Annualized."
    input_model = ComputeVolInput
    output_model = ComputeVolOutput

    async def __call__(self, payload: ComputeVolInput) -> ComputeVolOutput:  # type: ignore[override]
        from fincept_db.bars import read_bars
        # Fetch lookback_bars + 1 to get N returns
        # (in production, call get_bars with a tight window or via a dedicated lookback API)
        bars = await read_bars(payload.symbol, payload.freq, 0, payload.end_ns)
        if len(bars) < payload.lookback_bars + 1:
            return ComputeVolOutput(ok=True, realized_vol_annualized=None)
        bars = bars[-(payload.lookback_bars + 1):]
        closes = np.array([float(b.close) for b in bars])
        rets = np.diff(np.log(closes))
        # Annualization factor
        per_year = {"1m": 525600.0, "1h": 8760.0, "1d": 252.0}[payload.freq]
        return ComputeVolOutput(realized_vol_annualized=float(rets.std() * (per_year ** 0.5)))

register(ComputeVolTool())

# compute_vwap, compute_corr follow the same pattern. Implement.
```

### `exec.py`

```python
from decimal import Decimal
from pydantic import Field
from fincept_core.schemas import OrderIntent, Side, OrderType, Venue, TimeInForce
from fincept_core.ids import new_id
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_ORDERS
from redis.asyncio import Redis
from .protocol import BaseTool, ToolInput, ToolOutput
from .registry import register

class SubmitOrderInput(ToolInput):
    decision_id: str
    strategy_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    venue: Venue = Venue.PAPER
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC

class SubmitOrderOutput(ToolOutput):
    order_id: str | None = None

class SubmitOrderTool(BaseTool):
    name = "exec.submit_order"
    description = "Submit an OrderIntent to the OMS. PAPER ONLY in v1."
    input_model = SubmitOrderInput
    output_model = SubmitOrderOutput

    async def __call__(self, payload: SubmitOrderInput) -> SubmitOrderOutput:  # type: ignore[override]
        if get_settings().trading_mode != "paper":
            return SubmitOrderOutput(ok=False, error="exec.submit_order is paper-only in v1")
        order_id = new_id()
        intent = OrderIntent(
            order_id=order_id, decision_id=payload.decision_id, ts_event=now_ns(),
            strategy_id=payload.strategy_id, symbol=payload.symbol, venue=payload.venue,
            side=payload.side, order_type=payload.order_type, quantity=payload.quantity,
            limit_price=payload.limit_price, time_in_force=payload.time_in_force,
        )
        # Publish to ord.orders for the OMS to consume
        r = Redis.from_url(get_settings().redis_url)
        try:
            await Producer(r).publish(STREAM_ORDERS, intent)
        finally:
            await r.aclose()
        return SubmitOrderOutput(order_id=order_id)

register(SubmitOrderTool())

# cancel_order follows the same pattern.
```

## Tests (MUST pass)

### `tests/test_protocol.py`

```python
from fincept_tools.protocol import ToolInput, ToolOutput, BaseTool

def test_tool_input_forbids_extra():
    class MyIn(ToolInput):
        x: int
    try:
        MyIn(x=1, y=2)  # type: ignore[call-arg]
    except Exception:
        return
    raise AssertionError("ToolInput must forbid extra fields")

def test_base_tool_raises_not_implemented():
    class T(BaseTool):
        name = "t"
        description = "d"
        input_model = ToolInput
        output_model = ToolOutput
    import asyncio
    try:
        asyncio.run(T()(ToolInput()))
    except NotImplementedError:
        return
    raise AssertionError("BaseTool.__call__ must raise NotImplementedError")
```

### `tests/test_registry.py`

```python
from fincept_tools.registry import REGISTRY

def test_listing_includes_data_get_bars():
    schemas = REGISTRY.list()
    names = [s["function"]["name"] for s in schemas]
    assert "data.get_bars" in names
    assert "entity.resolve" in names
    assert "analytics.compute_vol" in names
    assert "exec.submit_order" in names

def test_each_listed_tool_has_valid_json_schema():
    schemas = REGISTRY.list()
    for s in schemas:
        params = s["function"]["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
```

### `tests/test_exec_tools.py`

```python
import pytest
from decimal import Decimal
from fincept_core.schemas import Side, OrderType
from fincept_tools.registry import REGISTRY
from fincept_tools.exec import SubmitOrderInput

@pytest.mark.asyncio
async def test_submit_order_returns_order_id_in_paper_mode(monkeypatch):
    # Settings.trading_mode is "paper" by default in dev
    tool = REGISTRY.get("exec.submit_order")
    out = await tool(SubmitOrderInput(
        decision_id="d1", strategy_id="s1", symbol="BTC-USD",
        side=Side.BUY, order_type=OrderType.MARKET, quantity=Decimal("0.01"),
    ))
    assert out.ok
    assert out.order_id is not None
    assert len(out.order_id) == 26
```

## Out of scope

- No live exec; `exec.submit_order` returns `ok=False` if `TRADING_MODE != paper`.
- No tool authentication / per-tenant ACL (defer to Phase U / Phase H).
- No streaming tools (e.g., subscribe to a stream); v1 is request/response only.
- No tool composition / chaining helpers; agents call tools directly.

## Done when

- [ ] Files exist
- [ ] `pytest libs/fincept-tools/tests` is green (requires Redis + Postgres)
- [ ] `mypy libs/fincept-tools` is green
- [ ] `ruff check libs/fincept-tools` is green
- [ ] `REGISTRY.list()` returns ≥6 tools, each with valid OpenAI-format JSON schema
- [ ] Manual smoke: `python -c "import asyncio; from fincept_tools.registry import REGISTRY; print(asyncio.run(REGISTRY.get('analytics.compute_vol')(...)))"` returns sane output
