"""Tests for the /regime endpoint.

Two read paths exercised:
  1. Snapshot key (``service:regime:latest``) - written by the
     regime_agent on every cycle.
  2. ``STREAM_SIG_REGIME`` for change-history.

Both are seeded against the fakeredis from conftest so we don't need a
real running agent.  The tests cover:
  * agent absent (no key) -> status="unavailable"
  * snapshot present -> status="ok" with parsed body + age_seconds
  * malformed snapshot JSON -> treated as unavailable (non-fatal)
  * history=N pulls the most recent N stream entries newest-first
  * history out-of-range returns 400
"""

from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import pytest
from httpx import AsyncClient

from fincept_bus.streams import STREAM_SIG_REGIME

SNAPSHOT_KEY = "service:regime:latest"


def _snapshot(
    *,
    regime: str = "risk_on",
    confidence: float = 0.65,
    ts_ns: int = 1_700_000_000_000_000_000,
    vix: float | None = 14.5,
    yield_spread: float | None = 0.42,
    fed_funds: float | None = 4.50,
    rationale: str = "VIX low (14.5); yield spread healthy (0.42)",
    direction_bias: float = 0.20,
) -> dict[str, Any]:
    return {
        "agent_id": "regime_agent.v1",
        "ts_event": ts_ns,
        "regime": regime,
        "confidence": confidence,
        "vix": vix,
        "yield_spread": yield_spread,
        "fed_funds": fed_funds,
        "rationale": rationale,
        "direction_bias": direction_bias,
    }


async def _seed_history(
    redis: fakeredis.aioredis.FakeRedis, events: list[dict[str, Any]]
) -> None:
    """Append events to STREAM_SIG_REGIME exactly the way Producer would."""
    for evt in events:
        await redis.xadd(
            STREAM_SIG_REGIME,
            {
                "type": "regime",
                "payload": json.dumps(evt),
            },
        )


# --------------------------------------------------------------------------- #
# Snapshot path                                                               #
# --------------------------------------------------------------------------- #


class TestRegimeRoute:
    @pytest.mark.asyncio
    async def test_requires_auth(self, client: AsyncClient) -> None:
        response = await client.get("/regime")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_unavailable_when_no_snapshot(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/regime", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["snapshot"] is None
        assert body["history"] == []
        # Direction map is always returned (so the dashboard knows the
        # palette even when the agent is down).
        assert isinstance(body["direction_map"], dict)

    @pytest.mark.asyncio
    async def test_ok_when_snapshot_present(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        snap = _snapshot(regime="risk_on", confidence=0.7)
        await fake_redis.set(SNAPSHOT_KEY, json.dumps(snap))
        response = await client.get("/regime", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        s = body["snapshot"]
        assert s["regime"] == "risk_on"
        assert s["confidence"] == pytest.approx(0.7)
        assert s["vix"] == pytest.approx(14.5)
        assert s["yield_spread"] == pytest.approx(0.42)
        assert s["fed_funds"] == pytest.approx(4.50)
        assert s["direction_bias"] == pytest.approx(0.20)
        # age_seconds is derived from ts_event (ns) and may be very
        # large for our seeded ts_ns; just confirm it's a number >= 0.
        assert s["age_seconds"] is not None
        assert s["age_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_malformed_snapshot_returns_unavailable(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await fake_redis.set(SNAPSHOT_KEY, "{not json")
        response = await client.get("/regime", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_history_returns_recent_events_newest_first(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # Seed three regime changes in chronological order.
        await _seed_history(
            fake_redis,
            [
                _snapshot(regime="risk_off", confidence=0.85, ts_ns=1_000),
                _snapshot(regime="high_vol", confidence=0.55, ts_ns=2_000),
                _snapshot(regime="neutral", confidence=0.30, ts_ns=3_000),
            ],
        )
        response = await client.get("/regime?history=10", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        # XREVRANGE returns newest first; the route preserves that order.
        regimes = [h["regime"] for h in body["history"]]
        assert regimes == ["neutral", "high_vol", "risk_off"]
        # All entries carry agent_id + confidence + stream_id.
        for h in body["history"]:
            assert h["agent_id"] == "regime_agent.v1"
            assert h["confidence"] is not None
            assert h["stream_id"]

    @pytest.mark.asyncio
    async def test_history_out_of_range(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        # Query exceeds HISTORY_MAX (100); FastAPI's Query(le=100)
        # rejects with 422 before the handler runs.
        response = await client.get("/regime?history=999", headers=auth_headers)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_history_zero_skips_stream_read(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # Even with stream entries present, history=0 returns []
        await _seed_history(
            fake_redis, [_snapshot(regime="risk_off", confidence=0.9, ts_ns=10)]
        )
        response = await client.get("/regime", headers=auth_headers)
        body = response.json()
        assert body["history"] == []

    @pytest.mark.asyncio
    async def test_direction_map_loaded(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = await client.get("/regime", headers=auth_headers)
        body = response.json()
        # The agent module exports REGIME_DIRECTION; the route imports
        # it lazily.  Confirm at least the four canonical regimes are
        # present so the dashboard can render its palette.
        for regime in ("risk_on", "neutral", "high_vol", "risk_off"):
            assert regime in body["direction_map"]
