"""Tests for the public /health endpoint."""

from __future__ import annotations

from httpx import AsyncClient


async def test_health_returns_ok_without_auth(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "version" in body


async def test_health_does_not_require_bearer(client: AsyncClient) -> None:
    """Load balancers and uptime probes should be able to hit /health
    without any authentication setup."""
    response = await client.get("/health")
    assert response.status_code != 401


# ---------------------------------------------------------------------------
# Readiness (TASK-0202) — TDD: tests first
# ---------------------------------------------------------------------------

async def test_readiness_requires_auth(client: AsyncClient) -> None:
    """Detailed readiness must be protected (operator dashboard supplies token)."""
    response = await client.get("/health/readiness")
    assert response.status_code == 401


async def test_readiness_returns_categories_and_states(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Readiness endpoint returns the required categories with safe states.
    Never leaks secrets or stacks. Supports disabled/skipped/stale.
    """
    response = await client.get("/health/readiness", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()

    assert "checks" in data
    checks = {c["id"]: c for c in data["checks"]}

    required_ids = {
        "api",
        "redis",
        "timescale",
        "verification_receipt",
        "provider_freshness",
        "news_impact",
        "dashboard_tests",
    }
    assert required_ids.issubset(checks.keys())

    for cid, check in checks.items():
        assert "state" in check
        assert check["state"] in {"pass", "warn", "fail", "skipped", "disabled", "stale"}
        assert "label" in check
        assert "detail" in check
        # Never leak secrets
        detail_lower = check["detail"].lower()
        assert "secret" not in detail_lower
        assert "token" not in detail_lower
        assert "password" not in detail_lower

    # disabled must not be treated as failure
    if "quant_foundry" in checks:
        assert checks["quant_foundry"]["state"] in {"disabled", "skipped"}

    # receipt link present for UI
    assert "receipt_url" in data or "receipts" in data
