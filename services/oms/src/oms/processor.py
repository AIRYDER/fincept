"""
oms.processor — pure OrderIntent -> (Order_states, Fill?) pipeline.

Extracted from ``main.py`` so tests can exercise the full intent ->
states -> fill flow without a Redis consumer loop.  Returns a tuple of:

  - ``order_states``: ordered list of ``Order`` snapshots, one per state
                      transition (PENDING_NEW, NEW, FILLED / REJECTED).
  - ``fill``:         the ``Fill`` if the order filled, else None.

The caller (``main.py``) is responsible for:
  - publishing each order state to STREAM_ORDERS
  - publishing the fill (if any) to STREAM_FILLS
  - persisting the audit trail via ``fincept_db.audit.append``

Keeping IO and business logic apart this way is the only way to write
deterministic tests for OMS behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fincept_core.clock import now_ns
from fincept_core.schemas import Fill, Order, OrderIntent, OrderStatus
from oms.paper import PaperFiller
from oms.prices import LivePrices
from oms.state import can_transition

NowFn = Callable[[], int]


@dataclass(frozen=True)
class IntentResult:
    """Outcome of processing a single OrderIntent."""

    order_states: list[Order]
    """Ordered Order snapshots — one per state transition."""

    fill: Fill | None
    """The Fill, if the order filled.  None if rejected or unfilled."""

    @property
    def final_status(self) -> OrderStatus:
        return self.order_states[-1].status if self.order_states else OrderStatus.REJECTED


def process_intent(
    intent: OrderIntent,
    *,
    prices: LivePrices,
    filler: PaperFiller,
    clock: NowFn = now_ns,
) -> IntentResult:
    """Run an OrderIntent through the OMS state machine.

    Synchronous on purpose — there's no IO inside.  ``main.py`` is the
    only place that touches Redis or the DB.
    """
    states: list[Order] = []

    # PENDING_NEW: created from intent, awaiting venue acknowledgement.
    pending = Order(
        **intent.model_dump(),
        status=OrderStatus.PENDING_NEW,
        created_at=clock(),
        updated_at=clock(),
    )
    states.append(pending)

    # PENDING_NEW -> NEW transition (always legal in this v1; the venue
    # gate is what would reject in real life).
    if not can_transition(pending.status, OrderStatus.NEW):
        # Defensive: this branch shouldn't fire given VALID_TRANSITIONS,
        # but keeping it here documents the invariant for future readers
        # who add new statuses to PENDING_NEW.
        rejected = pending.model_copy(
            update={"status": OrderStatus.REJECTED, "updated_at": clock()}
        )
        states.append(rejected)
        return IntentResult(order_states=states, fill=None)

    new_order = pending.model_copy(update={"status": OrderStatus.NEW, "updated_at": clock()})
    states.append(new_order)

    # No-mid -> reject.  A live venue would queue and try later; for
    # paper we'd rather fail loudly than silently sit on the order.
    mid_px = prices.get(new_order.symbol)
    if mid_px is None:
        rejected = new_order.model_copy(
            update={"status": OrderStatus.REJECTED, "updated_at": clock()}
        )
        states.append(rejected)
        return IntentResult(order_states=states, fill=None)

    # Fill via paper simulator.
    fill = filler.fill(new_order, mid_px)
    filled_order = new_order.model_copy(
        update={
            "status": OrderStatus.FILLED,
            "filled_qty": fill.quantity,
            "avg_fill_price": fill.price,
            "updated_at": clock(),
        }
    )
    states.append(filled_order)
    return IntentResult(order_states=states, fill=fill)
