"""Tests for /orders endpoint (GET list + POST submission)."""

from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import pytest
from httpx import AsyncClient

from fincept_bus.streams import STREAM_ORDERS


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #


async def _read_stream_payloads(
    redis: fakeredis.aioredis.FakeRedis, stream: str
) -> list[dict[str, Any]]:
    """Return the decoded ``payload`` JSON for every entry in ``stream``.

    Producer publishes Event envelopes with ``{type, payload}`` fields
    where payload is a JSON-encoded Pydantic dump.  This helper hides
    the boilerplate so the tests read naturally.
    """
    entries = await redis.xrange(stream)
    out: list[dict[str, Any]] = []
    for _id, fields in entries:
        # Redis returns byte keys/values when decode_responses=False.
        payload_raw = fields.get(b"payload") or fields.get("payload")
        if payload_raw is None:
            continue
        if isinstance(payload_raw, bytes):
            payload_raw = payload_raw.decode()
        out.append(json.loads(payload_raw))
    return out


@pytest.fixture
def capture_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture calls to ``fincept_db.audit.append`` for POST tests.

    We both monkeypatch the module-level symbol AND the one imported
    into ``api.routes.orders`` so the fixture works whether the route
    references it via ``audit.append(...)`` or a direct import.
    """
    calls: list[dict[str, Any]] = []

    async def fake_append(
        *,
        actor: str,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> str:
        calls.append(
            {
                "actor": actor,
                "event_type": event_type,
                "payload": payload,
                "correlation_id": correlation_id,
            }
        )
        return "audit-id"

    monkeypatch.setattr("api.routes.orders.audit.append", fake_append)
    return calls


# --------------------------------------------------------------------------- #
# GET /orders                                                                 #
# --------------------------------------------------------------------------- #


async def test_orders_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/orders")
    assert response.status_code == 401


async def test_orders_returns_audit_list(
    client: AsyncClient,
    auth_headers: dict[str, str],
    stub_orders: list[dict[str, Any]],
) -> None:
    response = await client.get("/orders", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["order_id"] == "o1"
    assert body[0]["status"] == "filled"


async def test_orders_filtered_by_strategy_id(
    client: AsyncClient,
    auth_headers: dict[str, str],
    stub_orders: list[dict[str, Any]],
) -> None:
    response = await client.get(
        "/orders",
        headers=auth_headers,
        params={"strategy_id": "ma_crossover.v1"},
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


async def test_orders_filtered_by_unknown_strategy_returns_empty(
    client: AsyncClient,
    auth_headers: dict[str, str],
    stub_orders: list[dict[str, Any]],
) -> None:
    response = await client.get(
        "/orders", headers=auth_headers, params={"strategy_id": "never_seen"}
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_orders_filtered_by_status(
    client: AsyncClient,
    auth_headers: dict[str, str],
    stub_orders: list[dict[str, Any]],
) -> None:
    response = await client.get(
        "/orders", headers=auth_headers, params={"status": "filled"}
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


async def test_orders_rejects_invalid_status_value(
    client: AsyncClient,
    auth_headers: dict[str, str],
    stub_orders: list[dict[str, Any]],
) -> None:
    response = await client.get(
        "/orders", headers=auth_headers, params={"status": "not_a_real_status"}
    )
    assert response.status_code == 422


async def test_orders_returns_empty_when_audit_store_unavailable(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    async def broken_list_recent_orders(
        *,
        strategy_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        raise ConnectionRefusedError("db down")

    monkeypatch.setattr("api.routes.orders.list_recent_orders", broken_list_recent_orders)

    response = await client.get("/orders", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == []


# --------------------------------------------------------------------------- #
# POST /orders                                                                #
# --------------------------------------------------------------------------- #


async def test_post_orders_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/orders",
        json={"symbol": "AAPL", "side": "buy", "quantity": "1"},
    )
    assert response.status_code == 401


async def test_post_orders_publishes_market_intent_to_stream(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
    capture_audit: list[dict[str, Any]],
) -> None:
    """Happy path: operator submits a market order via the API.

    We verify three things after the call:
      1. The response returns a fresh order_id + decision_id.
      2. The ord.orders stream got exactly one OrderIntent envelope.
      3. An audit entry was written for the submission.
    """
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={
            "symbol": "AAPL",
            "side": "buy",
            "order_type": "market",
            "quantity": "10",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["strategy_id"] == "manual"
    order_id = body["order_id"]
    decision_id = body["decision_id"]
    assert order_id
    assert decision_id
    assert order_id != decision_id
    assert isinstance(body["ts_event"], int)

    payloads = await _read_stream_payloads(fake_redis, STREAM_ORDERS)
    assert len(payloads) == 1
    intent = payloads[0]
    assert intent["order_id"] == order_id
    assert intent["decision_id"] == decision_id
    assert intent["symbol"] == "AAPL"
    assert intent["side"] == "buy"
    assert intent["order_type"] == "market"
    # Decimal serialises to a string in mode=json.
    assert intent["quantity"] == "10"
    assert intent["strategy_id"] == "manual"
    # Reserved tags land on every manual intent.
    assert intent["tags"]["source"] == "api.manual"
    assert "actor" in intent["tags"]

    assert len(capture_audit) == 1
    assert capture_audit[0]["event_type"] == "api.order_submitted"
    assert capture_audit[0]["correlation_id"] == order_id


async def test_post_orders_accepts_custom_strategy_id_and_tags(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
    capture_audit: list[dict[str, Any]],
) -> None:
    """Caller-provided tags merge with — but don't overwrite — reserved ones."""
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={
            "symbol": "BTC-USD",
            "side": "sell",
            "quantity": "0.25",
            "strategy_id": "ops_cleanup",
            "tags": {"reason": "position_flatten", "source": "attempted_override"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["strategy_id"] == "ops_cleanup"

    payloads = await _read_stream_payloads(fake_redis, STREAM_ORDERS)
    intent = payloads[0]
    assert intent["strategy_id"] == "ops_cleanup"
    # Caller tag preserved.
    assert intent["tags"]["reason"] == "position_flatten"
    # Reserved key overrides any clash from the caller.
    assert intent["tags"]["source"] == "api.manual"


async def test_post_orders_accepts_limit_order_with_price(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
    capture_audit: list[dict[str, Any]],
) -> None:
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={
            "symbol": "NVDA",
            "side": "buy",
            "order_type": "limit",
            "quantity": "5",
            "limit_price": "450.25",
            "time_in_force": "day",
        },
    )
    assert response.status_code == 200

    payloads = await _read_stream_payloads(fake_redis, STREAM_ORDERS)
    intent = payloads[0]
    assert intent["order_type"] == "limit"
    assert intent["limit_price"] == "450.25"
    assert intent["time_in_force"] == "day"


async def test_post_orders_rejects_limit_without_limit_price(
    client: AsyncClient,
    auth_headers: dict[str, str],
    capture_audit: list[dict[str, Any]],
) -> None:
    """Fail fast at the API boundary — saves a round-trip through the OMS."""
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={
            "symbol": "AAPL",
            "side": "buy",
            "order_type": "limit",
            "quantity": "1",
        },
    )
    assert response.status_code == 400
    assert "limit_price" in response.text


async def test_post_orders_rejects_stop_without_stop_price(
    client: AsyncClient,
    auth_headers: dict[str, str],
    capture_audit: list[dict[str, Any]],
) -> None:
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={
            "symbol": "AAPL",
            "side": "sell",
            "order_type": "stop",
            "quantity": "1",
        },
    )
    assert response.status_code == 400
    assert "stop_price" in response.text


async def test_post_orders_rejects_zero_quantity(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={"symbol": "AAPL", "side": "buy", "quantity": "0"},
    )
    # Pydantic returns 422 for gt=0 violations.
    assert response.status_code == 422


async def test_post_orders_rejects_negative_quantity(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={"symbol": "AAPL", "side": "buy", "quantity": "-1"},
    )
    assert response.status_code == 422


async def test_post_orders_rejects_unknown_side(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={"symbol": "AAPL", "side": "short", "quantity": "1"},
    )
    assert response.status_code == 422


async def test_post_orders_rejects_extra_fields(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """``extra='forbid'`` on PlaceOrderBody stops stray fields from slipping
    through -- we don't want a future field typo silently accepted."""
    response = await client.post(
        "/orders",
        headers=auth_headers,
        json={
            "symbol": "AAPL",
            "side": "buy",
            "quantity": "1",
            "mystery_field": "whatever",
        },
    )
    assert response.status_code == 422
