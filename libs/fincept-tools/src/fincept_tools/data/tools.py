"""
fincept_tools.data.tools — read-only data-access tool implementations.

Each tool subclasses ``BaseTool`` and overrides ``_run`` (NOT ``__call__``);
``BaseTool.__call__`` provides OTel tracing + typed-error handling.

Tools in this module:
  - data.get_bars       — OHLCV bars over a window
  - data.get_quote      — most-recent close as a quote
  - data.get_trades     — raw tick trades
  - data.get_universe   — symbols in the active universe
  - data.get_positions  — current open positions (from ord.positions stream)
  - data.get_features   — point-in-time feature values from the online store
  - entity.resolve      — free-text → canonical universe symbol; raises
                          NotInUniverse if not found

All read-only — never write.  All PIT-safe where ``end_ns`` applies.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import Field
from redis.asyncio import Redis
from sqlalchemy import or_, select

from fincept_core.config import get_settings
from fincept_core.schemas import Position
from fincept_db.bars import read_bars
from fincept_db.engine import session_scope
from fincept_db.models import UniverseSymbol
from fincept_db.ticks import read_trades
from fincept_tools.errors import NotInUniverse, ToolBackendError
from fincept_tools.protocol import BaseTool, ToolInput, ToolOutput
from fincept_tools.registry import register

# ---------------------------------------------------------------------------
# data.get_bars
# ---------------------------------------------------------------------------


class GetBarsInput(ToolInput):
    """Input for data.get_bars."""

    symbol: str = Field(description="Canonical symbol, e.g. BTC-USD or AAPL.")
    freq: str = Field(
        pattern=r"^(1m|1h|1d)$",
        description="Bar frequency: '1m', '1h', or '1d'.",
    )
    start_ns: int = Field(description="Window start, inclusive, nanoseconds since Unix epoch.")
    end_ns: int = Field(description="Window end, exclusive, nanoseconds since Unix epoch.")
    venue: str | None = Field(
        default=None,
        description="Optional venue filter, e.g. 'binance'. Omit to return all venues.",
    )


class GetBarsOutput(ToolOutput):
    """Output for data.get_bars."""

    bars: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of BarEvent dicts serialised with model_dump(mode='json').",
    )


class GetBarsTool(BaseTool):
    name = "data.get_bars"
    description = (
        "Fetch OHLCV bars for a symbol over the half-open window [start_ns, end_ns). "
        "freq must be '1m', '1h', or '1d'. Returns up to ~10 000 rows; use a tighter "
        "window if you need less data."
    )
    input_model = GetBarsInput
    output_model = GetBarsOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, GetBarsInput)
        try:
            bars = await read_bars(
                payload.symbol,
                payload.freq,
                payload.start_ns,
                payload.end_ns,
                venue=payload.venue,
            )
        except Exception as exc:
            raise ToolBackendError(f"read_bars failed: {exc}") from exc

        return GetBarsOutput(bars=[b.model_dump(mode="json") for b in bars])


register(GetBarsTool())


# ---------------------------------------------------------------------------
# data.get_quote  (latest close as a quote proxy)
# ---------------------------------------------------------------------------


class GetQuoteInput(ToolInput):
    """Input for data.get_quote."""

    symbol: str = Field(description="Canonical symbol, e.g. BTC-USD.")
    venue: str | None = Field(default=None, description="Optional venue filter.")


class GetQuoteOutput(ToolOutput):
    """Output for data.get_quote."""

    symbol: str | None = None
    close: str | None = Field(
        default=None,
        description="Most-recent 1-minute bar close price as decimal string.",
    )
    ts_event: int | None = None


class GetQuoteTool(BaseTool):
    name = "data.get_quote"
    description = (
        "Fetch the most-recent available price (last 1-minute bar close) for a symbol. "
        "Returns ok=False with error_type='ToolBackendError' if no recent bars are found. "
        "For a live tick subscribe to md.trades directly."
    )
    input_model = GetQuoteInput
    output_model = GetQuoteOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, GetQuoteInput)
        import time

        now = time.time_ns()
        one_hour_ns = 3_600_000_000_000
        try:
            bars = await read_bars(
                payload.symbol,
                "1m",
                now - one_hour_ns,
                now,
                venue=payload.venue,
            )
        except Exception as exc:
            raise ToolBackendError(f"read_bars failed: {exc}") from exc

        if not bars:
            raise ToolBackendError(f"no recent bars found for {payload.symbol!r}")
        last = bars[-1]
        return GetQuoteOutput(
            symbol=last.symbol,
            close=str(last.close),
            ts_event=last.ts_event,
        )


register(GetQuoteTool())


# ---------------------------------------------------------------------------
# data.get_trades
# ---------------------------------------------------------------------------


class GetTradesInput(ToolInput):
    """Input for data.get_trades."""

    symbol: str = Field(description="Canonical symbol, e.g. BTC-USD.")
    start_ns: int = Field(description="Window start, inclusive, nanoseconds since Unix epoch.")
    end_ns: int = Field(description="Window end, exclusive, nanoseconds since Unix epoch.")
    venue: str | None = Field(default=None, description="Optional venue filter.")
    limit: int = Field(
        default=1000,
        ge=1,
        le=50_000,
        description="Maximum number of trades to return.",
    )


class GetTradesOutput(ToolOutput):
    """Output for data.get_trades."""

    trades: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of TradeEvent dicts serialised with model_dump(mode='json').",
    )
    truncated: bool = Field(
        default=False,
        description="True when the result was capped by the limit parameter.",
    )


class GetTradesTool(BaseTool):
    name = "data.get_trades"
    description = (
        "Fetch raw tick trades for a symbol over the half-open window [start_ns, end_ns). "
        "Results are ordered by ts_event ascending.  Use the limit parameter to bound "
        "response size."
    )
    input_model = GetTradesInput
    output_model = GetTradesOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, GetTradesInput)
        try:
            trades = await read_trades(
                payload.symbol,
                payload.start_ns,
                payload.end_ns,
                venue=payload.venue,
            )
        except Exception as exc:
            raise ToolBackendError(f"read_trades failed: {exc}") from exc

        truncated = len(trades) > payload.limit
        trades = trades[: payload.limit]
        return GetTradesOutput(
            trades=[t.model_dump(mode="json") for t in trades],
            truncated=truncated,
        )


register(GetTradesTool())


# ---------------------------------------------------------------------------
# data.get_universe
# ---------------------------------------------------------------------------


class GetUniverseInput(ToolInput):
    """Input for data.get_universe (no required fields)."""

    active_only: bool = Field(
        default=True,
        description="If True (default), return only symbols marked active in the universe table.",
    )


class GetUniverseOutput(ToolOutput):
    """Output for data.get_universe."""

    symbols: list[str] = Field(
        default_factory=list,
        description="Canonical symbols currently in the trading universe.",
    )


class GetUniverseTool(BaseTool):
    name = "data.get_universe"
    description = (
        "Return the list of symbols in the trading universe. "
        "Agents MUST verify a symbol is in the universe before generating signals."
    )
    input_model = GetUniverseInput
    output_model = GetUniverseOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, GetUniverseInput)
        try:
            async with session_scope() as s:
                q = select(UniverseSymbol)
                if payload.active_only:
                    q = q.where(UniverseSymbol.active.is_(True))
                rows = (await s.execute(q)).scalars().all()
                return GetUniverseOutput(symbols=[r.symbol for r in rows])
        except Exception as exc:
            raise ToolBackendError(f"universe lookup failed: {exc}") from exc


register(GetUniverseTool())


# ---------------------------------------------------------------------------
# data.get_positions
# ---------------------------------------------------------------------------


class GetPositionsInput(ToolInput):
    """Input for data.get_positions."""

    strategy_id: str | None = Field(
        default=None,
        description="Filter by strategy ID.  Omit to return all strategies.",
    )


class GetPositionsOutput(ToolOutput):
    """Output for data.get_positions."""

    positions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of Position dicts from the portfolio stream (latest snapshots).",
    )


class GetPositionsTool(BaseTool):
    name = "data.get_positions"
    description = (
        "Return current open positions, optionally filtered by strategy_id. "
        "Quantity is signed (negative = short).  Sourced from the ord.positions "
        "Redis stream snapshot — reflects fills processed by the OMS."
    )
    input_model = GetPositionsInput
    output_model = GetPositionsOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, GetPositionsInput)
        settings = get_settings()
        try:
            r: Redis[Any] = Redis.from_url(settings.REDIS_URL)
            try:
                raw_messages = await r.xrevrange("ord.positions", count=500)
            finally:
                await r.aclose()  # type: ignore[attr-defined]
        except Exception as exc:
            raise ToolBackendError(f"positions stream read failed: {exc}") from exc

        # Build latest snapshot per (strategy_id, symbol) pair (xrevrange gives newest-first).
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for _msg_id, fields in raw_messages or []:
            decoded: dict[str, Any] = {
                k.decode() if isinstance(k, bytes) else k: (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in fields.items()
            }
            sid = decoded.get("strategy_id", "")
            sym = decoded.get("symbol", "")
            key = (str(sid), str(sym))
            if key not in seen:
                seen[key] = decoded

        positions: list[dict[str, Any]] = []
        for (_sid, _sym), data in seen.items():
            try:
                pos = Position(
                    strategy_id=data["strategy_id"],
                    symbol=data["symbol"],
                    quantity=Decimal(data["quantity"]),
                    avg_cost=Decimal(data["avg_cost"]),
                    realized_pnl=Decimal(data.get("realized_pnl", "0")),
                    unrealized_pnl=Decimal(data.get("unrealized_pnl", "0")),
                    updated_at=int(data["updated_at"]),
                )
            except (KeyError, ValueError):
                continue  # skip malformed rows
            if payload.strategy_id is None or pos.strategy_id == payload.strategy_id:
                positions.append(pos.model_dump(mode="json"))

        return GetPositionsOutput(positions=positions)


register(GetPositionsTool())


# ---------------------------------------------------------------------------
# data.get_features  (online feature store; populated by services/features)
# ---------------------------------------------------------------------------


class GetFeaturesInput(ToolInput):
    """Input for data.get_features."""

    symbol: str = Field(description="Canonical symbol, e.g. BTC-USD.")
    feature_names: list[str] = Field(
        default_factory=list,
        description=(
            "Feature names to retrieve, e.g. ['ret_5m', 'rv_30m']. "
            "Empty list returns all available features for the symbol."
        ),
    )


class GetFeaturesOutput(ToolOutput):
    """Output for data.get_features."""

    features: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of feature_name → value (string-encoded so heterogeneous types "
            "round-trip cleanly through Redis hashes; callers parse as needed)."
        ),
    )
    ts_event: int | None = Field(
        default=None,
        description="Last-update timestamp (ns) of the snapshot, if available.",
    )


class GetFeaturesTool(BaseTool):
    name = "data.get_features"
    description = (
        "Return the latest online-feature snapshot for a symbol from the feature "
        "store (Redis hash 'features:online:<symbol>').  Returns an empty dict if "
        "the feature service has not yet written for this symbol — callers MUST "
        "treat absence as 'no signal' rather than zero."
    )
    input_model = GetFeaturesInput
    output_model = GetFeaturesOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, GetFeaturesInput)
        settings = get_settings()
        key = f"features:online:{payload.symbol}"
        try:
            r: Redis[Any] = Redis.from_url(settings.REDIS_URL)
            try:
                raw: dict[bytes | str, bytes | str] = await r.hgetall(key)
            finally:
                await r.aclose()  # type: ignore[attr-defined]
        except Exception as exc:
            raise ToolBackendError(f"feature store read failed: {exc}") from exc

        decoded: dict[str, str] = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in (raw or {}).items()
        }
        ts_str = decoded.pop("__ts_event__", None)
        ts_event = int(ts_str) if ts_str else None

        if payload.feature_names:
            decoded = {k: v for k, v in decoded.items() if k in payload.feature_names}

        return GetFeaturesOutput(features=decoded, ts_event=ts_event)


register(GetFeaturesTool())


# ---------------------------------------------------------------------------
# entity.resolve  (the gate against hallucinated tickers)
# ---------------------------------------------------------------------------


class ResolveEntityInput(ToolInput):
    """Input for entity.resolve."""

    query: str = Field(
        description=(
            "Free-text ticker symbol or company name to resolve, e.g. 'BTC-USD', "
            "'bitcoin', 'Apple Inc.', '$AAPL'.  Case-insensitive."
        )
    )


class ResolveEntityOutput(ToolOutput):
    """Output for entity.resolve."""

    symbol: str | None = Field(
        default=None,
        description="Canonical symbol on success; None on miss (with error_type='NotInUniverse').",
    )
    in_universe: bool = Field(
        default=False,
        description="True iff the symbol is active in the trading universe.",
    )


class ResolveEntityTool(BaseTool):
    name = "entity.resolve"
    description = (
        "Resolve a free-text query (ticker or company name) to a canonical in-universe "
        "symbol.  On miss, returns ok=False with error_type='NotInUniverse' — agents "
        "MUST check this before emitting trading signals to prevent hallucinated tickers."
    )
    input_model = ResolveEntityInput
    output_model = ResolveEntityOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, ResolveEntityInput)
        upper = payload.query.upper().strip()
        # Strip a leading '$' (LLMs often write '$AAPL').
        if upper.startswith("$"):
            upper = upper[1:]

        try:
            async with session_scope() as s:
                q = select(UniverseSymbol).where(
                    or_(
                        UniverseSymbol.symbol == upper,
                        UniverseSymbol.symbol == upper.replace(" ", "-"),
                    )
                )
                row = (await s.execute(q)).scalar_one_or_none()
        except Exception as exc:
            raise ToolBackendError(f"universe lookup failed: {exc}") from exc

        if row is not None and row.active:
            return ResolveEntityOutput(symbol=row.symbol, in_universe=True)
        # Miss → typed error so callers branch on error_type, not on string parsing.
        raise NotInUniverse(f"symbol not in active universe: {payload.query!r}")


register(ResolveEntityTool())
