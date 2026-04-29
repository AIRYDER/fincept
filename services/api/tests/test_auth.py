"""Tests for api.auth — JWT bearer enforcement."""

from __future__ import annotations

import jwt
from httpx import AsyncClient


async def test_protected_route_rejects_missing_header(client: AsyncClient) -> None:
    response = await client.get("/positions")
    assert response.status_code == 401


async def test_protected_route_rejects_non_bearer_scheme(client: AsyncClient) -> None:
    response = await client.get(
        "/positions", headers={"Authorization": "Basic foo:bar"}
    )
    assert response.status_code == 401


async def test_protected_route_rejects_empty_bearer(client: AsyncClient) -> None:
    response = await client.get("/positions", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


async def test_protected_route_rejects_token_signed_with_wrong_key(
    client: AsyncClient,
) -> None:
    bad_token = jwt.encode(
        {"sub": "alice"}, "wrong-secret-but-still-long-enough", algorithm="HS256"
    )
    response = await client.get(
        "/positions", headers={"Authorization": f"Bearer {bad_token}"}
    )
    assert response.status_code == 401


async def test_protected_route_accepts_valid_token(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/positions", headers=auth_headers)
    # 200 with empty list (no positions in fakeredis); not 401.
    assert response.status_code == 200
    assert response.json() == []
