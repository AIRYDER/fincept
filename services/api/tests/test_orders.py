"""Tests for /orders endpoint."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


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
