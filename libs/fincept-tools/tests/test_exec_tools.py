"""Tests for fincept_tools.exec tools.

Uses fakeredis + monkeypatching so no live Redis connection is required.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from fincept_core.schemas import OrderType, Side
from fincept_tools.exec import (
    CancelOrderInput,
    CancelOrderTool,
    GetOrderStatusInput,
    GetOrderStatusTool,
    SubmitOrderInput,
    SubmitOrderTool,
)
from fincept_tools.registry import REGISTRY

# ---------------------------------------------------------------------------
# SubmitOrderInput validation
# ---------------------------------------------------------------------------


def test_submit_order_input_valid() -> None:
    inp = SubmitOrderInput(
        decision_id="d1",
        strategy_id="s1",
        symbol="BTC-USD",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.5"),
    )
    assert inp.venue.value == "paper"
    assert inp.time_in_force.value == "gtc"


def test_submit_order_input_quantity_must_be_positive() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitOrderInput(
            decision_id="d1",
            strategy_id="s1",
            symbol="BTC-USD",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0"),
        )


def test_submit_order_input_forbids_extra() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitOrderInput(  # type: ignore[call-arg]
            decision_id="d1",
            strategy_id="s1",
            symbol="BTC-USD",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            rogue_field="oops",
        )


# ---------------------------------------------------------------------------
# SubmitOrderTool — paper mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_order_paper_mode_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """In paper mode, the tool should publish to Redis and return an order_id."""
    from fincept_core.config import Settings

    Settings.clear_cache()
    monkeypatch.setenv("FINCEPT_TRADING_MODE", "paper")
    monkeypatch.setenv("FINCEPT_REDIS_URL", "redis://localhost:6379/0")
    Settings.clear_cache()

    fake_redis = AsyncMock()
    fake_redis.xadd = AsyncMock(return_value=b"1-0")
    fake_redis.aclose = AsyncMock()

    with patch("fincept_tools.exec.tools.get_redis", return_value=fake_redis):
        tool = SubmitOrderTool()
        result = await tool(
            SubmitOrderInput(
                decision_id="dec-001",
                strategy_id="strat-001",
                symbol="BTC-USD",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.01"),
            )
        )

    assert result.ok is True
    assert result.order_id is not None
    assert len(result.order_id) == 26  # ULID length


@pytest.mark.asyncio
async def test_submit_order_live_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """In live mode, the tool must refuse with ok=False."""
    from fincept_core.config import Settings

    Settings.clear_cache()
    monkeypatch.setenv("FINCEPT_TRADING_MODE", "live")
    Settings.clear_cache()

    tool = SubmitOrderTool()
    result = await tool(
        SubmitOrderInput(
            decision_id="d1",
            strategy_id="s1",
            symbol="ETH-USD",
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=Decimal("1.0"),
            limit_price=Decimal("2000"),
        )
    )

    assert result.ok is False
    assert result.error_type == "PaperOnlyExec"
    assert "paper-only" in (result.error or "").lower()
    assert result.order_id is None

    # Cleanup
    Settings.clear_cache()
    monkeypatch.delenv("FINCEPT_TRADING_MODE", raising=False)
    Settings.clear_cache()


@pytest.mark.asyncio
async def test_submit_order_redis_failure_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from fincept_core.config import Settings

    Settings.clear_cache()
    monkeypatch.setenv("FINCEPT_TRADING_MODE", "paper")
    Settings.clear_cache()

    with patch(
        "fincept_tools.exec.tools.get_redis", side_effect=ConnectionRefusedError("no redis")
    ):
        tool = SubmitOrderTool()
        result = await tool(
            SubmitOrderInput(
                decision_id="d1",
                strategy_id="s1",
                symbol="BTC-USD",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0.01"),
            )
        )

    assert result.ok is False
    assert result.error_type == "ToolBackendError"
    assert "no redis" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# CancelOrderTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_order_paper_mode_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from fincept_core.config import Settings

    Settings.clear_cache()
    monkeypatch.setenv("FINCEPT_TRADING_MODE", "paper")
    Settings.clear_cache()

    fake_redis = AsyncMock()
    fake_redis.xadd = AsyncMock(return_value=b"1234-0")
    fake_redis.aclose = AsyncMock()

    with patch("fincept_tools.exec.tools.get_redis", return_value=fake_redis):
        tool = CancelOrderTool()
        result = await tool(
            CancelOrderInput(
                order_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                strategy_id="strat-001",
                reason="risk_limit_breach",
            )
        )

    assert result.ok is True
    assert result.cancel_id is not None
    assert len(result.cancel_id) == 26  # ULID


@pytest.mark.asyncio
async def test_cancel_order_live_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from fincept_core.config import Settings

    Settings.clear_cache()
    monkeypatch.setenv("FINCEPT_TRADING_MODE", "live")
    Settings.clear_cache()

    tool = CancelOrderTool()
    result = await tool(
        CancelOrderInput(
            order_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            strategy_id="s1",
        )
    )

    assert result.ok is False
    assert result.error_type == "PaperOnlyExec"
    assert "paper-only" in (result.error or "").lower()

    Settings.clear_cache()
    monkeypatch.delenv("FINCEPT_TRADING_MODE", raising=False)
    Settings.clear_cache()


# ---------------------------------------------------------------------------
# Registry presence check
# ---------------------------------------------------------------------------


def test_exec_tools_in_registry() -> None:
    assert "exec.submit_order" in REGISTRY
    assert "exec.cancel_order" in REGISTRY
    assert "exec.get_order_status" in REGISTRY


# ---------------------------------------------------------------------------
# GetOrderStatusTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_order_status_finds_recent_match() -> None:
    """xrevrange returns newest-first; tool picks the first matching order_id."""
    target = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    fake_redis = AsyncMock()
    fake_redis.xrevrange = AsyncMock(
        return_value=[
            (
                b"1-0",
                {
                    b"order_id": target.encode(),
                    b"state": b"filled",
                    b"ts_event": b"1700000000000000000",
                },
            ),
            (
                b"0-0",
                {
                    b"order_id": b"OTHER",
                    b"state": b"submitted",
                },
            ),
        ]
    )
    fake_redis.aclose = AsyncMock()

    with patch("fincept_tools.exec.tools.get_redis", return_value=fake_redis):
        tool = GetOrderStatusTool()
        result = await tool(GetOrderStatusInput(order_id=target))

    assert result.ok is True
    assert result.order_id == target
    assert result.state == "filled"
    assert result.ts_event == 1_700_000_000_000_000_000
    assert result.raw is not None


@pytest.mark.asyncio
async def test_get_order_status_returns_none_state_when_not_found() -> None:
    fake_redis = AsyncMock()
    fake_redis.xrevrange = AsyncMock(
        return_value=[(b"0-0", {b"order_id": b"DIFFERENT", b"state": b"filled"})]
    )
    fake_redis.aclose = AsyncMock()

    with patch("fincept_tools.exec.tools.get_redis", return_value=fake_redis):
        tool = GetOrderStatusTool()
        result = await tool(GetOrderStatusInput(order_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"))

    assert result.ok is True
    assert result.state is None
    assert result.raw is None


@pytest.mark.asyncio
async def test_get_order_status_redis_failure_returns_typed_error() -> None:
    with patch("fincept_tools.exec.tools.get_redis", side_effect=ConnectionError("no redis")):
        tool = GetOrderStatusTool()
        result = await tool(GetOrderStatusInput(order_id="x"))

    assert result.ok is False
    assert result.error_type == "ToolBackendError"


def test_get_order_status_input_scan_limit_bounds() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GetOrderStatusInput(order_id="x", scan_limit=0)
    with pytest.raises(ValidationError):
        GetOrderStatusInput(order_id="x", scan_limit=100_000)
