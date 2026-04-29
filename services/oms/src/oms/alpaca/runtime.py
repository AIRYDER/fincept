"""
oms.alpaca.runtime - submit + poll lifecycle around AlpacaClient.

Two entry points:

  ``submit_intent(intent, client, *, instant_poll_s, ...)``
      Async.  POSTs the intent to Alpaca, then polls briefly (default
      5 s) for an instant fill.  Returns ``IntentResult`` with the
      Order state transitions and the Fill if the order filled within
      the window.  If not, returns Order(NEW) and the caller is
      responsible for tracking it via ``poll_pending_orders``.

  ``poll_pending_orders(client, pending, *, interval_s, on_filled, on_terminal)``
      Async loop.  Periodically queries Alpaca for each tracked
      ``order_id``, dispatches Fills via ``on_filled`` and terminal-
      unfilled states (canceled/rejected/expired) via ``on_terminal``,
      and removes them from the pending set.

The split lets ``main.py`` short-circuit fast-fillers (market orders
during regular hours fill in <100 ms) while still tracking limit orders
that may fill hours later.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fincept_core.clock import iso_to_ns, now_ns
from fincept_core.ids import new_id
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    Fill,
    Order,
    OrderIntent,
    OrderStatus,
)
from oms.alpaca.client import AlpacaClient, AlpacaError
from oms.alpaca.symbols import from_alpaca_symbol
from oms.processor import IntentResult

log = get_logger(__name__)

DEFAULT_INSTANT_POLL_S = 5.0
DEFAULT_INSTANT_INTERVAL_S = 0.5
DEFAULT_BACKGROUND_INTERVAL_S = 5.0


# Alpaca order statuses we treat as terminal-unfilled (no Fill emitted).
_TERMINAL_UNFILLED = frozenset({"canceled", "expired", "rejected", "suspended"})

# Map the Alpaca status string back to our OrderStatus enum where it makes
# sense.  "new"/"accepted"/"pending_new"/"accepted_for_bidding" all collapse
# to OrderStatus.NEW from the fincept side - the broker-side venue states
# don't matter to consumers.
_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.NEW,
    "accepted": OrderStatus.NEW,
    "accepted_for_bidding": OrderStatus.NEW,
    "pending_new": OrderStatus.PENDING_NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


@dataclass(frozen=True)
class PendingOrder:
    """Bookkeeping for an order awaiting fill confirmation."""

    fincept_order_id: str
    alpaca_order_id: str
    symbol: str
    submitted_at_ns: int


# --- submit -----------------------------------------------------------------


async def submit_intent(
    intent: OrderIntent,
    *,
    client: AlpacaClient,
    pending: MutableMapping[str, PendingOrder] | None = None,
    instant_poll_s: float = DEFAULT_INSTANT_POLL_S,
    poll_interval_s: float = DEFAULT_INSTANT_INTERVAL_S,
) -> IntentResult:
    """Submit ``intent`` to Alpaca, briefly poll for instant fills.

    Returns ``IntentResult`` with the same shape as
    :func:`oms.processor.process_intent` so ``main.py`` can use either
    transparently.  If the order doesn't fill within ``instant_poll_s``
    and ``pending`` was supplied, the order_id is registered there for
    the background poller.
    """
    pending_order = _make_pending_order(intent)

    # PENDING_NEW state: persisted before we hit the wire so an audit
    # trail exists even if the request crashes.
    states: list[Order] = [pending_order]

    try:
        response = await client.submit_order(intent)
    except AlpacaError as exc:
        log.warning(
            "alpaca.submit_rejected",
            order_id=intent.order_id,
            status=exc.status_code,
            body=exc.body,
        )
        rejected = pending_order.model_copy(
            update={"status": OrderStatus.REJECTED, "updated_at": now_ns()}
        )
        states.append(rejected)
        return IntentResult(order_states=states, fill=None)

    alpaca_order_id = str(response["id"])
    new_order = pending_order.model_copy(
        update={
            "status": OrderStatus.NEW,
            "venue_order_id": alpaca_order_id,
            "updated_at": now_ns(),
        }
    )
    states.append(new_order)

    # If the submit response itself indicates the order is already in a
    # terminal state (e.g., immediate market fill on a 24/7 crypto pair),
    # short-circuit instead of polling.
    terminal = _try_terminal_from_response(new_order, response)
    if terminal is not None:
        terminal_order, fill = terminal
        states.append(terminal_order)
        return IntentResult(order_states=states, fill=fill)

    # Otherwise poll for up to instant_poll_s seconds before giving up
    # and handing off to the background poller.
    deadline = time.monotonic() + instant_poll_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval_s)
        try:
            poll = await client.get_order(alpaca_order_id)
        except AlpacaError as exc:
            log.warning(
                "alpaca.poll_failed",
                order_id=intent.order_id,
                alpaca_id=alpaca_order_id,
                status=exc.status_code,
            )
            continue
        terminal = _try_terminal_from_response(new_order, poll)
        if terminal is not None:
            terminal_order, fill = terminal
            states.append(terminal_order)
            return IntentResult(order_states=states, fill=fill)

    # Still pending: hand off to the background poller.
    if pending is not None:
        pending[intent.order_id] = PendingOrder(
            fincept_order_id=intent.order_id,
            alpaca_order_id=alpaca_order_id,
            symbol=intent.symbol,
            submitted_at_ns=now_ns(),
        )
    return IntentResult(order_states=states, fill=None)


# --- background poll loop ---------------------------------------------------


OnFilled = Callable[[Order, Fill], Awaitable[None]]
OnTerminalUnfilled = Callable[[Order], Awaitable[None]]


async def poll_pending_orders(
    *,
    client: AlpacaClient,
    pending: MutableMapping[str, PendingOrder],
    on_filled: OnFilled,
    on_terminal: OnTerminalUnfilled,
    stop: asyncio.Event,
    interval_s: float = DEFAULT_BACKGROUND_INTERVAL_S,
) -> None:
    """Periodically reconcile Alpaca state for every order in ``pending``.

    ``on_filled`` is invoked with the updated ``Order`` and the synthesised
    ``Fill``; ``on_terminal`` is invoked when an order ends in
    canceled/expired/rejected/suspended without a fill.  Both callbacks
    are responsible for any side-effects (publish to streams, audit, etc.).
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass

        for order_id in list(pending):
            pending_order = pending[order_id]
            try:
                poll = await client.get_order(pending_order.alpaca_order_id)
            except AlpacaError as exc:
                log.warning(
                    "alpaca.poll_failed",
                    order_id=order_id,
                    status=exc.status_code,
                )
                continue
            status = str(poll.get("status", ""))
            if status == "filled":
                # Synthesise a NEW Order shell (we don't keep the Order
                # object across requests in v1; main.py logs both states
                # via callbacks) plus the Fill.
                new_order = _new_order_shell(pending_order)
                terminal = _try_terminal_from_response(new_order, poll)
                if terminal is not None:
                    filled_order, fill = terminal
                    if fill is not None:
                        await on_filled(filled_order, fill)
                pending.pop(order_id, None)
            elif status in _TERMINAL_UNFILLED:
                terminal_order = _new_order_shell(pending_order).model_copy(
                    update={
                        "status": _STATUS_MAP.get(status, OrderStatus.REJECTED),
                        "updated_at": now_ns(),
                    }
                )
                with contextlib.suppress(Exception):
                    await on_terminal(terminal_order)
                pending.pop(order_id, None)


# --- helpers ----------------------------------------------------------------


def _make_pending_order(intent: OrderIntent) -> Order:
    ts = now_ns()
    return Order(
        **intent.model_dump(),
        status=OrderStatus.PENDING_NEW,
        created_at=ts,
        updated_at=ts,
    )


def _new_order_shell(pending_order: PendingOrder) -> Order:
    """Synthesise a minimal Order for state-update callbacks.

    The background poller doesn't carry the original OrderIntent so we
    reconstruct just enough of an Order to feed callbacks.  Fields that
    can't be recovered are left at safe defaults; downstream consumers
    that need the original intent must look it up via the audit log.
    """
    from fincept_core.schemas import OrderType, Side, TimeInForce, Venue

    return Order(
        order_id=pending_order.fincept_order_id,
        decision_id=pending_order.fincept_order_id,  # we don't have decision_id here
        ts_event=pending_order.submitted_at_ns,
        strategy_id="alpaca.unknown",  # see audit log for true strategy
        symbol=pending_order.symbol,
        venue=Venue.ALPACA,
        side=Side.BUY,  # placeholder; gets overwritten via _try_terminal_from_response
        order_type=OrderType.MARKET,
        quantity=Decimal(0),
        time_in_force=TimeInForce.GTC,
        status=OrderStatus.NEW,
        created_at=pending_order.submitted_at_ns,
        updated_at=pending_order.submitted_at_ns,
        venue_order_id=pending_order.alpaca_order_id,
    )


def _try_terminal_from_response(
    base: Order, response: dict[str, Any]
) -> tuple[Order, Fill | None] | None:
    """If ``response`` indicates a terminal state, return (Order, Fill?)."""
    status_str = str(response.get("status", ""))
    if status_str == "filled":
        fill = _build_fill_from_response(base, response)
        filled = base.model_copy(
            update={
                "status": OrderStatus.FILLED,
                "filled_qty": fill.quantity,
                "avg_fill_price": fill.price,
                "updated_at": now_ns(),
            }
        )
        return filled, fill
    if status_str in _TERMINAL_UNFILLED:
        terminal = base.model_copy(
            update={
                "status": _STATUS_MAP.get(status_str, OrderStatus.REJECTED),
                "updated_at": now_ns(),
            }
        )
        return terminal, None
    return None


def _build_fill_from_response(base: Order, response: dict[str, Any]) -> Fill:
    """Construct our Fill from Alpaca's filled order JSON."""
    filled_at = response.get("filled_at") or response.get("updated_at")
    ts_event = iso_to_ns(str(filled_at).replace("Z", "+00:00")) if filled_at else now_ns()

    avg_price = response.get("filled_avg_price")
    qty = response.get("filled_qty") or response.get("qty") or "0"
    symbol_alpaca = response.get("symbol", "")
    return Fill(
        fill_id=new_id(),
        order_id=base.order_id,
        ts_event=ts_event,
        symbol=from_alpaca_symbol(str(symbol_alpaca)) if symbol_alpaca else base.symbol,
        side=base.side,
        price=Decimal(str(avg_price)) if avg_price else Decimal(0),
        quantity=Decimal(str(qty)),
        # Alpaca paper has no fees; live equity has zero commissions; live
        # crypto fees are surfaced in a separate ``fees`` field on the
        # account, not the order response.  Default 0 for v1.
        fee=Decimal(0),
        is_maker=None,  # Alpaca doesn't expose maker/taker on the order JSON
        venue_exec_id=str(response.get("id", "")),
    )
