"""
TDD tests for budget-guard HTTP mapping on POST /quant-foundry/jobs.

The gateway fails closed on budget; the route maps those rejections to HTTP:
- budget_exceeded   -> 402 Payment Required
- budget_kill_switch -> 429 Too Many Requests

These tests install a gateway with a BudgetGuard on app.state and assert the
status codes, mirroring the qf_client fixture pattern from test_quant_foundry.
"""

from __future__ import annotations

import pathlib

import pytest
from httpx import AsyncClient

from quant_foundry.budget import BudgetGuard
from quant_foundry.gateway import QuantFoundryGateway


def _budget_gateway(
    base: pathlib.Path,
    *,
    monthly_budget_cents: int,
    kill_switch_enabled: bool = False,
) -> QuantFoundryGateway:
    guard = BudgetGuard(
        base_dir=base / "budget",
        monthly_budget_cents=monthly_budget_cents,
        kill_switch_enabled=kill_switch_enabled,
    )
    return QuantFoundryGateway(
        enabled=True,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-cb-secret",
        base_dir=base / "qf",
        budget_guard=guard,
    )


@pytest.fixture
async def qf_over_budget_client(
    client: AsyncClient, tmp_path: pathlib.Path
) -> AsyncClient:
    from api.main import app

    app.state.quant_foundry_gateway = _budget_gateway(
        tmp_path, monthly_budget_cents=100
    )
    return client


@pytest.fixture
async def qf_kill_switch_client(
    client: AsyncClient, tmp_path: pathlib.Path
) -> AsyncClient:
    from api.main import app

    app.state.quant_foundry_gateway = _budget_gateway(
        tmp_path, monthly_budget_cents=10_000, kill_switch_enabled=True
    )
    return client


def _job_body(budget_cents: int) -> dict:
    job_id = "qf:train:budget:1"
    return {
        "job_id": job_id,
        "job_type": "training",
        "idempotency_key": "qf:training:budget:1",
        "request_payload": {
            "schema_version": 1,
            "job_id": job_id,
            "dataset_manifest_ref": "ds-manifest-1",
            "model_family": "gbm",
            "search_space": {"n_estimators": [100, 200]},
            "random_seed": 42,
            "hardware_class": "mock-gpu",
            "extra_constraints": {},
        },
        "budget_cents": budget_cents,
    }


async def test_over_budget_returns_402(
    qf_over_budget_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await qf_over_budget_client.post(
        "/quant-foundry/jobs", headers=auth_headers, json=_job_body(500)
    )
    assert r.status_code == 402


async def test_kill_switch_returns_429(
    qf_kill_switch_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await qf_kill_switch_client.post(
        "/quant-foundry/jobs", headers=auth_headers, json=_job_body(50)
    )
    assert r.status_code == 429


async def test_under_budget_succeeds(
    qf_over_budget_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # 50c is within the 100c ceiling -> normal 200 response.
    r = await qf_over_budget_client.post(
        "/quant-foundry/jobs", headers=auth_headers, json=_job_body(50)
    )
    assert r.status_code == 200
    assert r.json().get("error_code") is None
