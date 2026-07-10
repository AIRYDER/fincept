"""
TDD tests for the Quant Foundry worker-health endpoint.

Covers bearer-auth HTTP access to a read-only
``GET /quant-foundry/worker-health`` endpoint that surfaces worker
heartbeats and stale-worker detection from the gateway. The endpoint is
additive, never writes, and never returns secrets.

Acceptance criteria covered:
- 401 when no bearer token is provided.
- 503 when the gateway is absent (default disabled state).
- 200 + safe state (enabled, null path, empty lists) when the gateway is
  present but no ``worker_status_dir`` is configured.
- 200 + heartbeats when a status directory is configured and populated.
- 200 + stale_workers populated when a heartbeat is older than the
  staleness threshold.
- Idempotent: repeated calls return identical shapes (no side effects).
"""

from __future__ import annotations

import json
import pathlib
import time

import pytest
from httpx import AsyncClient
from quant_foundry.gateway import QuantFoundryGateway


def _gateway(
    base_dir: pathlib.Path,
    *,
    enabled: bool = True,
    worker_status_dir: pathlib.Path | str | None = None,
    stale_threshold_seconds: float = 60.0,
) -> QuantFoundryGateway:
    return QuantFoundryGateway(
        enabled=enabled,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-cb-secret",
        base_dir=base_dir,
        worker_status_dir=worker_status_dir,
        stale_threshold_seconds=stale_threshold_seconds,
    )


def _write_status(
    status_dir: pathlib.Path,
    job_id: str,
    *,
    status: str,
    heartbeat_at: float,
    updated_at: float | None = None,
) -> None:
    """Write a worker status JSON file into the status directory."""
    status_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "job_id": job_id,
        "status": status,
        "heartbeat_at": heartbeat_at,
        "updated_at": updated_at if updated_at is not None else heartbeat_at,
    }
    (status_dir / f"{job_id}.json").write_text(
        json.dumps(rec),
        encoding="utf-8",
    )


@pytest.fixture
async def qf_worker_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    """ASGI client with an enabled gateway + a configured status dir."""
    from api.main import app

    status_dir = tmp_path / "qf-worker" / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    app.state.quant_foundry_gateway = _gateway(
        tmp_path / "qf-worker",
        worker_status_dir=status_dir,
    )
    return client


@pytest.fixture
async def qf_worker_no_dir_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    """ASGI client with an enabled gateway but NO status dir configured."""
    from api.main import app

    app.state.quant_foundry_gateway = _gateway(tmp_path / "qf-worker-nodir")
    return client


@pytest.fixture
async def qf_worker_disabled_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    """ASGI client with a present-but-disabled gateway."""
    from api.main import app

    app.state.quant_foundry_gateway = _gateway(
        tmp_path / "qf-worker-disabled",
        enabled=False,
    )
    return client


@pytest.fixture
async def qf_worker_no_gateway_client(client: AsyncClient) -> AsyncClient:
    """ASGI client with no gateway on app.state (absent)."""
    from api.main import app

    if hasattr(app.state, "quant_foundry_gateway"):
        delattr(app.state, "quant_foundry_gateway")
    return client


# ---------------------------------------------------------------------------
# 401 auth
# ---------------------------------------------------------------------------


async def test_worker_health_requires_auth(qf_worker_client: AsyncClient) -> None:
    # Given: no Authorization header.
    # When: the operator calls the worker-health endpoint.
    response = await qf_worker_client.get("/quant-foundry/worker-health")

    # Then: bearer auth is required.
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 503 gateway absent
# ---------------------------------------------------------------------------


async def test_worker_health_returns_503_when_gateway_absent(
    qf_worker_no_gateway_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: no Quant Foundry gateway configured on app.state.
    # When: worker health is requested.
    response = await qf_worker_no_gateway_client.get(
        "/quant-foundry/worker-health",
        headers=auth_headers,
    )

    # Then: 503 disabled state, never crash.
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# 200 + safe defaults (no status dir configured)
# ---------------------------------------------------------------------------


async def test_worker_health_disabled_gateway_returns_safe_state(
    qf_worker_disabled_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: a present-but-disabled gateway with no status dir.
    # When: worker health is requested.
    response = await qf_worker_disabled_client.get(
        "/quant-foundry/worker-health",
        headers=auth_headers,
    )

    # Then: the endpoint reports the disabled shape (enabled=false, null
    # path, empty lists, zero counts). It must NOT raise 500.
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["worker_status_dir"] is None
    assert body["stale_threshold_seconds"] == pytest.approx(60.0)
    assert body["heartbeats"] == []
    assert body["stale_workers"] == []
    assert body["stale_count"] == 0
    assert body["total_workers"] == 0


async def test_worker_health_no_status_dir_returns_safe_state(
    qf_worker_no_dir_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with no worker_status_dir configured.
    # When: worker health is requested.
    response = await qf_worker_no_dir_client.get(
        "/quant-foundry/worker-health",
        headers=auth_headers,
    )

    # Then: 200 with enabled=true, null path, empty lists (no crash).
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["worker_status_dir"] is None
    assert body["stale_threshold_seconds"] == pytest.approx(60.0)
    assert body["heartbeats"] == []
    assert body["stale_workers"] == []
    assert body["stale_count"] == 0
    assert body["total_workers"] == 0


# ---------------------------------------------------------------------------
# 200 + heartbeats from a configured status dir
# ---------------------------------------------------------------------------


async def test_worker_health_with_status_dir_returns_heartbeats(
    qf_worker_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with a status dir containing two fresh
    # worker status records.
    from api.main import app

    gateway: QuantFoundryGateway = app.state.quant_foundry_gateway
    status_dir = gateway._worker_status_dir
    now = time.time()
    _write_status(
        status_dir,
        "job-fresh-1",
        status="training",
        heartbeat_at=now,
    )
    _write_status(
        status_dir,
        "job-fresh-2",
        status="inferring",
        heartbeat_at=now - 1.0,
    )

    # When: worker health is requested.
    response = await qf_worker_client.get(
        "/quant-foundry/worker-health",
        headers=auth_headers,
    )

    # Then: 200 with enabled=true, the configured path, both heartbeats,
    # and no stale workers (both are fresh).
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["worker_status_dir"] is not None
    assert str(status_dir) == body["worker_status_dir"]
    assert body["stale_threshold_seconds"] == pytest.approx(60.0)
    assert body["total_workers"] == 2
    assert body["stale_count"] == 0
    assert body["stale_workers"] == []
    job_ids = sorted(hb["job_id"] for hb in body["heartbeats"])
    assert job_ids == ["job-fresh-1", "job-fresh-2"]


# ---------------------------------------------------------------------------
# 200 + stale worker detection
# ---------------------------------------------------------------------------


async def test_worker_health_detects_stale_workers(
    qf_worker_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with a status dir containing one fresh and
    # one stale worker (heartbeat older than the 60s threshold).
    from api.main import app

    gateway: QuantFoundryGateway = app.state.quant_foundry_gateway
    status_dir = gateway._worker_status_dir
    now = time.time()
    _write_status(
        status_dir,
        "job-fresh",
        status="training",
        heartbeat_at=now,
    )
    # Stale: heartbeat 120s ago, active status -> flagged stale.
    _write_status(
        status_dir,
        "job-stale",
        status="running",
        heartbeat_at=now - 120.0,
    )
    # Completed jobs are never stale (not an active status).
    _write_status(
        status_dir,
        "job-done",
        status="completed",
        heartbeat_at=now - 9999.0,
    )

    # When: worker health is requested.
    response = await qf_worker_client.get(
        "/quant-foundry/worker-health",
        headers=auth_headers,
    )

    # Then: 200 with 3 total workers, 1 stale (job-stale), job-done excluded.
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["total_workers"] == 3
    assert body["stale_count"] == 1
    stale_ids = [rec["job_id"] for rec in body["stale_workers"]]
    assert stale_ids == ["job-stale"]


# ---------------------------------------------------------------------------
# Idempotent
# ---------------------------------------------------------------------------


async def test_worker_health_idempotent(
    qf_worker_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with a status dir containing one fresh worker.
    from api.main import app

    gateway: QuantFoundryGateway = app.state.quant_foundry_gateway
    status_dir = gateway._worker_status_dir
    _write_status(
        status_dir,
        "job-stable",
        status="training",
        heartbeat_at=time.time(),
    )

    # When: worker health is requested twice in quick succession.
    first = await qf_worker_client.get(
        "/quant-foundry/worker-health",
        headers=auth_headers,
    )
    second = await qf_worker_client.get(
        "/quant-foundry/worker-health",
        headers=auth_headers,
    )

    # Then: both calls return identical shapes (no side effects). The
    # enabled flag, path, threshold, counts, and heartbeat job_ids match.
    assert first.status_code == 200
    assert second.status_code == 200
    a = first.json()
    b = second.json()
    assert a["enabled"] == b["enabled"]
    assert a["worker_status_dir"] == b["worker_status_dir"]
    assert a["stale_threshold_seconds"] == b["stale_threshold_seconds"]
    assert a["stale_count"] == b["stale_count"]
    assert a["total_workers"] == b["total_workers"]
    assert sorted(hb["job_id"] for hb in a["heartbeats"]) == sorted(
        hb["job_id"] for hb in b["heartbeats"]
    )
