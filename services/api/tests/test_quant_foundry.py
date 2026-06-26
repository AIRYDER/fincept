"""
TDD tests for api.routes.quant_foundry (TASK-0306).

Covers the Quant Foundry gateway endpoints in local_mock mode:
- Disabled by default (safe state, no jobs created).
- Enabled mock mode creates + completes a job through the real contract.
- Operator endpoints require bearer auth.
- Callback endpoint uses HMAC auth (NOT bearer) and rejects bad signatures.
- Duplicate callback is idempotent (no duplicate effects).
- Health + heartbeats endpoints.
- No order stream / sig.predict writes (structural: route has no bus).
"""

from __future__ import annotations

import pathlib

import pytest
from httpx import AsyncClient

from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.ids import make_idempotency_key
from quant_foundry.signatures import sign_callback


@pytest.fixture
def qf_enabled_gateway(tmp_path: pathlib.Path) -> QuantFoundryGateway:
    """A gateway enabled in local_mock mode pointing at a tmp dir."""
    return QuantFoundryGateway(
        enabled=True,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-cb-secret",
        base_dir=tmp_path / "qf",
    )


@pytest.fixture
def qf_disabled_gateway(tmp_path: pathlib.Path) -> QuantFoundryGateway:
    return QuantFoundryGateway(
        enabled=False,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-cb-secret",
        base_dir=tmp_path / "qf-disabled",
    )


@pytest.fixture
async def qf_client(
    client: AsyncClient,
    qf_enabled_gateway: QuantFoundryGateway,
) -> AsyncClient:
    """ASGI client with an enabled Quant Foundry gateway on app.state."""
    from api.main import app

    app.state.quant_foundry_gateway = qf_enabled_gateway
    return client


@pytest.fixture
async def qf_disabled_client(
    client: AsyncClient,
    qf_disabled_gateway: QuantFoundryGateway,
) -> AsyncClient:
    from api.main import app

    app.state.quant_foundry_gateway = qf_disabled_gateway
    return client


# --- disabled state ---------------------------------------------------------


async def test_health_disabled_returns_safe_state(
    qf_disabled_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await qf_disabled_client.get("/quant-foundry/health", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["mode"] == "local_mock"


async def test_create_job_disabled_returns_disabled(
    qf_disabled_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await qf_disabled_client.post(
        "/quant-foundry/jobs",
        headers=auth_headers,
        json={
            "job_id": "qf:train:x:1",
            "job_type": "training",
            "idempotency_key": "qf:training:x:1",
            "request_payload": {"model": "gbm"},
        },
    )
    assert r.status_code in (200, 503)
    body = r.json()
    assert body.get("enabled") is False or "disabled" in str(body).lower()


async def test_jobs_endpoints_require_auth(qf_client: AsyncClient) -> None:
    """All operator endpoints require bearer auth."""
    for path in (
        "/quant-foundry/jobs",
        "/quant-foundry/health",
        "/quant-foundry/heartbeats",
    ):
        r = await qf_client.get(path)
        assert r.status_code == 401, f"{path} should require auth"


# --- enabled mock: create + complete ---------------------------------------


async def test_create_job_local_mock_completes(
    qf_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    job_id = "qf:train:ds1:gbm:h1:1"
    idem = make_idempotency_key("training", "ds1", "gbm", "h1", "1")
    r = await qf_client.post(
        "/quant-foundry/jobs",
        headers=auth_headers,
        json={
            "job_id": job_id,
            "job_type": "training",
            "idempotency_key": idem,
            "request_payload": {
                "schema_version": 1,
                "job_id": job_id,
                "dataset_manifest_ref": "ds-1",
                "model_family": "gbm",
                "search_space": {"n_estimators": [100]},
                "random_seed": 42,
                "hardware_class": "mock",
                "extra_constraints": {},
            },
            "priority": 1,
            "budget_cents": 100,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["job_id"] == job_id
    assert body["status"] == "completed"


async def test_get_job_detail(
    qf_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    job_id = "qf:train:detail:1"
    idem = make_idempotency_key("training", "detail", "gbm", "h1", "1")
    await qf_client.post(
        "/quant-foundry/jobs",
        headers=auth_headers,
        json={
            "job_id": job_id,
            "job_type": "training",
            "idempotency_key": idem,
            "request_payload": {
                "schema_version": 1,
                "job_id": job_id,
                "dataset_manifest_ref": "ds-1",
                "model_family": "gbm",
                "search_space": {},
                "random_seed": 1,
                "hardware_class": "m",
                "extra_constraints": {},
            },
        },
    )
    r = await qf_client.get(f"/quant-foundry/jobs/{job_id}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["status"] == "completed"


async def test_get_unknown_job_returns_404(
    qf_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await qf_client.get("/quant-foundry/jobs/does-not-exist", headers=auth_headers)
    assert r.status_code == 404


async def test_list_jobs(qf_client: AsyncClient, auth_headers: dict[str, str]) -> None:
    job_id = "qf:train:list:1"
    idem = make_idempotency_key("training", "list", "gbm", "h1", "1")
    await qf_client.post(
        "/quant-foundry/jobs",
        headers=auth_headers,
        json={
            "job_id": job_id,
            "job_type": "training",
            "idempotency_key": idem,
            "request_payload": {
                "schema_version": 1,
                "job_id": job_id,
                "dataset_manifest_ref": "ds-1",
                "model_family": "gbm",
                "search_space": {},
                "random_seed": 1,
                "hardware_class": "m",
                "extra_constraints": {},
            },
        },
    )
    r = await qf_client.get("/quant-foundry/jobs", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert any(j["job_id"] == job_id for j in body)


# --- callback endpoint: HMAC auth, bad signature, duplicate -----------------


async def test_callback_rejects_bad_signature(
    qf_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # First create a job so the outbox has a record.
    job_id = "qf:train:cbsig:1"
    idem = make_idempotency_key("training", "cbsig", "gbm", "h1", "1")
    await qf_client.post(
        "/quant-foundry/jobs",
        headers=auth_headers,
        json={
            "job_id": job_id,
            "job_type": "training",
            "idempotency_key": idem,
            "request_payload": {
                "schema_version": 1,
                "job_id": job_id,
                "dataset_manifest_ref": "ds-1",
                "model_family": "gbm",
                "search_space": {},
                "random_seed": 1,
                "hardware_class": "m",
                "extra_constraints": {},
            },
        },
    )
    # Send a callback with a BAD signature (no bearer; HMAC headers).
    payload = b'{"job_id": "qf:train:cbsig:1"}'
    r = await qf_client.post(
        "/quant-foundry/callbacks/runpod",
        content=payload,
        headers={
            "X-QF-Job-Id": job_id,
            "X-QF-Timestamp": "1700000000",
            "X-QF-Signature": "deadbeef" * 8,
            "Content-Type": "application/json",
        },
    )
    assert r.status_code in (401, 400)
    body = r.json()
    assert "bad_signature" in str(body) or "signature" in str(body).lower()


async def test_callback_rejects_missing_signature_headers(
    qf_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    job_id = "qf:train:cbmiss:1"
    idem = make_idempotency_key("training", "cbmiss", "gbm", "h1", "1")
    await qf_client.post(
        "/quant-foundry/jobs",
        headers=auth_headers,
        json={
            "job_id": job_id,
            "job_type": "training",
            "idempotency_key": idem,
            "request_payload": {
                "schema_version": 1,
                "job_id": job_id,
                "dataset_manifest_ref": "ds-1",
                "model_family": "gbm",
                "search_space": {},
                "random_seed": 1,
                "hardware_class": "m",
                "extra_constraints": {},
            },
        },
    )
    # No HMAC headers at all -> reject.
    r = await qf_client.post(
        "/quant-foundry/callbacks/runpod",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code in (401, 400)


async def test_callback_unknown_job_rejected(
    qf_client: AsyncClient,
) -> None:
    payload = b'{"job_id": "nope"}'
    r = await qf_client.post(
        "/quant-foundry/callbacks/runpod",
        content=payload,
        headers={
            "X-QF-Job-Id": "qf:train:unknown:1",
            "X-QF-Timestamp": "1700000000",
            "X-QF-Signature": "deadbeef" * 8,
            "Content-Type": "application/json",
        },
    )
    assert r.status_code in (404, 400)


async def test_duplicate_callback_idempotent(
    qf_client: AsyncClient,
    auth_headers: dict[str, str],
    qf_enabled_gateway: QuantFoundryGateway,
) -> None:
    # Create + complete a job (local_mock processes immediately).
    job_id = "qf:infer:dupcb:1"
    idem = make_idempotency_key("inference", "dupcb", "gbm", "h1", "1")
    await qf_client.post(
        "/quant-foundry/jobs",
        headers=auth_headers,
        json={
            "job_id": job_id,
            "job_type": "inference",
            "idempotency_key": idem,
            "request_payload": {
                "schema_version": 1,
                "job_id": job_id,
                "artifact_ref": "a-1",
                "symbols": ["AAPL"],
                "horizons_ns": [3600000000000],
            },
        },
    )
    pred_count_before = len(qf_enabled_gateway.shadow_ledger.list())

    # Build a validly-signed callback with the SAME payload the dispatcher
    # produced, and send it twice. The processor must not double-apply.
    # (In local_mock the job is already COMPLETED, so the duplicate external
    # callback is an idempotent no-op.)
    import time as _time

    payload = b'{"job_id": "qf:infer:dupcb:1"}'
    ts = int(_time.time())
    sig = sign_callback(payload, secret="test-cb-secret", ts=ts, job_id=job_id)
    for _ in range(2):
        await qf_client.post(
            "/quant-foundry/callbacks/runpod",
            content=payload,
            headers={
                "X-QF-Job-Id": job_id,
                "X-QF-Timestamp": str(ts),
                "X-QF-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert len(qf_enabled_gateway.shadow_ledger.list()) == pred_count_before


# --- health + heartbeats ----------------------------------------------------


async def test_health_enabled(
    qf_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await qf_client.get("/quant-foundry/health", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["mode"] == "local_mock"
    assert body["shadow_only"] is True


async def test_heartbeats_enabled(
    qf_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await qf_client.get("/quant-foundry/heartbeats", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
