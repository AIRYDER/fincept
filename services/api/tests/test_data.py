"""Tests for /data/universe and /data/bars endpoints."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# /data/sources
# ---------------------------------------------------------------------------


async def test_data_sources_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/data/sources")
    assert response.status_code == 401


async def test_data_sources_returns_control_registry(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/data/sources", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    source_ids = {source["id"] for source in payload["sources"]}
    assert source_ids == {
        "exa",
        "openbb",
        "alpaca",
        "binance",
        "timescale_bars",
        "redis",
        "local_predictions",
        "news_impact_model",
    }
    assert payload["summary"]["total"] == 8

    exa = next(source for source in payload["sources"] if source["id"] == "exa")
    assert "POST /research/exa" in exa["call_surfaces"]
    assert exa["safety"] == "read_only"
    assert exa["return_format"] == "structured_brief_with_grounding"

    bars = next(
        source for source in payload["sources"] if source["id"] == "timescale_bars"
    )
    assert "GET /data/coverage" in bars["call_surfaces"]
    assert "ohlcv_bars" in bars["data"]


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


async def test_seed_universe_from_positions_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/data/universe/seed-from-positions")
    assert response.status_code == 401


async def test_seed_universe_from_positions_upserts_open_book_symbols(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fake_redis,
    monkeypatch,
) -> None:
    from fincept_core.schemas import Position
    from portfolio.store import PositionStore

    store = PositionStore(fake_redis)
    await store.put(
        Position(
            strategy_id="alpaca.live",
            symbol="amd",
            quantity=Decimal("10"),
            avg_cost=Decimal("100"),
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            updated_at=0,
        )
    )
    await store.put(
        Position(
            strategy_id="alpaca.live",
            symbol="MSFT",
            quantity=Decimal(0),
            avg_cost=Decimal("100"),
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            updated_at=0,
        )
    )

    captured: list[dict[str, object]] = []

    async def fake_upsert_universe_symbols(rows: list[dict[str, object]]):
        captured.extend(rows)
        return rows

    monkeypatch.setattr(
        "api.routes.data.upsert_universe_symbols",
        fake_upsert_universe_symbols,
    )

    response = await client.post(
        "/data/universe/seed-from-positions",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["seeded"] == 1
    assert body["symbols"] == ["AMD"]
    assert captured == [
        {
            "symbol": "AMD",
            "asset_class": "equity",
            "venue_default": "alpaca",
            "active": True,
        }
    ]
    assert body["universe"][0]["venue"] == "alpaca"


async def test_seed_universe_from_positions_marks_crypto_defaults(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fake_redis,
    monkeypatch,
) -> None:
    from fincept_core.schemas import Position
    from portfolio.store import PositionStore

    store = PositionStore(fake_redis)
    await store.put(
        Position(
            strategy_id="crypto.live",
            symbol="BTC-USD",
            quantity=Decimal("0.25"),
            avg_cost=Decimal("50000"),
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            updated_at=0,
        )
    )

    captured: list[dict[str, object]] = []

    async def fake_upsert_universe_symbols(rows: list[dict[str, object]]):
        captured.extend(rows)
        return rows

    monkeypatch.setattr(
        "api.routes.data.upsert_universe_symbols",
        fake_upsert_universe_symbols,
    )

    response = await client.post(
        "/data/universe/seed-from-positions",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["symbols"] == ["BTC-USD"]
    assert captured[0]["asset_class"] == "crypto_spot"
    assert captured[0]["venue_default"] == "binance"


async def test_alpaca_demo_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/data/alpaca/demo")
    assert response.status_code == 401


async def test_alpaca_demo_reports_missing_credentials(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "api.routes.data.get_settings",
        lambda: SimpleNamespace(ALPACA_API_KEY=None, ALPACA_API_SECRET=None),
    )

    response = await client.get("/data/alpaca/demo", headers=auth_headers)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error_type"] == "AlpacaCredentialsMissing"


async def test_alpaca_demo_returns_news_and_bar_sample(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeAlpacaDataClient:
        def __init__(self, **kwargs: object) -> None:
            calls["init"] = kwargs

        async def list_news(self, **kwargs: object) -> dict[str, object]:
            calls["news"] = kwargs
            return {
                "news": [
                    {
                        "id": 1,
                        "headline": "Demo headline",
                        "symbols": ["AAPL"],
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "next_page_token": None,
            }

        async def list_bars(
            self, symbols: list[str], **kwargs: object
        ) -> dict[str, object]:
            calls["bars"] = {"symbols": symbols, **kwargs}
            return {
                "bars": {
                    "AAPL": [{"t": "2026-01-01T00:00:00Z", "c": 100.0}],
                    "NVDA": [],
                }
            }

    monkeypatch.setattr(
        "api.routes.data.get_settings",
        lambda: SimpleNamespace(ALPACA_API_KEY="key", ALPACA_API_SECRET="secret"),
    )
    monkeypatch.setattr("api.routes.data.AlpacaDataClient", FakeAlpacaDataClient)

    response = await client.get(
        "/data/alpaca/demo",
        headers=auth_headers,
        params={"symbols": "aapl,nvda,aapl", "news_limit": 2, "bar_limit": 3},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["symbols"] == ["AAPL", "NVDA"]
    assert body["summary"] == {"news_count": 1, "symbols_with_bars": 1, "bar_count": 1}
    assert body["news"][0]["headline"] == "Demo headline"
    assert calls["news"]["symbols"] == ["AAPL", "NVDA"]
    assert calls["bars"]["symbols"] == ["AAPL", "NVDA"]
    assert calls["bars"]["feed"] == "iex"


# ---------------------------------------------------------------------------
# /data/symbols/search
# ---------------------------------------------------------------------------


async def test_symbol_search_returns_503_when_universe_unavailable(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    async def broken_read_universe(*, asset_class=None, active_only=True):
        raise ConnectionRefusedError("db down")

    monkeypatch.setattr("api.routes.data.read_universe", broken_read_universe)

    response = await client.get(
        "/data/symbols/search",
        headers=auth_headers,
        params={"q": "BTC"},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error_type"] == "DataStoreUnavailable"
    assert "db down" not in str(detail)


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
        {
            "symbol": "FRESH",
            "asset_class": "equity",
            "venue_default": "sim",
            "active": True,
        },
        {
            "symbol": "STALE",
            "asset_class": "equity",
            "venue_default": "sim",
            "active": True,
        },
        {
            "symbol": "EMPTY",
            "asset_class": "crypto_spot",
            "venue_default": "binance",
            "active": True,
        },
    ]

    async def fake_read_universe(*, asset_class=None, active_only=True):
        return [
            row
            for row in universe
            if asset_class is None or row["asset_class"] == asset_class
        ]

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
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "venue_default": "nasdaq",
                "active": True,
            },
            {
                "symbol": "MSFT",
                "asset_class": "equity",
                "venue_default": "nasdaq",
                "active": True,
            },
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
