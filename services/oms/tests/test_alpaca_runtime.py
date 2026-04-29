"""
Tests for oms.alpaca.runtime — submit_intent + poll_pending_orders.

Use respx to fake the Alpaca REST API and exercise the full submit ->
instant-poll -> background-poll flow without a real network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest_asyncio
import respx

from fincept_core.schemas import (
    Fill,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Venue,
)
from oms.alpaca.client import AlpacaClient
from oms.alpaca.runtime import (
    PendingOrder,
    poll_pending_orders,
    submit_intent,
)

BASE_URL = "https://paper-api.alpaca.markets"


def _intent(
    *,
    order_id: str = "o1",
    symbol: str = "BTC-USD",
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="d1",
        ts_event=1_000,
        strategy_id="ma_crossover.v1",
        symbol=symbol,
        venue=Venue.ALPACA,
        side=side,
        order_type=order_type,
        quantity=Decimal("1"),
        limit_price=limit_price,
        time_in_force=TimeInForce.GTC,
    )


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield client


@pytest_asyncio.fixture
async def client(http: httpx.AsyncClient) -> AlpacaClient:
    return AlpacaClient(http=http, api_key="test-key", api_secret="test-secret")


# ---------------------------------------------------------------------------
# submit_intent
# ---------------------------------------------------------------------------


async def test_submit_intent_returns_pending_then_new_when_alpaca_accepts(
    client: AlpacaClient,
) -> None:
    """Alpaca accepts the order with status='accepted' and no fill -> we
    return [PENDING_NEW, NEW] and no fill (caller registers it pending)."""
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "alpaca-uuid",
                    "client_order_id": "o1",
                    "status": "accepted",
                    "symbol": "BTC/USD",
                },
            )
        )
        # subsequent get_order calls also return accepted (no fill)
        router.get(f"{BASE_URL}/v2/orders/alpaca-uuid").mock(
            return_value=httpx.Response(
                200, json={"id": "alpaca-uuid", "status": "accepted", "symbol": "BTC/USD"}
            )
        )
        pending: dict[str, PendingOrder] = {}
        result = await submit_intent(
            _intent(),
            client=client,
            pending=pending,
            instant_poll_s=0.05,
            poll_interval_s=0.01,
        )

    assert [o.status for o in result.order_states] == [
        OrderStatus.PENDING_NEW,
        OrderStatus.NEW,
    ]
    assert result.fill is None
    # Order should have been registered for background poll.
    assert "o1" in pending
    assert pending["o1"].alpaca_order_id == "alpaca-uuid"
    # NEW state should carry the venue order id.
    assert result.order_states[-1].venue_order_id == "alpaca-uuid"


async def test_submit_intent_short_circuits_when_alpaca_returns_filled(
    client: AlpacaClient,
) -> None:
    """If Alpaca returns status='filled' on submit, no polling is needed."""
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "alpaca-uuid",
                    "client_order_id": "o1",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "100.50",
                    "filled_at": "2026-04-29T10:00:00Z",
                    "symbol": "BTC/USD",
                    "side": "buy",
                },
            )
        )
        pending: dict[str, PendingOrder] = {}
        result = await submit_intent(
            _intent(),
            client=client,
            pending=pending,
        )

    statuses = [o.status for o in result.order_states]
    assert statuses == [OrderStatus.PENDING_NEW, OrderStatus.NEW, OrderStatus.FILLED]
    assert result.fill is not None
    assert result.fill.price == Decimal("100.50")
    assert result.fill.quantity == Decimal("1")
    # Symbol should be back to canonical form.
    assert result.fill.symbol == "BTC-USD"
    # Pending should NOT track this — already terminal.
    assert "o1" not in pending


async def test_submit_intent_polls_until_fill_within_window(
    client: AlpacaClient,
) -> None:
    """Submit returns 'accepted', second poll returns 'filled' — instant
    poll should pick up the fill and return it."""
    poll_calls = {"count": 0}

    def submit_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "alpaca-uuid",
                "client_order_id": "o1",
                "status": "accepted",
                "symbol": "BTC/USD",
            },
        )

    def get_response(request: httpx.Request) -> httpx.Response:
        poll_calls["count"] += 1
        if poll_calls["count"] >= 2:
            return httpx.Response(
                200,
                json={
                    "id": "alpaca-uuid",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "100.5",
                    "filled_at": "2026-04-29T10:00:00Z",
                    "symbol": "BTC/USD",
                    "side": "buy",
                },
            )
        return httpx.Response(
            200, json={"id": "alpaca-uuid", "status": "accepted", "symbol": "BTC/USD"}
        )

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(side_effect=submit_response)
        router.get(f"{BASE_URL}/v2/orders/alpaca-uuid").mock(side_effect=get_response)
        pending: dict[str, PendingOrder] = {}
        result = await submit_intent(
            _intent(),
            client=client,
            pending=pending,
            instant_poll_s=0.5,
            poll_interval_s=0.01,
        )

    assert result.fill is not None
    assert result.fill.quantity == Decimal("1")
    # We polled at least twice.
    assert poll_calls["count"] >= 2
    assert "o1" not in pending  # filled within window, not registered for background


async def test_submit_intent_emits_rejected_when_alpaca_returns_4xx(
    client: AlpacaClient,
) -> None:
    """Alpaca rejects the order at submit time."""
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(
            return_value=httpx.Response(
                422, json={"code": 40010001, "message": "insufficient buying power"}
            )
        )
        pending: dict[str, PendingOrder] = {}
        result = await submit_intent(_intent(), client=client, pending=pending)

    statuses = [o.status for o in result.order_states]
    assert statuses == [OrderStatus.PENDING_NEW, OrderStatus.REJECTED]
    assert result.fill is None
    assert "o1" not in pending


# ---------------------------------------------------------------------------
# poll_pending_orders
# ---------------------------------------------------------------------------


async def test_poll_pending_orders_emits_fill_when_remote_filled(
    client: AlpacaClient,
) -> None:
    """Background poller picks up a fill on a previously-submitted order."""
    pending: dict[str, PendingOrder] = {
        "o1": PendingOrder(
            fincept_order_id="o1",
            alpaca_order_id="alpaca-uuid",
            symbol="BTC-USD",
            submitted_at_ns=1_000_000_000,
        )
    }
    filled_events: list[tuple[Order, Fill]] = []
    terminal_events: list[Order] = []

    async def on_filled(order: Order, fill: Fill) -> None:
        filled_events.append((order, fill))

    async def on_terminal(order: Order) -> None:
        terminal_events.append(order)

    stop = asyncio.Event()

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/v2/orders/alpaca-uuid").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "alpaca-uuid",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "100.5",
                    "filled_at": "2026-04-29T10:00:00Z",
                    "symbol": "BTC/USD",
                    "side": "buy",
                },
            )
        )
        # Run the loop briefly then signal stop.
        task = asyncio.create_task(
            poll_pending_orders(
                client=client,
                pending=pending,
                on_filled=on_filled,
                on_terminal=on_terminal,
                stop=stop,
                interval_s=0.05,
            )
        )
        await asyncio.sleep(0.15)
        stop.set()
        await task

    assert len(filled_events) == 1
    assert "o1" not in pending
    _order, fill = filled_events[0]
    assert fill.symbol == "BTC-USD"
    assert fill.quantity == Decimal("1")


async def test_poll_pending_orders_emits_terminal_for_canceled_order(
    client: AlpacaClient,
) -> None:
    pending: dict[str, PendingOrder] = {
        "o1": PendingOrder(
            fincept_order_id="o1",
            alpaca_order_id="alpaca-uuid",
            symbol="BTC-USD",
            submitted_at_ns=1_000_000_000,
        )
    }
    filled_events: list[tuple[Order, Fill]] = []
    terminal_events: list[Order] = []

    async def on_filled(order: Order, fill: Fill) -> None:
        filled_events.append((order, fill))

    async def on_terminal(order: Order) -> None:
        terminal_events.append(order)

    stop = asyncio.Event()

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/v2/orders/alpaca-uuid").mock(
            return_value=httpx.Response(
                200, json={"id": "alpaca-uuid", "status": "canceled", "symbol": "BTC/USD"}
            )
        )
        task = asyncio.create_task(
            poll_pending_orders(
                client=client,
                pending=pending,
                on_filled=on_filled,
                on_terminal=on_terminal,
                stop=stop,
                interval_s=0.05,
            )
        )
        await asyncio.sleep(0.15)
        stop.set()
        await task

    assert filled_events == []
    assert len(terminal_events) == 1
    assert terminal_events[0].status == OrderStatus.CANCELED
    assert "o1" not in pending


async def test_poll_pending_orders_keeps_open_orders_pending(
    client: AlpacaClient,
) -> None:
    """An accepted (not-yet-filled) order stays in the pending dict."""
    pending: dict[str, PendingOrder] = {
        "o1": PendingOrder(
            fincept_order_id="o1",
            alpaca_order_id="alpaca-uuid",
            symbol="BTC-USD",
            submitted_at_ns=1_000_000_000,
        )
    }
    filled_events: list[object] = []
    terminal_events: list[object] = []

    async def on_filled(order: Order, fill: Fill) -> None:
        filled_events.append((order, fill))

    async def on_terminal(order: Order) -> None:
        terminal_events.append(order)

    stop = asyncio.Event()

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/v2/orders/alpaca-uuid").mock(
            return_value=httpx.Response(
                200, json={"id": "alpaca-uuid", "status": "accepted", "symbol": "BTC/USD"}
            )
        )
        task = asyncio.create_task(
            poll_pending_orders(
                client=client,
                pending=pending,
                on_filled=on_filled,
                on_terminal=on_terminal,
                stop=stop,
                interval_s=0.05,
            )
        )
        await asyncio.sleep(0.15)
        stop.set()
        await task

    assert filled_events == []
    assert terminal_events == []
    assert "o1" in pending  # still pending; we'll poll again next interval
