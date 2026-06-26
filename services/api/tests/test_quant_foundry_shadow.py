"""
TDD tests for TASK-0604 Quant Foundry shadow inference health endpoint.

Covers bearer-auth HTTP access to a read-only ``/quant-foundry/shadow/health``
endpoint that surfaces aggregate health metrics from the shadow prediction
ledger. The endpoint is additive, never writes, and never returns secrets or
raw callback payloads.

Acceptance criteria covered:
- 401 when no bearer token is provided.
- 503 when the gateway is absent (default disabled state).
- 200 + zero/null metrics when the gateway is present but the shadow ledger
  is empty (never crash).
- 200 + computed metrics when the shadow ledger has stored predictions.
"""

from __future__ import annotations

import pathlib

import pytest
from httpx import AsyncClient

from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.schemas import Authority, ShadowPrediction
from quant_foundry.shadow_ledger import compute_batch_hash


def _gateway(base_dir: pathlib.Path, *, enabled: bool = True) -> QuantFoundryGateway:
    return QuantFoundryGateway(
        enabled=enabled,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-cb-secret",
        base_dir=base_dir,
    )


def _prediction(
    *,
    prediction_id: str,
    model_id: str,
    ts_event: int,
    latency_ms: float | None = None,
    feature_availability: dict[str, bool] | None = None,
) -> dict[str, object]:
    """Build a valid ShadowPrediction dict for the real ShadowLedger."""
    sp = ShadowPrediction(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="BTC-USD",
        ts_event=ts_event,
        horizon_ns=60_000_000_000,
        direction=0.1,
        magnitude=0.05,
        confidence=0.6,
        authority=Authority.SHADOW_ONLY,
        feature_availability=feature_availability,
        latency_ms=latency_ms,
    )
    return sp.model_dump()


@pytest.fixture
async def qf_shadow_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    """ASGI client with an enabled Quant Foundry gateway on app.state."""
    from api.main import app

    app.state.quant_foundry_gateway = _gateway(tmp_path / "qf-shadow")
    return client


@pytest.fixture
async def qf_shadow_disabled_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    """ASGI client with a present-but-disabled gateway."""
    from api.main import app

    app.state.quant_foundry_gateway = _gateway(
        tmp_path / "qf-shadow-disabled",
        enabled=False,
    )
    return client


@pytest.fixture
async def qf_shadow_no_gateway_client(client: AsyncClient) -> AsyncClient:
    """ASGI client with no gateway on app.state (absent)."""
    from api.main import app

    # Explicitly remove the gateway so the route sees the absent state.
    if hasattr(app.state, "quant_foundry_gateway"):
        delattr(app.state, "quant_foundry_gateway")
    return client


# ---------------------------------------------------------------------------
# 401 auth
# ---------------------------------------------------------------------------


async def test_shadow_health_requires_auth(qf_shadow_client: AsyncClient) -> None:
    # Given: no Authorization header.
    # When: the operator calls the shadow health endpoint.
    response = await qf_shadow_client.get("/quant-foundry/shadow/health")

    # Then: bearer auth is required.
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 503 gateway absent
# ---------------------------------------------------------------------------


async def test_shadow_health_returns_503_when_gateway_absent(
    qf_shadow_no_gateway_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: no Quant Foundry gateway configured on app.state.
    # When: shadow health is requested.
    response = await qf_shadow_no_gateway_client.get(
        "/quant-foundry/shadow/health",
        headers=auth_headers,
    )

    # Then: 503 disabled state, never crash.
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# 200 + safe defaults
# ---------------------------------------------------------------------------


async def test_shadow_health_disabled_gateway_returns_safe_state(
    qf_shadow_disabled_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: a present-but-disabled gateway.
    # When: shadow health is requested.
    response = await qf_shadow_disabled_client.get(
        "/quant-foundry/shadow/health",
        headers=auth_headers,
    )

    # Then: the endpoint reports the disabled shape (enabled=false, zero counts,
    # null metrics). It must NOT raise 500 for the disabled state.
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["models_running"] == 0
    assert body["prediction_count"] == 0
    assert body["settled_count"] == 0
    assert body["latest_prediction_ts"] is None
    assert body["latency_p50_ms"] is None
    assert body["latency_p95_ms"] is None
    assert body["feature_availability"] is None
    assert body["callback_rejection_rate"] is None
    assert body["settlement_lag_seconds"] is None
    assert body["circuit_breaker_state"] == "closed"


async def test_shadow_health_empty_ledger_returns_nulls_not_crash(
    qf_shadow_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with no shadow ledger records.
    # When: shadow health is requested.
    response = await qf_shadow_client.get(
        "/quant-foundry/shadow/health",
        headers=auth_headers,
    )

    # Then: 200 with enabled=true, zero counts, null latency / latest_ts.
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["prediction_count"] == 0
    assert body["settled_count"] == 0
    assert body["models_running"] == 0
    assert body["latest_prediction_ts"] is None
    assert body["latency_p50_ms"] is None
    assert body["latency_p95_ms"] is None
    assert body["feature_availability"] is None
    # rejection tracking is not yet durable — null with the documented note
    assert body["callback_rejection_rate"] is None
    assert body["settlement_lag_seconds"] is None
    # No real drift data — circuit breaker is "closed" by default.
    assert body["circuit_breaker_state"] == "closed"


# ---------------------------------------------------------------------------
# 200 + populated metrics
# ---------------------------------------------------------------------------


async def test_shadow_health_populated_ledger_reports_metrics(
    qf_shadow_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with stored shadow predictions across 2 models
    # and varying latency / feature availability.
    from api.main import app

    gateway: QuantFoundryGateway = app.state.quant_foundry_gateway
    ledger = gateway.shadow_ledger_real()
    predictions = [
        _prediction(
            prediction_id="pred-1",
            model_id="model-alpha",
            ts_event=1_000,
            latency_ms=10.0,
            feature_availability={"f1": True, "f2": True},
        ),
        _prediction(
            prediction_id="pred-2",
            model_id="model-alpha",
            ts_event=2_000,
            latency_ms=20.0,
            feature_availability={"f1": False, "f2": False},
        ),
        _prediction(
            prediction_id="pred-3",
            model_id="model-beta",
            ts_event=3_000,
            latency_ms=30.0,
            feature_availability={"f1": True, "f2": False},
        ),
    ]
    batch_hash = compute_batch_hash(predictions)
    ledger.store_batch(predictions, batch_hash=batch_hash)

    # When: shadow health is requested.
    response = await qf_shadow_client.get(
        "/quant-foundry/shadow/health",
        headers=auth_headers,
    )

    # Then: counts + latency + latest_ts + models_running + feature_availability
    # reflect the stored batch. No secrets, no raw callbacks.
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["prediction_count"] == 3
    assert body["models_running"] == 2
    assert body["latest_prediction_ts"] == pytest.approx(3_000.0)
    # p50 of [10, 20, 30] == 20.0; p95 with linear interpolation at index 1.9
    # of [10, 20, 30] == 20 * 0.1 + 30 * 0.9 == 29.0 (the gateway must not
    # crash on small samples; linear interpolation is the documented method).
    assert body["latency_p50_ms"] == pytest.approx(20.0)
    assert body["latency_p95_ms"] == pytest.approx(29.0)
    # Average feature availability: 3 available out of 6 total = 0.5.
    # (pred-1: 2/2, pred-2: 0/2, pred-3: 1/2 → 3/6 = 0.5)
    assert body["feature_availability"] == pytest.approx(0.5)
    # Settlement + rejection are not durable in MVP.
    assert body["settlement_lag_seconds"] is None
    assert body["callback_rejection_rate"] is None
    assert body["circuit_breaker_state"] == "closed"
    # Sanity: no raw callback payloads, no secrets.
    for forbidden in ("raw_payload", "callback_secret", "secret", "payload"):
        assert forbidden not in body


async def test_shadow_health_repeated_calls_are_idempotent(
    qf_shadow_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with no stored predictions.
    # When: shadow health is requested twice.
    first = await qf_shadow_client.get(
        "/quant-foundry/shadow/health",
        headers=auth_headers,
    )
    second = await qf_shadow_client.get(
        "/quant-foundry/shadow/health",
        headers=auth_headers,
    )

    # Then: both calls return identical safe-empty shapes (no side effects).
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
