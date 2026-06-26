"""
api.routes.orders — manual order submission + read endpoints.

  GET  /orders                 List latest Order state per order_id
                               (newest first).  Backed by the audit log.
  POST /orders                 Submit a fresh OrderIntent.  The route
                               wraps the operator's request in a
                               canonical OrderIntent, publishes it to
                               the ``ord.orders`` stream, and returns
                               the fresh ``order_id``.  From that point
                               on it follows the exact same lifecycle
                               as a strategy-initiated order: risk gate,
                               venue dispatch, fill, state updates
                               back on the same stream.

Orders aren't persisted to a dedicated table in v1; the OMS appends a
state row to ``audit_log`` for every transition (PENDING_NEW -> NEW ->
FILLED / REJECTED).  ``fincept_db.audit.list_recent_orders`` collapses
those rows to the latest state per ``order_id`` and returns the most
recent N (default 100, max 1000).

Why let the API mint OrderIntents directly?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The alternative (send the request to a manual-trading strategy via
its context) adds a hop with no risk benefit — the OMS already runs
the single authoritative risk gate on every intent regardless of
origin.  A thin, auditable API path lets an operator react in
seconds without spinning up a host.  Manual intents carry a
``strategy_id`` of ``"manual"`` (or the caller-chosen override) and
an audit entry records the actor who submitted them, so downstream
analytics can attribute them correctly.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_redis
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_ORDERS
from fincept_core.clock import now_ns
from fincept_core.events import Event
from fincept_core.ids import new_id
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)
from fincept_db import audit
from fincept_db.audit import list_recent_orders

router = APIRouter()
log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# GET                                                                         #
# --------------------------------------------------------------------------- #


@router.get("")
async def list_orders(
    strategy_id: str | None = Query(None),
    status: OrderStatus | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Return latest Order snapshot per order_id, newest first."""
    try:
        return await list_recent_orders(
            strategy_id=strategy_id,
            status=status.value if status is not None else None,
            limit=limit,
        )
    except Exception as exc:
        log.warning("api.orders.list_unavailable", error=str(exc))
        return []


# --------------------------------------------------------------------------- #
# POST — manual order submission                                              #
# --------------------------------------------------------------------------- #


class PlaceOrderBody(BaseModel):
    """Request body for manual order submission.

    Mirrors the core ``OrderIntent`` shape minus the ids + timestamps
    (those are minted by the route).  Decimals accept either strings
    or numeric JSON to match dashboard convention.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=32)
    side: Side
    order_type: OrderType = OrderType.MARKET
    quantity: Decimal = Field(..., gt=Decimal(0))
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    venue: Venue = Venue.ALPACA
    # Who to credit for the order.  Defaults to "manual" so the
    # dashboard's submit flow attributes cleanly; power users can
    # override to emulate a specific strategy for attribution tests.
    strategy_id: str = Field("manual", min_length=1, max_length=64)
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("quantity", "limit_price", "stop_price", mode="before")
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Any:
        # Accept numeric JSON too; Pydantic already handles str -> Decimal
        # cleanly but floats can surprise by rounding, so funnel them
        # through str() first (the standard idiom for money).
        if v is None or isinstance(v, Decimal):
            return v
        if isinstance(v, (int, float)):
            try:
                return Decimal(str(v))
            except InvalidOperation as exc:
                raise ValueError("invalid numeric value") from exc
        return v


def _validate_price_for_type(body: PlaceOrderBody) -> None:
    """Ensure the price fields match the order type.

    The OMS's risk gate will happily process a limit order without a
    limit_price -- it just can't price-check it and may reject.  Fail
    fast at the edge instead for a cleaner 400 instead of an async
    REJECTED state.
    """
    if body.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
        if body.limit_price is None or body.limit_price <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"{body.order_type.value} orders require a positive limit_price",
            )
    if body.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
        if body.stop_price is None or body.stop_price <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"{body.order_type.value} orders require a positive stop_price",
            )


@router.post("")
async def place_order(
    body: PlaceOrderBody = Body(...),
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Submit a fresh OrderIntent to the OMS via ``ord.orders``.

    Returns ``{ok, order_id, decision_id, ts_event}``.  The caller can
    poll ``GET /orders?limit=N`` or tail the ``orders`` WS stream to
    see the lifecycle transitions.
    """
    _validate_price_for_type(body)

    actor = str(user.get("sub", "manual"))
    order_id = new_id()
    decision_id = new_id()
    ts = now_ns()

    # Merge caller tags with a reserved set that marks this as a
    # manual submission -- we don't want these to be overwritable.
    tags = {
        **body.tags,
        "source": "api.manual",
        "actor": actor,
    }
    intent = OrderIntent(
        order_id=order_id,
        decision_id=decision_id,
        ts_event=ts,
        strategy_id=body.strategy_id,
        symbol=body.symbol,
        venue=body.venue,
        side=body.side,
        order_type=body.order_type,
        quantity=body.quantity,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        time_in_force=body.time_in_force,
        tags=tags,
    )

    # Audit BEFORE publishing so a crash between the audit write and
    # the stream publish leaves an artifact for forensic recovery.
    # Suppressed errors match the OMS pattern in main.py.
    with contextlib.suppress(Exception):
        await audit.append(
            actor=f"api.orders.post:{actor}",
            event_type="api.order_submitted",
            payload=intent.model_dump(mode="json"),
            correlation_id=order_id,
        )

    try:
        producer = Producer(redis)
        await producer.publish(
            STREAM_ORDERS, Event(type="order_intent", payload=intent)
        )
    except Exception as exc:  # pragma: no cover -- network-level failures
        log.warning(
            "api.orders.publish_failed",
            order_id=order_id,
            error=repr(exc),
        )
        raise HTTPException(
            status_code=503,
            detail=f"order stream unavailable: {exc}",
        ) from exc

    log.info(
        "api.orders.submitted",
        actor=actor,
        order_id=order_id,
        strategy_id=body.strategy_id,
        symbol=body.symbol,
        side=body.side.value,
        order_type=body.order_type.value,
        quantity=str(body.quantity),
    )
    return {
        "ok": True,
        "order_id": order_id,
        "decision_id": decision_id,
        "ts_event": ts,
        "strategy_id": body.strategy_id,
    }
