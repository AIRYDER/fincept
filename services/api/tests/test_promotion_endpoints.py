"""
TDD tests for promotion POST endpoints (TASK-B3).

Covers:
- Submit → approve flow (gate approves with sufficient evidence).
- Submit → reject flow (human rejects with reason).
- Missing model → 404.
- Insufficient evidence → 422 (gate fails closed).
- Bearer auth required on all POST endpoints.
- Disabled gateway → 503.
"""

from __future__ import annotations

import pathlib
import time

import pytest
from httpx import AsyncClient
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outcomes import SettlementRecord, SettlementStatus


def _gateway(base_dir: pathlib.Path, *, enabled: bool = True) -> QuantFoundryGateway:
    return QuantFoundryGateway(
        enabled=enabled,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-cb-secret",
        base_dir=base_dir,
    )


def _dossier(model_id: str, *, trial_count: int = 3) -> DossierRecord:
    return DossierRecord(
        model_id=model_id,
        artifact_manifest_id=f"artifact-{model_id}",
        artifact_sha256=f"sha256-{model_id}",
        dataset_manifest_id="dataset-test",
        feature_schema_hash="fs-hash",
        label_schema_hash="ls-hash",
        trial_count=trial_count,
        status=DossierStatus.CANDIDATE,
    )


def _settled_record(
    *,
    prediction_id: str,
    model_id: str,
    realized_net: float = 0.002,
    settled_at_ns: int | None = None,
) -> SettlementRecord:
    ts = settled_at_ns if settled_at_ns is not None else time.time_ns()
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="BTC-USD",
        ts_event=ts - 1000,
        horizon_ns=86_400_000_000_000,
        status=SettlementStatus.SETTLED,
        settled_at_ns=ts,
        realized_return_gross=realized_net,
        realized_return_net=realized_net,
        abnormal_return=None,
        brier=0.2,
        calibration_bucket="medium",
        cost_model_version="cm-v1",
        decision_window_start=ts - 1000,
        decision_window_end=ts,
    )


def _write_settlements(base_dir: pathlib.Path, model_id: str, count: int) -> None:
    ledger_dir = base_dir / "settlements"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / f"{model_id}.settlements.jsonl"
    now_ns = time.time_ns()
    returns = [0.001 + i * 0.0001 for i in range(count)]
    with path.open("a", encoding="utf-8") as f:
        for i in range(count):
            rec = _settled_record(
                prediction_id=f"{model_id}-pred-{i}",
                model_id=model_id,
                realized_net=returns[i % len(returns)],
                settled_at_ns=now_ns - i * 1_000_000_000,
            )
            f.write(rec.to_json() + "\n")


@pytest.fixture
async def qf_promo_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    from api.main import app

    app.state.quant_foundry_gateway = _gateway(tmp_path / "qf")
    return client


@pytest.fixture
async def qf_disabled_promo_client(
    client: AsyncClient,
    tmp_path: pathlib.Path,
) -> AsyncClient:
    from api.main import app

    app.state.quant_foundry_gateway = _gateway(tmp_path / "qf-disabled", enabled=False)
    return client


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


async def test_submit_promotion_requires_auth(qf_promo_client: AsyncClient) -> None:
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={"model_id": "m1", "target_level": "shadow_approved", "review_note": ""},
    )
    assert response.status_code == 401


async def test_approve_promotion_requires_auth(qf_promo_client: AsyncClient) -> None:
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/approve",
        json={"model_id": "m1", "review_note": ""},
    )
    assert response.status_code == 401


async def test_reject_promotion_requires_auth(qf_promo_client: AsyncClient) -> None:
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/reject",
        json={
            "model_id": "m1",
            "review_note": "",
            "rejection_reason": "blocking_issue",
        },
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Disabled gateway tests
# ---------------------------------------------------------------------------


async def test_submit_promotion_disabled_returns_503(
    qf_disabled_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await qf_disabled_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={"model_id": "m1", "target_level": "shadow_approved", "review_note": ""},
        headers=auth_headers,
    )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Submit → approve flow
# ---------------------------------------------------------------------------


async def test_submit_then_approve_flow(
    qf_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    from api.main import app

    gw: QuantFoundryGateway = app.state.quant_foundry_gateway
    gw.dossier_registry().register(_dossier("model-promo"))
    _write_settlements(gw.base_dir, "model-promo", 12)

    # Submit.
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={
            "model_id": "model-promo",
            "target_level": "shadow_approved",
            "review_note": "looks good",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["entry"]["request"]["model_id"] == "model-promo"
    assert body["entry"]["request"]["target_level"] == "shadow_approved"

    # Approve.
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/approve",
        json={"model_id": "model-promo", "review_note": "approved by operator"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    receipt = body["receipt"]
    assert receipt["decision"] == "approved"
    assert (
        gw.dossier_registry().get("model-promo").status == DossierStatus.SHADOW_APPROVED
    )
    assert [
        d.model_id
        for d in gw.dossier_registry().list(status=DossierStatus.SHADOW_APPROVED)
    ] == ["model-promo"]


# ---------------------------------------------------------------------------
# Submit → reject flow
# ---------------------------------------------------------------------------


async def test_submit_then_reject_flow(
    qf_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    from api.main import app

    gw: QuantFoundryGateway = app.state.quant_foundry_gateway
    gw.dossier_registry().register(_dossier("model-reject"))
    _write_settlements(gw.base_dir, "model-reject", 12)

    # Submit.
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={
            "model_id": "model-reject",
            "target_level": "shadow_approved",
            "review_note": "considering",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200

    # Reject.
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/reject",
        json={
            "model_id": "model-reject",
            "review_note": "not enough evidence",
            "rejection_reason": "blocking_issue",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    receipt = body["receipt"]
    assert receipt["decision"] == "rejected"
    assert receipt["rejection_reason"] == "blocking_issue"


# ---------------------------------------------------------------------------
# Missing model → 404
# ---------------------------------------------------------------------------


async def test_submit_promotion_missing_model_returns_404(
    qf_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={
            "model_id": "nonexistent-model",
            "target_level": "shadow_approved",
            "review_note": "",
        },
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_approve_promotion_no_pending_returns_404(
    qf_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/approve",
        json={"model_id": "no-pending", "review_note": ""},
        headers=auth_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Invalid target level → 422
# ---------------------------------------------------------------------------


async def test_submit_promotion_invalid_target_level_returns_422(
    qf_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    from api.main import app

    gw: QuantFoundryGateway = app.state.quant_foundry_gateway
    gw.dossier_registry().register(_dossier("model-bad-level"))

    response = await qf_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={
            "model_id": "model-bad-level",
            "target_level": "invalid_level",
            "review_note": "",
        },
        headers=auth_headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# MVP level limit: limited_live_approved → rejected receipt
# ---------------------------------------------------------------------------


async def test_submit_then_approve_limited_live_approved_rejected_mvp_limit(
    qf_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    from api.main import app

    gw: QuantFoundryGateway = app.state.quant_foundry_gateway
    gw.dossier_registry().register(_dossier("model-live-limit"))
    _write_settlements(gw.base_dir, "model-live-limit", 12)

    # Submit a promotion to limited_live_approved (above MVP max).
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={
            "model_id": "model-live-limit",
            "target_level": "limited_live_approved",
            "review_note": "attempting limited live pilot",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True

    # Approve — gate should reject with MVP_LEVEL_LIMIT.
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/approve",
        json={"model_id": "model-live-limit", "review_note": "approve attempt"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    receipt = body["receipt"]
    assert receipt["decision"] == "rejected"
    assert receipt["rejection_reason"] == "mvp_level_limit"


# ---------------------------------------------------------------------------
# Gate fails closed: insufficient evidence → rejected receipt
# ---------------------------------------------------------------------------


async def test_approve_with_insufficient_evidence_fails_closed(
    qf_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    from api.main import app

    gw: QuantFoundryGateway = app.state.quant_foundry_gateway
    gw.dossier_registry().register(_dossier("model-few-evidence"))
    # Only 3 settlements — below min_settled_samples=10.
    _write_settlements(gw.base_dir, "model-few-evidence", 3)

    # Submit.
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={
            "model_id": "model-few-evidence",
            "target_level": "shadow_approved",
            "review_note": "trying with little evidence",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200

    # Approve — gate should fail closed (REJECTED, not APPROVED).
    response = await qf_promo_client.post(
        "/quant-foundry/promotion/approve",
        json={"model_id": "model-few-evidence", "review_note": "approve attempt"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    receipt = body["receipt"]
    assert receipt["decision"] == "rejected"
    assert receipt["rejection_reason"] == "insufficient_evidence"
    assert gw.dossier_registry().get("model-few-evidence").status == (
        DossierStatus.CANDIDATE
    )


# ---------------------------------------------------------------------------
# Completed promotions visible after processing
# ---------------------------------------------------------------------------


async def test_completed_promotions_visible_after_reject(
    qf_promo_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    from api.main import app

    gw: QuantFoundryGateway = app.state.quant_foundry_gateway
    gw.dossier_registry().register(_dossier("model-completed"))
    _write_settlements(gw.base_dir, "model-completed", 12)

    # Submit + reject.
    await qf_promo_client.post(
        "/quant-foundry/promotion/submit",
        json={
            "model_id": "model-completed",
            "target_level": "shadow_approved",
            "review_note": "",
        },
        headers=auth_headers,
    )
    await qf_promo_client.post(
        "/quant-foundry/promotion/reject",
        json={
            "model_id": "model-completed",
            "review_note": "rejected",
            "rejection_reason": "blocking_issue",
        },
        headers=auth_headers,
    )

    # Check completed endpoint.
    response = await qf_promo_client.get(
        "/quant-foundry/promotion/completed",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 1
    assert any(r["request"]["model_id"] == "model-completed" for r in body)
