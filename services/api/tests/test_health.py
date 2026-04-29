"""Tests for the public /health endpoint."""

from __future__ import annotations

from httpx import AsyncClient


async def test_health_returns_ok_without_auth(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "version" in body


async def test_health_does_not_require_bearer(client: AsyncClient) -> None:
    """Load balancers and uptime probes should be able to hit /health
    without any authentication setup."""
    response = await client.get("/health")
    assert response.status_code != 401
