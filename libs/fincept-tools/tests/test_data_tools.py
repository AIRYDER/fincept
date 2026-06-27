"""Tests for fincept_tools.data tools.

These tests are self-contained — they mock fincept_db calls so no database
connection is required.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from fincept_tools.data.tools import (
    GetBarsInput,
    GetBarsTool,
    GetFeaturesInput,
    GetFeaturesTool,
    GetQuoteInput,
    GetQuoteTool,
    GetTradesInput,
    GetTradesTool,
    GetUniverseInput,
    GetUniverseTool,
    ResolveEntityInput,
    ResolveEntityTool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(
    symbol: str = "BTC-USD",
    freq: str = "1d",
    close: str = "50000.00",
    ts_event: int = 1_700_000_000_000_000_000,
    volume: str = "100.0",
    vwap: str | None = None,
) -> Any:
    from fincept_core.schemas import AssetClass, BarEvent, Venue

    return BarEvent(
        venue=Venue.BINANCE,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_event,
        freq=freq,
        open=Decimal("49000"),
        high=Decimal("51000"),
        low=Decimal("48500"),
        close=Decimal(close),
        volume=Decimal(volume),
        trades=1000,
        vwap=Decimal(vwap) if vwap else None,
    )


# ---------------------------------------------------------------------------
# GetBarsInput validation
# ---------------------------------------------------------------------------


def test_get_bars_input_valid() -> None:
    inp = GetBarsInput(symbol="BTC-USD", freq="1h", start_ns=0, end_ns=1000)
    assert inp.symbol == "BTC-USD"
    assert inp.freq == "1h"


def test_get_bars_input_invalid_freq() -> None:
    with pytest.raises(ValidationError):
        GetBarsInput(symbol="BTC-USD", freq="5m", start_ns=0, end_ns=1000)


def test_get_bars_input_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        GetBarsInput(  # type: ignore[call-arg]
            symbol="BTC-USD", freq="1d", start_ns=0, end_ns=1000, extra_field="nope"
        )


# ---------------------------------------------------------------------------
# GetBarsTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_bars_tool_returns_bars() -> None:
    bars = [_make_bar(ts_event=i * 1_000_000) for i in range(3)]

    with patch("fincept_tools.data.tools.read_bars", new_callable=AsyncMock, return_value=bars):
        tool = GetBarsTool()
        result = await tool(GetBarsInput(symbol="BTC-USD", freq="1d", start_ns=0, end_ns=9999999))

    assert result.ok is True
    assert len(result.bars) == 3
    assert result.bars[0]["symbol"] == "BTC-USD"


@pytest.mark.asyncio
async def test_get_bars_tool_returns_error_on_db_failure() -> None:
    with patch(
        "fincept_tools.data.tools.read_bars",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db error"),
    ):
        tool = GetBarsTool()
        result = await tool(GetBarsInput(symbol="X", freq="1m", start_ns=0, end_ns=1))

    assert result.ok is False
    assert result.error_type == "ToolBackendError"
    assert "db error" in (result.error or "")


# ---------------------------------------------------------------------------
# GetQuoteTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_returns_latest_close() -> None:
    bar = _make_bar(close="55000.00")

    with patch("fincept_tools.data.tools.read_bars", new_callable=AsyncMock, return_value=[bar]):
        tool = GetQuoteTool()
        result = await tool(GetQuoteInput(symbol="BTC-USD"))

    assert result.ok is True
    assert result.symbol == "BTC-USD"
    assert result.close == "55000.00"


@pytest.mark.asyncio
async def test_get_quote_returns_error_when_no_bars() -> None:
    with patch("fincept_tools.data.tools.read_bars", new_callable=AsyncMock, return_value=[]):
        tool = GetQuoteTool()
        result = await tool(GetQuoteInput(symbol="UNKNOWN"))

    assert result.ok is False


# ---------------------------------------------------------------------------
# GetTradesTool
# ---------------------------------------------------------------------------


def _make_trade(
    symbol: str = "BTC-USD",
    ts_event: int = 1_700_000_000_000_000_000,
) -> Any:
    from fincept_core.schemas import AssetClass, Side, TradeEvent, Venue

    return TradeEvent(
        venue=Venue.BINANCE,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_event,
        price=Decimal("50000"),
        size=Decimal("0.1"),
        side=Side.BUY,
    )


@pytest.mark.asyncio
async def test_get_trades_returns_trades() -> None:
    trades = [_make_trade(ts_event=i * 1000) for i in range(5)]

    with patch("fincept_tools.data.tools.read_trades", new_callable=AsyncMock, return_value=trades):
        tool = GetTradesTool()
        result = await tool(GetTradesInput(symbol="BTC-USD", start_ns=0, end_ns=9999))

    assert result.ok is True
    assert len(result.trades) == 5
    assert result.truncated is False


@pytest.mark.asyncio
async def test_get_trades_respects_limit() -> None:
    trades = [_make_trade(ts_event=i * 1000) for i in range(10)]

    with patch("fincept_tools.data.tools.read_trades", new_callable=AsyncMock, return_value=trades):
        tool = GetTradesTool()
        result = await tool(GetTradesInput(symbol="BTC-USD", start_ns=0, end_ns=9999, limit=3))

    assert result.ok is True
    assert len(result.trades) == 3
    assert result.truncated is True


def test_get_trades_input_limit_bounds() -> None:
    with pytest.raises(ValidationError):
        GetTradesInput(symbol="X", start_ns=0, end_ns=1, limit=0)
    with pytest.raises(ValidationError):
        GetTradesInput(symbol="X", start_ns=0, end_ns=1, limit=100_001)


# ---------------------------------------------------------------------------
# GetUniverseTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_universe_returns_symbols() -> None:
    class FakeRow:
        symbol: str

        def __init__(self, sym: str) -> None:
            self.symbol = sym

    fake_rows = [FakeRow("BTC-USD"), FakeRow("ETH-USD")]

    # Result.scalars() and ScalarResult.all() are SYNC — must be MagicMock.
    scalars_obj = MagicMock()
    scalars_obj.all = MagicMock(return_value=fake_rows)
    result_obj = MagicMock()
    result_obj.scalars = MagicMock(return_value=scalars_obj)

    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(return_value=result_obj)

    class FakeScope:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    with patch("fincept_tools.data.tools.session_scope", return_value=FakeScope()):
        tool = GetUniverseTool()
        result = await tool(GetUniverseInput())

    assert result.ok is True
    assert "BTC-USD" in result.symbols
    assert "ETH-USD" in result.symbols


# ---------------------------------------------------------------------------
# ResolveEntityTool
# ---------------------------------------------------------------------------


def _resolve_session(scalar_value: Any) -> AsyncMock:
    """Build an AsyncMock session whose ``execute`` returns a Result whose
    sync ``scalar_one_or_none()`` yields ``scalar_value``."""
    result_obj = MagicMock()
    result_obj.scalar_one_or_none = MagicMock(return_value=scalar_value)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_obj)
    return session


@pytest.mark.asyncio
async def test_resolve_entity_found() -> None:
    class FakeRow:
        symbol = "BTC-USD"
        active = True

    fake_session = _resolve_session(FakeRow())

    class FakeScope:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    with patch("fincept_tools.data.tools.session_scope", return_value=FakeScope()):
        tool = ResolveEntityTool()
        result = await tool(ResolveEntityInput(query="BTC-USD"))

    assert result.ok is True
    assert result.symbol == "BTC-USD"
    assert result.in_universe is True


@pytest.mark.asyncio
async def test_resolve_entity_not_found_raises_not_in_universe() -> None:
    """On miss, entity.resolve raises NotInUniverse → surfaces as typed error."""
    fake_session = _resolve_session(None)

    class FakeScope:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    with patch("fincept_tools.data.tools.session_scope", return_value=FakeScope()):
        tool = ResolveEntityTool()
        result = await tool(ResolveEntityInput(query="INVALID-XXX"))

    assert result.ok is False
    assert result.error_type == "NotInUniverse"
    assert "INVALID-XXX" in (result.error or "")
    # Output fields stay at defaults on a typed-error return
    assert result.in_universe is False
    assert result.symbol is None


@pytest.mark.asyncio
async def test_resolve_entity_inactive_symbol_raises_not_in_universe() -> None:
    """Inactive symbols are also misses — same NotInUniverse semantics."""

    class FakeRow:
        symbol = "DELIST"
        active = False

    fake_session = _resolve_session(FakeRow())

    class FakeScope:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    with patch("fincept_tools.data.tools.session_scope", return_value=FakeScope()):
        tool = ResolveEntityTool()
        result = await tool(ResolveEntityInput(query="DELIST"))

    assert result.ok is False
    assert result.error_type == "NotInUniverse"
    assert result.in_universe is False


@pytest.mark.asyncio
async def test_resolve_entity_strips_dollar_prefix() -> None:
    """LLMs frequently emit '$AAPL'; the tool should strip the leading $."""

    class FakeRow:
        symbol = "AAPL"
        active = True

    fake_session = _resolve_session(FakeRow())

    class FakeScope:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    with patch("fincept_tools.data.tools.session_scope", return_value=FakeScope()):
        tool = ResolveEntityTool()
        result = await tool(ResolveEntityInput(query="$aapl"))

    assert result.ok is True
    assert result.in_universe is True
    assert result.symbol == "AAPL"


# ---------------------------------------------------------------------------
# GetFeaturesTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_features_returns_decoded_hash() -> None:
    """hgetall bytes → decoded str dict."""
    fake_redis = AsyncMock()
    fake_redis.hgetall = AsyncMock(
        return_value={
            b"ret_5m": b"0.0123",
            b"rv_30m": b"0.0044",
            b"__ts_event__": b"1700000000000000000",
        }
    )
    fake_redis.aclose = AsyncMock()

    with patch("fincept_tools.data.tools.get_redis", return_value=fake_redis):
        tool = GetFeaturesTool()
        result = await tool(GetFeaturesInput(symbol="BTC-USD"))

    assert result.ok is True
    assert result.features == {"ret_5m": "0.0123", "rv_30m": "0.0044"}
    assert result.ts_event == 1_700_000_000_000_000_000


@pytest.mark.asyncio
async def test_get_features_filters_by_requested_names() -> None:
    fake_redis = AsyncMock()
    fake_redis.hgetall = AsyncMock(return_value={b"a": b"1", b"b": b"2", b"c": b"3"})
    fake_redis.aclose = AsyncMock()

    with patch("fincept_tools.data.tools.get_redis", return_value=fake_redis):
        tool = GetFeaturesTool()
        result = await tool(GetFeaturesInput(symbol="BTC-USD", feature_names=["a", "c"]))

    assert result.ok is True
    assert result.features == {"a": "1", "c": "3"}


@pytest.mark.asyncio
async def test_get_features_empty_when_no_data() -> None:
    fake_redis = AsyncMock()
    fake_redis.hgetall = AsyncMock(return_value={})
    fake_redis.aclose = AsyncMock()

    with patch("fincept_tools.data.tools.get_redis", return_value=fake_redis):
        tool = GetFeaturesTool()
        result = await tool(GetFeaturesInput(symbol="NEW-SYM"))

    assert result.ok is True
    assert result.features == {}
    assert result.ts_event is None


@pytest.mark.asyncio
async def test_get_features_redis_failure_returns_typed_error() -> None:
    with patch(
        "fincept_tools.data.tools.get_redis", side_effect=ConnectionRefusedError("no redis")
    ):
        tool = GetFeaturesTool()
        result = await tool(GetFeaturesInput(symbol="X"))

    assert result.ok is False
    assert result.error_type == "ToolBackendError"
    assert "no redis" in (result.error or "").lower()
