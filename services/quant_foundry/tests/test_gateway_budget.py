"""
TDD tests for budget-guard enforcement in QuantFoundryGateway.create_job.

The budget guard (TASK-0901) is wired into the gateway so that GPU spend fails
closed: a paid job whose estimated cost would exceed the monthly ceiling is
rejected BEFORE it is enqueued, and the kill switch blocks all paid jobs. These
tests prove the enforcement and, critically, that a rejected job is never
enqueued (no outbox record).

Acceptance:
- Over-budget paid job -> ok=False, error_code='budget_exceeded', NOT enqueued.
- Kill switch active -> ok=False, error_code='budget_kill_switch', NOT enqueued.
- Under-budget paid job -> reserved and runs (job enqueued + completed).
- Zero-cost job (budget_cents None/0) -> always allowed (mock loop unaffected).
- No guard configured -> create_job behaves exactly as before (unaffected).
"""

from __future__ import annotations

import pathlib

import pytest
from quant_foundry.budget import BudgetGuard
from quant_foundry.gateway import QuantFoundryGateway


def _gateway(
    base: pathlib.Path,
    *,
    monthly_budget_cents: int = 0,
    kill_switch_enabled: bool = False,
    with_guard: bool = True,
) -> QuantFoundryGateway:
    guard = None
    if with_guard:
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


def _training_payload(job_id: str) -> dict:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "ds-manifest-1",
        "model_family": "gbm",
        "search_space": {"n_estimators": [100, 200]},
        "random_seed": 42,
        "hardware_class": "mock-gpu",
        "extra_constraints": {},
    }


def _create(gw: QuantFoundryGateway, *, job_id: str, budget_cents: int | None):
    return gw.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"qf:training:{job_id}",
        request_payload=_training_payload(job_id),
        budget_cents=budget_cents,
    )


def test_over_budget_job_rejected_and_not_enqueued(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path, monthly_budget_cents=100)
    result = _create(gw, job_id="qf:train:over:1", budget_cents=500)

    assert result["ok"] is False
    assert result["error_code"] == "budget_exceeded"
    # Fail-closed invariant: the job must NOT have been enqueued.
    assert gw.outbox.get("qf:train:over:1") is None


def test_kill_switch_blocks_paid_job_and_not_enqueued(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path, monthly_budget_cents=10_000, kill_switch_enabled=True)
    result = _create(gw, job_id="qf:train:killed:1", budget_cents=50)

    assert result["ok"] is False
    assert result["error_code"] == "budget_kill_switch"
    assert gw.outbox.get("qf:train:killed:1") is None


def test_under_budget_job_reserved_and_runs(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path, monthly_budget_cents=10_000)
    result = _create(gw, job_id="qf:train:ok:1", budget_cents=50)

    assert result.get("ok") is not False
    assert result["enabled"] is True
    assert "error_code" not in result
    # Job was enqueued (and, in local_mock, processed through the loop).
    assert gw.outbox.get("qf:train:ok:1") is not None


def test_zero_cost_job_always_allowed(tmp_path: pathlib.Path) -> None:
    # Zero budget ceiling, but a zero-cost job must still run.
    gw = _gateway(tmp_path, monthly_budget_cents=0)
    result = _create(gw, job_id="qf:train:free:1", budget_cents=0)

    assert result.get("ok") is not False
    assert "error_code" not in result
    assert gw.outbox.get("qf:train:free:1") is not None


def test_none_budget_treated_as_zero_cost(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path, monthly_budget_cents=0)
    result = _create(gw, job_id="qf:train:none:1", budget_cents=None)

    assert result.get("ok") is not False
    assert "error_code" not in result
    assert gw.outbox.get("qf:train:none:1") is not None


def test_no_guard_configured_is_unaffected(tmp_path: pathlib.Path) -> None:
    # Without a guard, even a large budget_cents is enqueued (legacy behaviour).
    gw = _gateway(tmp_path, with_guard=False)
    result = _create(gw, job_id="qf:train:noguard:1", budget_cents=999_999)

    assert result.get("ok") is not False
    assert "error_code" not in result
    assert gw.outbox.get("qf:train:noguard:1") is not None
