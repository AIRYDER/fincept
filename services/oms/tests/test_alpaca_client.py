"""
Tests for oms.alpaca.client.AlpacaClient — REST wire protocol.

Uses ``respx`` to mock httpx responses so tests run with no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
import respx

from fincept_core.schemas import OrderIntent, OrderType, Side, TimeInForce, Venue
from oms.alpaca.client import AlpacaClient, AlpacaError

BASE_URL = "https://paper-api.alpaca.markets"


def _intent(
    *,
    order_id: str = "o1",
    symbol: str = "BTC-USD",
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: str = "1",
    limit_price: Decimal | None = None,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="d1",
        ts_event=1_000,
        strategy_id="s",
        symbol=symbol,
        venue=Venue.ALPACA,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
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
# Construction
# ---------------------------------------------------------------------------


async def test_client_requires_both_credentials(http: httpx.AsyncClient) -> None:
    with pytest.raises(ValueError, match="api_key"):
        AlpacaClient(http=http, api_key="", api_secret="x")
    with pytest.raises(ValueError, match="api_key"):
        AlpacaClient(http=http, api_key="x", api_secret="")


# ---------------------------------------------------------------------------
# submit_order
# ---------------------------------------------------------------------------


async def test_submit_order_posts_canonical_body_with_auth_headers(
    client: AlpacaClient,
) -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = httpx.Response(200, content=request.content).json()
        captured["key"] = request.headers.get("APCA-API-KEY-ID")
        captured["secret"] = request.headers.get("APCA-API-SECRET-KEY")
        return httpx.Response(
            200,
            json={
                "id": "alpaca-uuid-1",
                "client_order_id": "o1",
                "status": "accepted",
                "symbol": "BTC/USD",
                "side": "buy",
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(side_effect=respond)
        response = await client.submit_order(_intent(symbol="BTC-USD"))

    assert response["id"] == "alpaca-uuid-1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v2/orders"
    body = captured["body"]
    assert body == {
        "client_order_id": "o1",
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "market",
        "qty": "1",
        "time_in_force": "gtc",
    }
    assert captured["key"] == "test-key"
    assert captured["secret"] == "test-secret"


async def test_submit_order_includes_limit_price_for_limit_orders(
    client: AlpacaClient,
) -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = httpx.Response(200, content=request.content).json()
        return httpx.Response(200, json={"id": "x", "status": "accepted"})

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(side_effect=respond)
        await client.submit_order(_intent(order_type=OrderType.LIMIT, limit_price=Decimal("99.50")))

    # Decimal normalize() drops trailing zeros: 99.50 -> 99.5.  Alpaca
    # accepts both forms; we just need to verify the price round-trips.
    assert captured["body"]["limit_price"] == "99.5"  # type: ignore[index]
    assert captured["body"]["type"] == "limit"  # type: ignore[index]


async def test_submit_order_translates_canonical_crypto_symbol(
    client: AlpacaClient,
) -> None:
    """BTC-USD should hit the wire as BTC/USD."""
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = httpx.Response(200, content=request.content).json()
        return httpx.Response(200, json={"id": "x", "status": "accepted"})

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(side_effect=respond)
        await client.submit_order(_intent(symbol="ETH-USD"))

    assert captured["body"]["symbol"] == "ETH/USD"  # type: ignore[index]


async def test_submit_order_passes_through_equity_symbol(client: AlpacaClient) -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = httpx.Response(200, content=request.content).json()
        return httpx.Response(200, json={"id": "x", "status": "accepted"})

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(side_effect=respond)
        await client.submit_order(_intent(symbol="AAPL"))

    assert captured["body"]["symbol"] == "AAPL"  # type: ignore[index]


async def test_submit_order_raises_on_4xx_with_error_body(
    client: AlpacaClient,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/v2/orders").mock(
            return_value=httpx.Response(422, json={"code": 40010001, "message": "qty must be > 0"})
        )
        with pytest.raises(AlpacaError) as excinfo:
            await client.submit_order(_intent())

    assert excinfo.value.status_code == 422
    assert excinfo.value.body == {"code": 40010001, "message": "qty must be > 0"}


# ---------------------------------------------------------------------------
# get_order
# ---------------------------------------------------------------------------


async def test_get_order_returns_alpaca_payload(client: AlpacaClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/v2/orders/alpaca-uuid").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "alpaca-uuid",
                    "client_order_id": "o1",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "100.5",
                    "filled_at": "2026-04-29T10:00:00Z",
                    "symbol": "BTC/USD",
                    "side": "buy",
                },
            )
        )
        response = await client.get_order("alpaca-uuid")

    assert response["status"] == "filled"
    assert response["filled_avg_price"] == "100.5"


async def test_get_order_raises_on_404(client: AlpacaClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/v2/orders/missing").mock(
            return_value=httpx.Response(404, json={"code": 40410000, "message": "not found"})
        )
        with pytest.raises(AlpacaError) as excinfo:
            await client.get_order("missing")
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


async def test_cancel_order_accepts_204_no_content(client: AlpacaClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.delete(f"{BASE_URL}/v2/orders/o1").mock(return_value=httpx.Response(204))
        # Should not raise.
        await client.cancel_order("o1")


async def test_cancel_order_raises_on_failure(client: AlpacaClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.delete(f"{BASE_URL}/v2/orders/o1").mock(
            return_value=httpx.Response(422, json={"code": 42210000, "message": "already filled"})
        )
        with pytest.raises(AlpacaError):
            await client.cancel_order("o1")
