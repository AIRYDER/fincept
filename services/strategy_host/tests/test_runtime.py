"""
Tests for ``strategy_host.runtime.LiveStrategyContext``.

The context is a thin per-strategy state holder: it implements the
StrategyContext protocol so strategy hooks can submit / cancel / log
without knowing about Redis, plus runner-only helpers
(``drain_submits``, ``update_position``).

These tests verify the small contract:

  * submit returns the order_id and queues the intent
  * drain pops + returns in submission order, leaves the queue empty
  * update_position respects the strategy_id filter
  * cancel / get_feature stubs don't raise
  * log forwards strategy_id to the bound logger
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pytest

from fincept_core.schemas import (
    OrderIntent,
    OrderType,
    Position,
    Side,
    TimeInForce,
    Venue,
)
from strategy_host.runtime import LiveStrategyContext

# --------------------------------------------------------------------------- #
# Helpers + fixtures                                                          #
# --------------------------------------------------------------------------- #


class RecordingLogger:
    """Minimal stand-in for a structlog BoundLogger.

    Records every (method, msg, kwargs) call so tests can assert on
    log output without depending on structlog's API surface.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, msg: str, **kwargs: Any) -> None:
        self.entries.append(("info", msg, kwargs))

    def warning(self, msg: str, **kwargs: Any) -> None:
        self.entries.append(("warning", msg, kwargs))

    def error(self, msg: str, **kwargs: Any) -> None:
        self.entries.append(("error", msg, kwargs))


@pytest.fixture
def rec_log() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def ctx(rec_log: RecordingLogger) -> LiveStrategyContext:
    return LiveStrategyContext(strategy_id="alpha", log=rec_log)


def _intent(
    *,
    strategy_id: str = "alpha",
    symbol: str = "BTC-USD",
    side: Side = Side.BUY,
    qty: str = "0.1",
    order_id: str = "order-1",
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="dec-1",
        ts_event=1_000_000_000,
        strategy_id=strategy_id,
        symbol=symbol,
        venue=Venue.PAPER,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
        time_in_force=TimeInForce.GTC,
    )


def _position(
    *,
    strategy_id: str = "alpha",
    symbol: str = "BTC-USD",
    qty: str = "0.5",
) -> Position:
    return Position(
        strategy_id=strategy_id,
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal("100"),
        updated_at=0,
    )


# --------------------------------------------------------------------------- #
# Submit / drain                                                              #
# --------------------------------------------------------------------------- #


class TestSubmit:
    def test_submit_returns_order_id(
        self, ctx: LiveStrategyContext
    ) -> None:
        intent = _intent(order_id="abc-123")
        assert ctx.submit(intent) == "abc-123"

    def test_submit_queues_for_drain(
        self, ctx: LiveStrategyContext
    ) -> None:
        intent_a = _intent(order_id="a")
        intent_b = _intent(order_id="b")
        ctx.submit(intent_a)
        ctx.submit(intent_b)
        drained = ctx.drain_submits()
        # FIFO: the first submitted is first drained.
        assert [i.order_id for i in drained] == ["a", "b"]

    def test_drain_clears_queue(self, ctx: LiveStrategyContext) -> None:
        ctx.submit(_intent(order_id="a"))
        ctx.drain_submits()
        # Subsequent drain returns empty -- no double-publish risk.
        assert ctx.drain_submits() == []

    def test_drain_returns_independent_list(
        self, ctx: LiveStrategyContext
    ) -> None:
        # The runner iterates the drained list while the strategy
        # may submit again on the next event.  The drained list
        # MUST be detached from the internal queue so the runner's
        # iteration isn't disrupted by concurrent (in this case
        # serial) submits.
        ctx.submit(_intent(order_id="a"))
        drained = ctx.drain_submits()
        ctx.submit(_intent(order_id="b"))
        # ``drained`` only contains the first; the second is on the
        # internal queue waiting for the next drain.
        assert [i.order_id for i in drained] == ["a"]
        assert [i.order_id for i in ctx.drain_submits()] == ["b"]

    def test_drain_empty_on_fresh_ctx(
        self, ctx: LiveStrategyContext
    ) -> None:
        assert ctx.drain_submits() == []


# --------------------------------------------------------------------------- #
# Positions                                                                   #
# --------------------------------------------------------------------------- #


class TestPositions:
    def test_update_installs_matching_strategy(
        self, ctx: LiveStrategyContext
    ) -> None:
        pos = _position(strategy_id="alpha", symbol="BTC-USD", qty="0.5")
        ctx.update_position(pos)
        assert ctx.positions["BTC-USD"] == pos

    def test_update_ignores_other_strategy(
        self, ctx: LiveStrategyContext
    ) -> None:
        # Defence-in-depth: even if the caller forgets to filter
        # upstream, the context itself rejects positions for a
        # different strategy_id.  This means a strategy's positions
        # dict is always trustworthy.
        ctx.update_position(_position(strategy_id="beta", symbol="BTC-USD"))
        assert ctx.positions == {}

    def test_update_overwrites_prior_position(
        self, ctx: LiveStrategyContext
    ) -> None:
        ctx.update_position(_position(qty="0.5"))
        ctx.update_position(_position(qty="0.7"))
        assert ctx.positions["BTC-USD"].quantity == Decimal("0.7")


# --------------------------------------------------------------------------- #
# Stub methods                                                                #
# --------------------------------------------------------------------------- #


class TestStubs:
    def test_cancel_does_not_raise(
        self, ctx: LiveStrategyContext, rec_log: RecordingLogger
    ) -> None:
        # F3 stub: log only, no exception.
        ctx.cancel("any-order-id")
        # Verify the warning landed -- this is the only signal an
        # operator gets that cancel was attempted.
        assert any(
            entry[1] == "strategy.cancel_unsupported"
            for entry in rec_log.entries
        )

    def test_get_feature_returns_none(
        self, ctx: LiveStrategyContext
    ) -> None:
        assert ctx.get_feature("rv_5m", "BTC-USD") is None


# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #


class TestLog:
    def test_log_stamps_strategy_id(
        self, ctx: LiveStrategyContext, rec_log: RecordingLogger
    ) -> None:
        ctx.log("strategy.foo_event", custom="value")
        assert rec_log.entries == [
            (
                "info",
                "strategy.foo_event",
                {"strategy_id": "alpha", "custom": "value"},
            )
        ]

    def test_log_with_stdlib_logger_does_not_raise(self) -> None:
        # The protocol allows passing any logger with an .info(msg,
        # **kwargs) shape.  A stdlib Logger ignores **kwargs except
        # ``extra=`` -- which is fine for tests / fallback use.
        ctx = LiveStrategyContext(
            strategy_id="alpha", log=logging.getLogger("test")
        )
        ctx.log("plain_msg", a=1)


# --------------------------------------------------------------------------- #
# Initial state                                                               #
# --------------------------------------------------------------------------- #


def test_initial_state(rec_log: RecordingLogger) -> None:
    ctx = LiveStrategyContext(strategy_id="alpha", log=rec_log)
    assert ctx.strategy_id == "alpha"
    assert ctx.now_ns == 0
    assert ctx.positions == {}
    assert ctx.drain_submits() == []
