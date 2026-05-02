"""Tests for /data/universe and /data/bars endpoints."""

from __future__ import annotations

from types import SimpleNamespace
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
    rows = response.json()
    assert {r["symbol"] for r in rows} == {"BTC-USD", "AAPL"}
    assert all(row["venue"] == row["venue_default"] for row in rows)


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


# ---------------------------------------------------------------------------
# /data/coverage
# ---------------------------------------------------------------------------


async def test_coverage_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/data/coverage")
    assert response.status_code == 401


async def test_coverage_reports_fresh_stale_and_empty_symbols(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    universe = [
        {"symbol": "FRESH", "asset_class": "equity", "venue_default": "sim", "active": True},
        {"symbol": "STALE", "asset_class": "equity", "venue_default": "sim", "active": True},
        {"symbol": "EMPTY", "asset_class": "crypto_spot", "venue_default": "binance", "active": True},
    ]

    async def fake_read_universe(*, asset_class=None, active_only=True):
        return [row for row in universe if asset_class is None or row["asset_class"] == asset_class]

    async def fake_read_bar_coverage(
        symbols: list[str],
        freq: str,
        start: int,
        end: int,
        venue: str | None = None,
    ):
        assert symbols == ["FRESH", "STALE", "EMPTY"]
        assert freq == "1m"
        assert start == 0
        assert end == 10_001
        assert venue is None
        return {
            "FRESH": SimpleNamespace(bar_count=1, last_ts_event=9_700),
            "STALE": SimpleNamespace(bar_count=1, last_ts_event=1_000),
        }

    monkeypatch.setattr("api.routes.data.read_universe", fake_read_universe)
    monkeypatch.setattr("api.routes.data.read_bar_coverage", fake_read_bar_coverage)

    response = await client.get(
        "/data/coverage",
        headers=auth_headers,
        params={
            "freq": "1m",
            "as_of_ns": 10_000,
            "lookback_ns": 10_000,
            "stale_after_ns": 500,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "total": 3,
        "ok": 1,
        "stale": 1,
        "empty": 1,
        "error": 0,
        "coverage_pct": 66.67,
    }
    statuses = {row["symbol"]: row["status"] for row in payload["rows"]}
    assert statuses == {"FRESH": "ok", "STALE": "stale", "EMPTY": "empty"}
    assert payload["rows"][0]["last_ts_event"] == 9700
    assert payload["rows"][0]["venue_default"] == "sim"
    assert payload["rows"][0]["venue"] is None


async def test_coverage_omitted_venue_queries_all_venues(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    async def fake_read_universe(*, asset_class=None, active_only=True):
        return [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "venue_default": "nasdaq",
                "active": True,
            }
        ]

    async def fake_read_bar_coverage(
        symbols: list[str],
        freq: str,
        start: int,
        end: int,
        venue: str | None = None,
    ):
        assert symbols == ["AAPL"]
        assert venue is None
        return {"AAPL": SimpleNamespace(bar_count=3, last_ts_event=9_900)}

    monkeypatch.setattr("api.routes.data.read_universe", fake_read_universe)
    monkeypatch.setattr("api.routes.data.read_bar_coverage", fake_read_bar_coverage)

    response = await client.get(
        "/data/coverage",
        headers=auth_headers,
        params={"as_of_ns": 10_000, "lookback_ns": 10_000, "stale_after_ns": 500},
    )

    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["status"] == "ok"
    assert row["venue_default"] == "nasdaq"


async def test_coverage_explicit_venue_filters_batch_reader(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    async def fake_read_universe(*, asset_class=None, active_only=True):
        return [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "venue_default": "nasdaq",
                "active": True,
            }
        ]

    async def fake_read_bar_coverage(
        symbols: list[str],
        freq: str,
        start: int,
        end: int,
        venue: str | None = None,
    ):
        assert venue == "paper"
        return {"AAPL": SimpleNamespace(bar_count=1, last_ts_event=9_900)}

    monkeypatch.setattr("api.routes.data.read_universe", fake_read_universe)
    monkeypatch.setattr("api.routes.data.read_bar_coverage", fake_read_bar_coverage)

    response = await client.get(
        "/data/coverage",
        headers=auth_headers,
        params={
            "venue": "paper",
            "as_of_ns": 10_000,
            "lookback_ns": 10_000,
            "stale_after_ns": 500,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["venue"] == "paper"
    assert payload["rows"][0]["status"] == "ok"


async def test_coverage_returns_503_when_universe_unavailable(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    async def broken_read_universe(*, asset_class=None, active_only=True):
        raise ConnectionRefusedError("db down")

    monkeypatch.setattr("api.routes.data.read_universe", broken_read_universe)

    response = await client.get("/data/coverage", headers=auth_headers)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error_type"] == "DataStoreUnavailable"
    assert "db down" not in str(detail)


async def test_coverage_returns_public_error_rows_when_bar_reader_unavailable(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    async def fake_read_universe(*, asset_class=None, active_only=True):
        return [
            {"symbol": "AAPL", "asset_class": "equity", "venue_default": "nasdaq", "active": True},
            {"symbol": "MSFT", "asset_class": "equity", "venue_default": "nasdaq", "active": True},
        ]

    async def broken_read_bar_coverage(
        symbols: list[str],
        freq: str,
        start: int,
        end: int,
        venue: str | None = None,
    ):
        raise RuntimeError("secret connection string leaked")

    monkeypatch.setattr("api.routes.data.read_universe", fake_read_universe)
    monkeypatch.setattr("api.routes.data.read_bar_coverage", broken_read_bar_coverage)

    response = await client.get("/data/coverage", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["error"] == 2
    assert {row["error_type"] for row in payload["rows"]} == {"BarReadFailed"}
    assert "secret connection string leaked" not in str(payload)
