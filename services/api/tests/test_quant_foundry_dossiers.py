"""
TDD tests for TASK-0802 Quant Foundry read-only dossier endpoints.

Covers bearer-auth HTTP access to dossiers, tournament, and promotion read
surfaces without adding write endpoints or trading side effects.
"""

from __future__ import annotations

import pathlib

import pytest
from httpx import AsyncClient

from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.gateway import QuantFoundryGateway


def _gateway(base_dir: pathlib.Path, *, enabled: bool = True) -> QuantFoundryGateway:
    return QuantFoundryGateway(
        enabled=enabled,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-cb-secret",
        base_dir=base_dir,
    )


def _dossier(
    model_id: str, *, status: DossierStatus = DossierStatus.CANDIDATE
) -> DossierRecord:
    return DossierRecord(
        model_id=model_id,
        artifact_manifest_id=f"artifact-{model_id}",
        artifact_sha256=f"sha256-{model_id}",
        dataset_manifest_id="dataset-2026-06-23",
        dataset_manifest_ref="reports/qf/dataset.json",
        feature_schema_hash="feature-schema-hash",
        label_schema_hash="label-schema-hash",
        code_git_sha="abc1234",
        lockfile_hash="lock-hash",
        container_image_digest="container@sha256:abc",
        random_seed=42,
        hardware_class="local-cpu",
        trial_count=3,
        training_metrics={"brier": 0.18, "accuracy": 0.61},
        settlement_evidence_refs=["settlement:m1:001"],
        shadow_prediction_refs=["shadow:m1:001"],
        status=status,
    )


@pytest.fixture
async def qf_dossier_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    from api.main import app

    app.state.quant_foundry_gateway = _gateway(tmp_path / "qf")
    return client


@pytest.fixture
async def qf_disabled_dossier_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    from api.main import app

    app.state.quant_foundry_gateway = _gateway(tmp_path / "qf-disabled", enabled=False)
    return client


async def test_dossiers_endpoint_requires_auth(qf_dossier_client: AsyncClient) -> None:
    # Given: no Authorization header.
    # When: the operator calls the dossier list endpoint.
    response = await qf_dossier_client.get("/quant-foundry/dossiers")
    # Then: bearer auth is required.
    assert response.status_code == 401


async def test_dossiers_disabled_returns_empty_list(
    qf_disabled_dossier_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: a configured but disabled Quant Foundry gateway.
    # When: dossiers are listed.
    response = await qf_disabled_dossier_client.get(
        "/quant-foundry/dossiers",
        headers=auth_headers,
    )
    # Then: disabled is a safe empty state, not a server error.
    assert response.status_code == 200
    assert response.json() == []


async def test_dossiers_empty_registry_returns_empty_list(
    qf_dossier_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with no dossier registry records.
    # When: dossiers are listed.
    response = await qf_dossier_client.get(
        "/quant-foundry/dossiers", headers=auth_headers
    )
    # Then: the endpoint returns an empty list.
    assert response.status_code == 200
    assert response.json() == []


async def test_dossiers_list_returns_registered_dossiers(
    qf_dossier_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: two persisted dossier records under the gateway base directory.
    from api.main import app

    gateway: QuantFoundryGateway = app.state.quant_foundry_gateway
    gateway.dossier_registry().register(_dossier("model-alpha"))
    gateway.dossier_registry().register(
        _dossier("model-beta", status=DossierStatus.SHADOW_APPROVED),
    )

    # When: dossiers are listed without a filter.
    response = await qf_dossier_client.get(
        "/quant-foundry/dossiers", headers=auth_headers
    )

    # Then: all dossiers are returned with their immutable artifact metadata.
    assert response.status_code == 200
    body = response.json()
    assert [row["model_id"] for row in body] == ["model-alpha", "model-beta"]
    assert body[0]["artifact_sha256"] == "sha256-model-alpha"
    assert body[0]["status"] == "candidate"


async def test_dossiers_list_filters_by_status(
    qf_dossier_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: candidate and shadow-approved dossiers.
    from api.main import app

    gateway: QuantFoundryGateway = app.state.quant_foundry_gateway
    gateway.dossier_registry().register(_dossier("candidate-model"))
    gateway.dossier_registry().register(
        _dossier("shadow-model", status=DossierStatus.SHADOW_APPROVED),
    )

    # When: the list endpoint is filtered by status.
    response = await qf_dossier_client.get(
        "/quant-foundry/dossiers?status=shadow_approved",
        headers=auth_headers,
    )

    # Then: only matching dossiers are returned.
    assert response.status_code == 200
    assert [row["model_id"] for row in response.json()] == ["shadow-model"]


async def test_dossier_detail_returns_registered_model(
    qf_dossier_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: a registered dossier.
    from api.main import app

    gateway: QuantFoundryGateway = app.state.quant_foundry_gateway
    gateway.dossier_registry().register(_dossier("detail-model"))

    # When: the detail endpoint is called.
    response = await qf_dossier_client.get(
        "/quant-foundry/dossiers/detail-model",
        headers=auth_headers,
    )

    # Then: the exact dossier is returned.
    assert response.status_code == 200
    body = response.json()
    assert body["model_id"] == "detail-model"
    assert body["trial_count"] == 3


async def test_dossier_detail_unknown_model_returns_404(
    qf_dossier_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: no dossier for the requested model.
    # When: the detail endpoint is called.
    response = await qf_dossier_client.get(
        "/quant-foundry/dossiers/unknown-model",
        headers=auth_headers,
    )
    # Then: the API returns 404.
    assert response.status_code == 404


async def test_tournament_and_promotion_reads_start_empty(
    qf_dossier_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    # Given: an enabled gateway with no tournament entries or review requests.
    paths = (
        "/quant-foundry/tournament/leaderboard",
        "/quant-foundry/promotion/queue",
        "/quant-foundry/promotion/completed",
    )

    # When/Then: each read-only endpoint returns an empty list.
    for path in paths:
        response = await qf_dossier_client.get(path, headers=auth_headers)
        assert response.status_code == 200, path
        assert response.json() == []
