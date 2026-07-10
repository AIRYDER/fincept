"""
quant_foundry.local_training_dispatch — Stage Task 1 dispatch pipeline.

This is the operator-facing staging pipeline for Task 1 of NEXT_FIVE_TASKS.md
("Train First Real Baseline Model Family"). It builds a
``TrainingManifest``, consults ``BudgetGuard`` for cost authorization,
dispatches the job through the deterministic ``LocalTrainer`` (no live
RunPod), and emits a ``DispatchReceipt`` for audit.

Key invariants:
- **No live RunPod.** ``LocalTrainer`` is CPU-only and deterministic. The
  proof produced here is end-to-end (manifest -> dispatch -> artifact ->
  dossier), but no GPU / network round-trip is performed.
- **Budget fails closed.** ``BudgetGuard.check_and_reserve`` is called
  before the dispatch. A budget rejection returns ``DispatchReceipt``
  with ``status=BUDGET_REJECTED`` — the dispatch is NOT attempted.
- **Walk-forward validated.** The dispatch script derives the
  ``(train, val, test)`` split from the manifest and pins it on the
  request's ``extra_constraints`` so the trainer honors it.
- **No secrets.** The dispatch function accepts only the manifest,
  budget guard, and a callback secret. The manifest's constructor
  already rejects secret-shaped values; the dispatch adds no logging of
  raw content.
- **Authority enforced.** The trained artifact always carries
  ``Authority.SHADOW_ONLY`` — the dispatch cannot elevate authority.

File-disjoint from all active builders. Imports from ``budget.py``,
``dataset_manifest.py``, ``schemas.py``, ``training_manifest.py``, and
``runpod_training.py`` only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import time
from enum import StrEnum
from typing import Any

from quant_foundry.budget import BudgetDecision, BudgetGuard
from quant_foundry.runpod_training import (
    LocalTrainer,
    RunPodTrainingHandler,
    TrainingFailure,
    TrainingResult,
)
from quant_foundry.schemas import (
    Authority,
    RunPodTrainingRequest,
)
from quant_foundry.training_manifest import (
    TrainingManifest,
    WalkForwardWindow,
    derive_walk_forward_window,
)


class DispatchStatus(StrEnum):
    """Outcome status of a baseline training dispatch."""

    DISPATCHED = "dispatched"
    BUDGET_REJECTED = "budget_rejected"
    TRAINER_FAILED = "trainer_failed"
    VALIDATION_ERROR = "validation_error"


@dataclasses.dataclass(frozen=True)
class DispatchReceipt:
    """Immutable record of one staged dispatch.

    The receipt carries the manifest reference, the dispatched job_id,
    the artifact id + sha256, the dossier id, the budget decision, the
    walk-forward window, the dispatch status, and the wall-clock
    duration. ``to_dict`` is JSON-serializable for audit / persistence.
    """

    receipt_id: str
    manifest_id: str
    manifest_content_hash: str
    job_id: str
    feature_lake_manifest_ref: str
    feature_lake_manifest_hash: str
    model_family: str
    budget_decision: BudgetDecision
    walk_forward: WalkForwardWindow
    status: DispatchStatus
    artifact_id: str | None
    artifact_sha256: str | None
    dossier_id: str | None
    dossier_authority: str
    started_at_ns: int
    ended_at_ns: int
    error_code: str | None
    error_summary: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "manifest_id": self.manifest_id,
            "manifest_content_hash": self.manifest_content_hash,
            "job_id": self.job_id,
            "feature_lake_manifest_ref": self.feature_lake_manifest_ref,
            "feature_lake_manifest_hash": self.feature_lake_manifest_hash,
            "model_family": self.model_family,
            "budget_decision": {
                "allowed": self.budget_decision.allowed,
                "reason": self.budget_decision.reason,
                "job_type": self.budget_decision.job_type,
                "amount_cents": self.budget_decision.amount_cents,
                "monthly_budget_cents": self.budget_decision.monthly_budget_cents,
                "spent_cents": self.budget_decision.spent_cents,
                "remaining_cents": self.budget_decision.remaining_cents,
                "year_month": self.budget_decision.year_month,
            },
            "walk_forward": self.walk_forward.to_dict(),
            "status": self.status.value,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "dossier_id": self.dossier_id,
            "dossier_authority": self.dossier_authority,
            "started_at_ns": self.started_at_ns,
            "ended_at_ns": self.ended_at_ns,
            "duration_seconds": (self.ended_at_ns - self.started_at_ns) / 1e9,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
        }


@dataclasses.dataclass
class LocalTrainingDispatcher:
    """Stage Task 1 dispatch pipeline.

    Args:
        budget_guard: ``BudgetGuard`` consulted BEFORE dispatch.
        callback_secret: HMAC secret used by the local trainer to sign
            the callback envelope. Required.
        trainer: optional ``LocalTrainer`` override (used by tests).
        worker_id: identifier for the worker instance; recorded on the
            callback envelope for audit.

    Invariants:
    - A zero-cost manifest (``budget_cents == 0``) bypasses the budget
      guard (consistent with the system-wide zero-cost rule) but still
      records the decision on the receipt.
    - The dispatch NEVER elevates authority. The trained dossier carries
      ``Authority.SHADOW_ONLY`` (enforced by the trainer).
    - On any trainer failure, the dispatch returns ``TRAINER_FAILED``
      with the error code; no partial artifact is recorded.
    """

    budget_guard: BudgetGuard
    callback_secret: str
    trainer: LocalTrainer = dataclasses.field(default_factory=LocalTrainer)
    worker_id: str = "local-trainer-1"

    def dispatch(
        self,
        manifest: TrainingManifest,
        *,
        job_id: str,
        as_of_ts: int,
    ) -> DispatchReceipt:
        """Run the Stage Task 1 dispatch pipeline for one ``manifest``.

        Steps:
        1. Derive the walk-forward window.
        2. Translate the manifest into a ``RunPodTrainingRequest``.
        3. Check ``BudgetGuard``. Reject if not allowed.
        4. Run the local trainer (``RunPodTrainingHandler``).
        5. Record the result on a ``DispatchReceipt``.
        """
        started_at_ns = time.time_ns()
        receipt_id = (
            "receipt-"
            + hashlib.sha256(
                f"{manifest.manifest_id}:{job_id}:{started_at_ns}".encode()
            ).hexdigest()[:16]
        )

        # 1. Walk-forward window.
        try:
            wfw = derive_walk_forward_window(
                train_window_ns=manifest.train_window_ns,
                val_window_ns=manifest.val_window_ns,
                test_window_ns=manifest.test_window_ns,
                label_horizon_ns=manifest.label_horizon_ns,
                as_of_ts=as_of_ts,
            )
        except ValueError as exc:
            return DispatchReceipt(
                receipt_id=receipt_id,
                manifest_id=manifest.manifest_id,
                manifest_content_hash=manifest.content_hash,
                job_id=job_id,
                feature_lake_manifest_ref=manifest.feature_lake_manifest_ref,
                feature_lake_manifest_hash=manifest.feature_lake_manifest_hash,
                model_family=manifest.model_family,
                budget_decision=_zero_decision(job_type="baseline_training"),
                walk_forward=_empty_window(manifest.label_horizon_ns),
                status=DispatchStatus.VALIDATION_ERROR,
                artifact_id=None,
                artifact_sha256=None,
                dossier_id=None,
                dossier_authority=Authority.SHADOW_ONLY.value,
                started_at_ns=started_at_ns,
                ended_at_ns=time.time_ns(),
                error_code="validation_error",
                error_summary=str(exc),
            )

        # 2. Translate manifest -> RunPodTrainingRequest (training
        # boundary schema). Pin walk-forward in extra_constraints.
        req_dict = manifest.to_dispatch_request(job_id=job_id)
        # Surface the walk-forward split in extra_constraints too, so
        # the trainer (or future RunPod worker) can audit it.
        req_dict["extra_constraints"]["walk_forward_train_end"] = str(wfw.train_end)
        req_dict["extra_constraints"]["walk_forward_val_start"] = str(wfw.val_start)
        req_dict["extra_constraints"]["walk_forward_val_end"] = str(wfw.val_end)
        req_dict["extra_constraints"]["walk_forward_test_start"] = str(wfw.test_start)
        req_dict["extra_constraints"]["walk_forward_test_end"] = str(wfw.test_end)
        req = RunPodTrainingRequest.model_validate(req_dict)

        # 3. BudgetGuard check. Zero-cost manifests bypass the guard but
        # still get a recorded decision.
        bd = _check_budget(
            guard=self.budget_guard,
            amount_cents=manifest.budget_cents,
            job_type="baseline_training",
        )
        if not bd.allowed:
            return DispatchReceipt(
                receipt_id=receipt_id,
                manifest_id=manifest.manifest_id,
                manifest_content_hash=manifest.content_hash,
                job_id=job_id,
                feature_lake_manifest_ref=manifest.feature_lake_manifest_ref,
                feature_lake_manifest_hash=manifest.feature_lake_manifest_hash,
                model_family=manifest.model_family,
                budget_decision=bd,
                walk_forward=wfw,
                status=DispatchStatus.BUDGET_REJECTED,
                artifact_id=None,
                artifact_sha256=None,
                dossier_id=None,
                dossier_authority=Authority.SHADOW_ONLY.value,
                started_at_ns=started_at_ns,
                ended_at_ns=time.time_ns(),
                error_code="budget_rejected",
                error_summary=bd.reason,
            )

        # 4. Local trainer.
        handler = RunPodTrainingHandler(
            callback_secret=self.callback_secret,
            trainer=self.trainer,
            deadline_seconds=manifest.timeout_seconds,
            worker_id=self.worker_id,
        )
        try:
            result: TrainingResult = handler.handle(req)
        except TrainingFailure as exc:
            return DispatchReceipt(
                receipt_id=receipt_id,
                manifest_id=manifest.manifest_id,
                manifest_content_hash=manifest.content_hash,
                job_id=job_id,
                feature_lake_manifest_ref=manifest.feature_lake_manifest_ref,
                feature_lake_manifest_hash=manifest.feature_lake_manifest_hash,
                model_family=manifest.model_family,
                budget_decision=bd,
                walk_forward=wfw,
                status=DispatchStatus.TRAINER_FAILED,
                artifact_id=None,
                artifact_sha256=None,
                dossier_id=None,
                dossier_authority=Authority.SHADOW_ONLY.value,
                started_at_ns=started_at_ns,
                ended_at_ns=time.time_ns(),
                error_code=exc.error_code,
                error_summary=exc.error_summary,
            )

        ended_at_ns = time.time_ns()
        return DispatchReceipt(
            receipt_id=receipt_id,
            manifest_id=manifest.manifest_id,
            manifest_content_hash=manifest.content_hash,
            job_id=job_id,
            feature_lake_manifest_ref=manifest.feature_lake_manifest_ref,
            feature_lake_manifest_hash=manifest.feature_lake_manifest_hash,
            model_family=manifest.model_family,
            budget_decision=bd,
            walk_forward=wfw,
            status=DispatchStatus.DISPATCHED,
            artifact_id=result.artifact_id,
            artifact_sha256=_payload_hash(result.callback_payload),
            dossier_id=result.dossier_id,
            dossier_authority=Authority.SHADOW_ONLY.value,
            started_at_ns=started_at_ns,
            ended_at_ns=ended_at_ns,
            error_code=None,
            error_summary=None,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_budget(
    *,
    guard: BudgetGuard,
    amount_cents: int,
    job_type: str,
) -> BudgetDecision:
    """Consult ``BudgetGuard`` for the given cost; zero-cost bypasses the guard."""
    if amount_cents == 0:
        return BudgetDecision(
            allowed=True,
            reason="zero-cost bypass (local / staging)",
            job_type=job_type,
            amount_cents=0,
            monthly_budget_cents=guard.monthly_budget_cents,
            spent_cents=guard.get_monthly_spend(),
            remaining_cents=guard.monthly_budget_cents - guard.get_monthly_spend(),
            year_month=_current_year_month(),
        )
    return guard.check_and_reserve(amount_cents=amount_cents, job_type=job_type)


def _zero_decision(*, job_type: str) -> BudgetDecision:
    """A zero-amount BudgetDecision for the validation-error path."""
    return BudgetDecision(
        allowed=False,
        reason="validation_error (no budget consulted)",
        job_type=job_type,
        amount_cents=0,
        monthly_budget_cents=0,
        spent_cents=0,
        remaining_cents=0,
        year_month=_current_year_month(),
    )


def _empty_window(label_horizon_ns: int) -> WalkForwardWindow:
    """An empty placeholder WalkForwardWindow for failure paths."""
    return WalkForwardWindow(
        train_start=0,
        train_end=0,
        val_start=0,
        val_end=0,
        test_start=0,
        test_end=0,
        label_horizon_ns=label_horizon_ns,
    )


def _payload_hash(callback_payload: bytes) -> str | None:
    """Return the SHA-256 of the callback payload, or None on error."""
    try:
        return hashlib.sha256(callback_payload).hexdigest()
    except Exception:
        return None


def _current_year_month() -> str:
    t = time.gmtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


# ---------------------------------------------------------------------------
# Manifest builder (operator-facing convenience)
# ---------------------------------------------------------------------------


def build_training_manifest_from_feature_lake(
    *,
    feature_lake_manifest: Any,
    manifest_id: str,
    model_family: str,
    hyperparameters: dict[str, float] | None = None,
    train_window_ns: int,
    val_window_ns: int,
    test_window_ns: int,
    label_horizon_ns: int,
    random_seed: int | None = None,
    budget_cents: int = 0,
    timeout_seconds: int = 600,
    walk_forward_enabled: bool = True,
    operator_note: str = "",
) -> TrainingManifest:
    """Construct a ``TrainingManifest`` from a ``FeatureLakeManifest``.

    The operator passes the output of ``FeatureLakeBuilder.build_manifest()``
    plus a few baseline knobs. This helper does not mutate the lake
    manifest — it reads only the fields it needs and packages them into
    a schema-versioned ``TrainingManifest``.
    """
    return TrainingManifest(
        manifest_id=manifest_id,
        feature_lake_manifest_ref=feature_lake_manifest.dataset_id,
        feature_lake_manifest_hash=feature_lake_manifest.manifest_hash(),
        model_family=model_family,
        hyperparameters=dict(hyperparameters or {}),
        train_window_ns=train_window_ns,
        val_window_ns=val_window_ns,
        test_window_ns=test_window_ns,
        label_horizon_ns=label_horizon_ns,
        random_seed=random_seed,
        walk_forward_enabled=walk_forward_enabled,
        budget_cents=budget_cents,
        timeout_seconds=timeout_seconds,
        operator_note=operator_note,
    )


def write_dispatch_receipt(receipt: DispatchReceipt, output_path: pathlib.Path) -> pathlib.Path:
    """Write a dispatch receipt to a JSON file and return the path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(receipt.to_dict(), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return output_path


__all__ = [
    "DispatchReceipt",
    "DispatchStatus",
    "LocalTrainingDispatcher",
    "build_training_manifest_from_feature_lake",
    "write_dispatch_receipt",
]
