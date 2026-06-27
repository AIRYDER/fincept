"""
fincept_tools.exec.tools — order-execution tool implementations (paper-only).

Each tool subclasses ``BaseTool`` and overrides ``_run``; ``BaseTool.__call__``
provides OTel tracing + typed-error handling.

Tools:
  - exec.submit_order      — publish an OrderIntent to ``ord.orders``
  - exec.cancel_order      — publish a cancel-request to ``ord.orders``
  - exec.get_order_status  — fetch the most-recent state of an order from the stream

Live execution is gated until Phase H.  Every tool checks
``settings.TRADING_MODE`` and raises ``PaperOnlyExec`` when the mode is not
``"paper"``.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_ORDERS
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.events import make_event
from fincept_core.ids import new_id
from fincept_core.schemas import (
    CancelRequest,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)
from fincept_tools.errors import PaperOnlyExec, ToolBackendError
from fincept_tools.protocol import BaseTool, ToolInput, ToolOutput
from fincept_tools.redis_client import get_redis
from fincept_tools.registry import register


def _ensure_paper_mode() -> None:
    """Raise PaperOnlyExec unless TRADING_MODE is 'paper'.

    Centralised so every exec tool gates identically; the BaseTool runner
    catches PaperOnlyExec and serialises as ok=False, error_type='PaperOnlyExec'.
    """
    mode = get_settings().TRADING_MODE.lower()
    if mode != "paper":
        raise PaperOnlyExec(f"exec tools are paper-only in v1; current TRADING_MODE={mode!r}")


# ---------------------------------------------------------------------------
# exec.submit_order
# ---------------------------------------------------------------------------


class SubmitOrderInput(ToolInput):
    """Input for exec.submit_order."""

    decision_id: str = Field(description="ULID of the orchestrator Decision that triggered this.")
    strategy_id: str = Field(description="Strategy that owns this order.")
    symbol: str = Field(description="Canonical symbol, e.g. BTC-USD.")
    side: Side = Field(description="'buy' or 'sell'.")
    order_type: OrderType = Field(description="'market', 'limit', 'stop', or 'stop_limit'.")
    quantity: Decimal = Field(gt=Decimal("0"), description="Order quantity (positive).")
    venue: Venue = Field(default=Venue.PAPER, description="Execution venue.  PAPER in v1.")
    limit_price: Decimal | None = Field(
        default=None,
        description="Required when order_type is 'limit' or 'stop_limit'.",
    )
    stop_price: Decimal | None = Field(
        default=None,
        description="Required when order_type is 'stop' or 'stop_limit'.",
    )
    time_in_force: TimeInForce = Field(
        default=TimeInForce.GTC,
        description="'gtc', 'ioc', 'fok', or 'day'.",
    )


class SubmitOrderOutput(ToolOutput):
    """Output for exec.submit_order."""

    order_id: str | None = Field(
        default=None,
        description="ULID of the submitted order, or None on error.",
    )


class SubmitOrderTool(BaseTool):
    name = "exec.submit_order"
    description = (
        "Submit an OrderIntent to the OMS via the ord.orders Redis stream. "
        "PAPER ONLY in v1 — raises PaperOnlyExec if TRADING_MODE != 'paper'. "
        "The OMS processes the intent and transitions it through the order lifecycle."
    )
    input_model = SubmitOrderInput
    output_model = SubmitOrderOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, SubmitOrderInput)
        _ensure_paper_mode()

        order_id = new_id()
        ts = now_ns()
        intent = OrderIntent(
            order_id=order_id,
            decision_id=payload.decision_id,
            ts_event=ts,
            strategy_id=payload.strategy_id,
            symbol=payload.symbol,
            venue=payload.venue,
            side=payload.side,
            order_type=payload.order_type,
            quantity=payload.quantity,
            limit_price=payload.limit_price,
            stop_price=payload.stop_price,
            time_in_force=payload.time_in_force,
        )

        try:
            r = get_redis()
            producer = Producer(r)
            event = make_event("order_intent", intent)
            await producer.publish(STREAM_ORDERS, event)
        except Exception as exc:
            raise ToolBackendError(f"order publish failed: {exc}") from exc

        return SubmitOrderOutput(order_id=order_id)


register(SubmitOrderTool())


# ---------------------------------------------------------------------------
# exec.cancel_order
# ---------------------------------------------------------------------------


class CancelOrderInput(ToolInput):
    """Input for exec.cancel_order."""

    order_id: str = Field(description="ULID of the order to cancel.")
    strategy_id: str = Field(
        description="Strategy that owns this order.  Used for authorisation in future versions."
    )
    reason: str = Field(
        default="agent_requested",
        description="Human-readable cancellation reason for the audit trail.",
    )


class CancelOrderOutput(ToolOutput):
    """Output for exec.cancel_order."""

    cancel_id: str | None = Field(
        default=None,
        description="ULID of the cancel-request message, or None on error.",
    )


class CancelOrderTool(BaseTool):
    name = "exec.cancel_order"
    description = (
        "Publish a cancel request for an existing order to the ord.orders stream. "
        "PAPER ONLY in v1.  The OMS will attempt to cancel; the final status is "
        "reflected in the order's state transitions in the stream."
    )
    input_model = CancelOrderInput
    output_model = CancelOrderOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, CancelOrderInput)
        _ensure_paper_mode()

        cancel_id = new_id()
        cancel_request = CancelRequest(
            cancel_id=cancel_id,
            order_id=payload.order_id,
            strategy_id=payload.strategy_id,
            ts_event=now_ns(),
            reason=payload.reason,
        )

        try:
            r = get_redis()
            producer = Producer(r)
            event = make_event("cancel_request", cancel_request)
            await producer.publish(STREAM_ORDERS, event)
        except Exception as exc:
            raise ToolBackendError(f"cancel publish failed: {exc}") from exc

        return CancelOrderOutput(cancel_id=cancel_id)


register(CancelOrderTool())


# ---------------------------------------------------------------------------
# exec.get_order_status
# ---------------------------------------------------------------------------


class GetOrderStatusInput(ToolInput):
    """Input for exec.get_order_status."""

    order_id: str = Field(description="ULID of the order to look up.")
    scan_limit: int = Field(
        default=2_000,
        ge=1,
        le=50_000,
        description=(
            "How many recent stream messages to scan (newest-first) before giving up. "
            "Tune higher for high-throughput strategies with deep histories."
        ),
    )


class GetOrderStatusOutput(ToolOutput):
    """Output for exec.get_order_status."""

    order_id: str | None = None
    state: str | None = Field(
        default=None,
        description=(
            "Most-recent status seen on the stream: 'submitted', 'partial_fill', "
            "'filled', 'cancelled', 'rejected', etc.  None if the order_id was "
            "not found within scan_limit messages."
        ),
    )
    ts_event: int | None = Field(
        default=None, description="Timestamp (ns) of the most-recent state observed."
    )
    raw: dict[str, str] | None = Field(
        default=None,
        description="Decoded raw fields of the most-recent matching message; None on miss.",
    )


class GetOrderStatusTool(BaseTool):
    name = "exec.get_order_status"
    description = (
        "Look up the most-recent state of an order by scanning the ord.orders Redis stream "
        "newest-first.  Returns None state if the order_id is not found within scan_limit "
        "messages (most callers should consult positions or the OMS state store for older orders)."
    )
    input_model = GetOrderStatusInput
    output_model = GetOrderStatusOutput

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, GetOrderStatusInput)
        # Read-only — no PaperOnlyExec gate; safe in any mode.
        try:
            r = get_redis()
            raw_messages = await r.xrevrange(STREAM_ORDERS, count=payload.scan_limit)
        except Exception as exc:
            raise ToolBackendError(f"order stream read failed: {exc}") from exc

        for _msg_id, fields in raw_messages or []:
            decoded: dict[str, str] = {
                (k.decode() if isinstance(k, bytes) else str(k)): (
                    v.decode() if isinstance(v, bytes) else str(v)
                )
                for k, v in fields.items()
            }
            if decoded.get("order_id") != payload.order_id:
                continue
            ts_str = decoded.get("ts_event")
            ts_event = int(ts_str) if ts_str and ts_str.isdigit() else None
            state = decoded.get("state") or decoded.get("status") or decoded.get("event_type")
            return GetOrderStatusOutput(
                order_id=payload.order_id,
                state=state,
                ts_event=ts_event,
                raw=decoded,
            )

        # Not found within scan_limit — informational, not an error.
        return GetOrderStatusOutput(order_id=payload.order_id)


register(GetOrderStatusTool())
