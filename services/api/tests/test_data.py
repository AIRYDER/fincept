"""Tests for /data/universe and /data/bars endpoints."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# /data/universe
# ---------------------------------------------------------------------------


async def test_universe_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/data/universe")
    assert response.status_code == 401


async def test_universe_returns_active_rows(
    client: AsyncClient,
    auth_headers: dict[str, str],
    stub_universe: list[dict[str, Any]],
) -> None:
    response = await client.get("/data/universe", headers=auth_headers)
    assert response.status_code == 200
    assert {r["symbol"] for r in response.json()} == {"BTC-USD", "AAPL"}


async def test_universe_filtered_by_asset_class(
    client: AsyncClient,
    auth_headers: dict[str, str],
    stub_universe: list[dict[str, Any]],
) -> None:
    response = await client.get(
        "/data/universe", headers=auth_headers, params={"asset_class": "equity"}
    )
    assert response.status_code == 200
    assert [r["symbol"] for r in response.json()] == ["AAPL"]


# ---------------------------------------------------------------------------
# /data/bars/{symbol}
# ---------------------------------------------------------------------------


async def test_bars_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/data/bars/BTC-USD?start=0&end=100")
    assert response.status_code == 401


async def test_bars_returns_synthetic_data(
    client: AsyncClient, auth_headers: dict[str, str], stub_bars: None
) -> None:
    response = await client.get(
        "/data/bars/BTC-USD",
        headers=auth_headers,
        params={"start": 0, "end": 999_999_999_999_999},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "BTC-USD"
    assert body[0]["close"] == "100.5"  # Decimal serialised as string


async def test_bars_rejects_inverted_range(
    client: AsyncClient, auth_headers: dict[str, str], stub_bars: None
) -> None:
    response = await client.get(
        "/data/bars/BTC-USD",
        headers=auth_headers,
        params={"start": 999_999, "end": 1_000},
    )
    assert response.status_code == 400


async def test_bars_default_freq_is_1m(
    client: AsyncClient, auth_headers: dict[str, str], stub_bars: None
) -> None:
    """Smoke: omitting ``freq`` should not 422; defaults to 1m."""
    response = await client.get(
        "/data/bars/BTC-USD",
        headers=auth_headers,
        params={"start": 0, "end": 999_999_999_999_999},
    )
    assert response.status_code == 200
