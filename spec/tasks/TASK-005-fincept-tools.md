# TASK-005 · `fincept-tools` — typed tool protocol + data/analytics/exec tools

**Phase:** F · **Depends on:** TASK-002, TASK-004 · **Blocks:** TASK-061 (LLM agents), TASK-064 (LLM orchestrator)

**Status:** [x] Implemented and verified.  See `Done` checklist at the bottom.

## Goal

Tool protocol (`Tool`, `ToolInput`, `ToolOutput`), a global `ToolRegistry`, a typed-error hierarchy (`fincept_tools.errors`), and a baseline set of tools that LLM agents will call.  Output JSON-schema descriptions match OpenAI / Anthropic function-calling format.  All exec tools are paper-only in v1; live exec is gated until Phase H.

## Files (as built)

```
libs/fincept-tools/
├── pyproject.toml
├── src/fincept_tools/
│   ├── __init__.py            # registers all built-in tools at import
│   ├── py.typed                # PEP 561 marker
│   ├── errors.py               # ToolError + NotInUniverse / PaperOnlyExec / ToolBackendError / ToolValidationError
│   ├── protocol.py             # ToolInput, ToolOutput, BaseTool, Tool (Protocol), ToolMeta
│   ├── registry.py             # ToolRegistry + REGISTRY + register + to_openai/anthropic_*_spec
│   ├── data/
│   │   ├── __init__.py
│   │   └── tools.py            # 7 read-only tools
│   ├── analytics/
│   │   ├── __init__.py
│   │   └── tools.py            # 6 pure-compute tools
│   └── exec/
│       ├── __init__.py
│       └── tools.py            # 3 exec tools (paper-only)
└── tests/
    ├── test_protocol.py        # protocol + typed-error catch behaviour
    ├── test_registry.py        # registration, retrieval, JSON schema spec helpers
    ├── test_data_tools.py      # all 7 data tools + entity.resolve typed errors
    ├── test_analytics_tools.py # all 6 analytics tools
    └── test_exec_tools.py      # all 3 exec tools, paper-mode gate, get_order_status
```

## Protocol — see CONTRACTS.md §8 for the canonical version.

Subclasses MUST override `_run`; `BaseTool.__call__` is the framework-provided wrapper that adds:

1. **OTel span** `tool.<name>` per call with attributes `tool.args_size`, `tool.result_size`, `tool.duration_ns`, `tool.ok`, `tool.error_type` — the orchestrator aggregates these for cost tracking.
2. **Typed-error handling** — `ToolError` subclasses (raised inside `_run`) are caught and surfaced as `output_model(ok=False, error=str(exc), error_type=type(exc).__name__)`.  Untyped exceptions propagate (programming errors stay visible).

Overriding `__call__` directly bypasses observability and is therefore disallowed by convention; tests in `test_protocol.py` enforce this.

## Tool inventory

### `data/tools.py` (7 tools)

| name                     | purpose                                                                                          |
|--------------------------|--------------------------------------------------------------------------------------------------|
| `data.get_bars`          | OHLCV bars for `(symbol, freq)` over `[start_ns, end_ns)`                                        |
| `data.get_quote`         | Most-recent 1-min bar close as a quote proxy                                                     |
| `data.get_trades`        | Raw tick trades over a window with `limit` cap                                                   |
| `data.get_universe`      | List of active in-universe symbols                                                               |
| `data.get_positions`     | Latest position snapshots from `ord.positions` Redis stream, optionally filtered by strategy_id  |
| `data.get_features`      | Online feature snapshot from Redis hash `features:online:<symbol>` (populated by TASK-016)       |
| `entity.resolve`         | Free-text → canonical symbol; **raises `NotInUniverse`** on miss (the gate vs. hallucination)    |

### `analytics/tools.py` (6 tools, all PIT-safe)

| name                            | purpose                                                                       |
|---------------------------------|-------------------------------------------------------------------------------|
| `analytics.compute_returns`     | Log-return series from the last N bars ending at `end_ns`                     |
| `analytics.compute_vol`         | Realised volatility, annualised by `sqrt(bars_per_year)`                      |
| `analytics.compute_correlation` | Pearson correlation of two log-return series                                  |
| `analytics.compute_vwap`        | VWAP from bars (uses stored `vwap` field, falls back to `(H+L+C)/3`)          |
| `analytics.compute_sharpe`      | Annualised Sharpe ratio with optional `risk_free_rate_annual` (FP-noise safe) |
| `analytics.compute_drawdown`    | Max peak-to-trough drawdown with `peak_index` and `trough_index`              |

### `exec/tools.py` (3 tools — paper-only in v1)

| name                  | purpose                                                                                  |
|-----------------------|------------------------------------------------------------------------------------------|
| `exec.submit_order`   | Build an `OrderIntent` and `xadd` a self-describing JSON envelope to `ord.orders`        |
| `exec.cancel_order`   | Publish a `cancel_request` to `ord.orders` for the named order                           |
| `exec.get_order_status` | Scan `ord.orders` newest-first via `xrevrange` for the most-recent state of an order   |

`submit_order` and `cancel_order` raise `PaperOnlyExec` when `TRADING_MODE != "paper"`; `get_order_status` is read-only and unrestricted.

## Typed errors (`fincept_tools.errors`)

```python
class ToolError(FinceptError):           ...   # base
class NotInUniverse(ToolError):          ...   # entity.resolve gate
class PaperOnlyExec(ToolError):          ...   # exec gate while live is locked
class ToolValidationError(ToolError):    ...   # tool-internal post-condition failure
class ToolBackendError(ToolError):       ...   # downstream DB/Redis/HTTP failure
```

## Registry helpers

```python
from fincept_tools.registry import REGISTRY, to_openai_function_spec, to_anthropic_tool_spec

REGISTRY.list()          # → list of OpenAI function-call dicts (canonical signature per CONTRACTS §8)
REGISTRY.list_meta()     # → list of ToolMeta for typed introspection
to_openai_function_spec(tool)   # OpenAI shape:  {"type": "function", "function": {...}}
to_anthropic_tool_spec(tool)    # Anthropic shape: {"name": ..., "input_schema": ...}
```

## Out of scope (deferred)

- Live execution (Phase H gate; `PaperOnlyExec` raised until then).
- Tool authentication / per-tenant ACLs (Phase U / H).
- Streaming tools (subscribe-style) — v1 is request/response only.
- Tool composition / chaining helpers — agents call tools directly.

## Done when

- [x] Files exist and the package imports cleanly.
- [x] `uv run pytest libs/fincept-tools` is green — **81 / 81 passed locally** (no DB/Redis required; mocks).
- [x] `uv run mypy --strict libs/fincept-tools/src` is green — **10 source files, 0 errors**.
- [x] `uv run ruff check libs/fincept-tools` is green.
- [x] `uv run ruff format --check libs/fincept-tools` is green.
- [x] `REGISTRY.list()` returns ≥ 16 tools, each with valid OpenAI-format JSON schema.
- [x] Round-trip works: `register → retrieve → call → typed result` (covered by `test_round_trip_register_retrieve_call`).
- [x] Typed-error contract enforced by tests (`test_base_tool_catches_typed_error_and_returns_ok_false`, `test_base_tool_does_not_catch_untyped_exceptions`).
- [x] CONTRACTS §8 updated to add `error_type` field and document the typed-error / cost-tracking contract.

## Key implementation notes

- `ord.orders` carries a self-describing JSON envelope (`event_type`, `order_id`, `state`, `ts_event`, `payload`).  The `Producer` from `fincept-bus` is typed for market-data Events only; orders write through raw `xadd` because `OrderIntent` is intentionally not part of the `EventPayload` union.
- `data.get_features` reads from `features:online:<symbol>` Redis hashes that the feature service (TASK-016) will populate.  Until then it returns an empty `features` dict — by design, callers MUST treat absence as "no signal" rather than zero.
- All workspace packages now ship a `py.typed` marker so cross-package type inference works under `mypy --strict`.
