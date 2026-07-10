"""
Shared fixtures for api tests.

Uses ``httpx.AsyncClient`` + ``ASGITransport`` instead of FastAPI's
``TestClient`` so async fixtures (which seed fakeredis state) run on
the SAME event loop as the request handlers.  ``TestClient`` spins up
its own sync loop, which clashes with fakeredis internal queues
created by async fixtures and produces "Queue is bound to a different
event loop" errors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fincept_core.config import Settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a known JWT secret for tests; clear the singleton between runs."""
    monkeypatch.setenv("FINCEPT_JWT_SECRET", "test-secret-needs-to-be-long-enough")
    monkeypatch.setenv("FINCEPT_REDIS_URL", "redis://localhost:0/0")
    Settings.clear_cache()


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis()
    try:
        yield redis
    finally:
        await redis.aclose()


@pytest_asyncio.fixture
async def client(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to a FastAPI app whose state.redis is the fake."""
    from api.main import app

    # Skip the real lifespan (which would open a real Redis); just stash
    # the fake on app.state directly.  The app's routes only touch
    # app.state.redis via the get_redis dependency.
    app.state.redis = fake_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """A valid Bearer header signed with the test JWT secret."""
    from api.auth import encode_token

    token = encode_token({"sub": "test-user"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def stub_universe(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows = [
        {
            "symbol": "BTC-USD",
            "asset_class": "crypto_spot",
            "venue_default": "binance",
            "active": True,
        },
        {
            "symbol": "AAPL",
            "asset_class": "equity",
            "venue_default": "nasdaq",
            "active": True,
        },
    ]

    async def fake_read_universe(
        *, asset_class: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        out = list(rows)
        if asset_class is not None:
            out = [r for r in out if r["asset_class"] == asset_class]
        if active_only:
            out = [r for r in out if r["active"]]
        return out

    monkeypatch.setattr("api.routes.data.read_universe", fake_read_universe)
    return rows


@pytest.fixture
def stub_orders(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows = [
        {
            "order_id": "o1",
            "decision_id": "d1",
            "ts_event": 1_000,
            "strategy_id": "ma_crossover.v1",
            "symbol": "BTC-USD",
            "venue": "paper",
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
            "status": "filled",
            "filled_qty": "1",
            "avg_fill_price": "100.5",
            "created_at": 1_000,
            "updated_at": 1_001,
        }
    ]

    async def fake_list(
        *,
        strategy_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        out = list(rows)
        if strategy_id is not None:
            out = [r for r in out if r["strategy_id"] == strategy_id]
        if status is not None:
            out = [r for r in out if r["status"] == status]
        return out[:limit]

    monkeypatch.setattr("api.routes.orders.list_recent_orders", fake_list)
    return rows


@pytest.fixture
def stub_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    from fincept_core.schemas import AssetClass, BarEvent, Venue

    bars = [
        BarEvent(
            venue=Venue.BINANCE,
            symbol="BTC-USD",
            asset_class=AssetClass.CRYPTO_SPOT,
            ts_event=1_000_000,
            ts_recv=1_000_000,
            freq="1m",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("10"),
            trades=3,
        )
    ]

    async def fake_read_bars(
        symbol: str, freq: str, start: int, end: int, venue: str | None = None
    ) -> list[BarEvent]:
        return [b for b in bars if b.symbol == symbol and start <= b.ts_event < end]

    monkeypatch.setattr("api.routes.data.read_bars", fake_read_bars)
