"""Auto shadow dispatcher (Tier 2f).

Automatically dispatches shadow inference jobs for model versions
that are ``research_approved`` (the status before
``shadow_approved``) but have no shadow predictions yet in the
:class:`ShadowLedger`.

This closes Gap 5 from the Tier 2a audit: shadow dispatch was not
scheduled or automated. With this module, the fully automated
product loop is:

  dispatch training → callback → auto-tournament → auto-promotion
  → auto shadow dispatch → settlement → auto-tournament (next cycle)

The dispatcher uses the DB-backed :class:`ModelRegistryDB` to find
eligible versions (not the file-based DossierRegistry that the
existing ``dispatch_shadow_inference_batch()`` uses), and checks
the :class:`ShadowLedger` to avoid re-dispatching models that
already have shadow predictions.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fincept_db.registry_tables import ModelVersionRow
from quant_foundry.dossier import DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.shadow_ledger import ShadowLedger

__all__ = [
    "AutoShadowDispatcher",
    "AutoShadowReceipt",
    "ShadowDispatchResult",
]


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


class ShadowDispatchResult(BaseModel):
    """The result of dispatching shadow inference for one version.

    Frozen + extra='forbid' (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version_id: str
    model_id: str
    artifact_id: str
    job_id: str
    dispatched: bool
    skipped: bool = False
    error: str | None = None


class AutoShadowReceipt(BaseModel):
    """Receipt for one auto-shadow-dispatch run.

    Frozen + extra='forbid'.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_at_ns: int
    total: int = 0
    dispatched: int = 0
    skipped: int = 0
    errored: int = 0
    results: list[ShadowDispatchResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AutoShadowDispatcher
# ---------------------------------------------------------------------------


class AutoShadowDispatcher:
    """Auto-dispatch shadow inference for research_approved versions.

    Scans the model registry for versions with status
    ``research_approved`` that have no shadow predictions in the
    :class:`ShadowLedger`, and dispatches shadow inference jobs via
    the gateway.

    Usage::

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()
        print(f"Dispatched {receipt.dispatched}/{receipt.total}")

    Args:
        gateway: the Quant Foundry gateway (for create_job).
        registry: the model registry DB (for finding eligible versions).
        shadow_ledger: the shadow prediction ledger (for checking
            existing predictions).
        target_status: the dossier status that makes a version
            eligible for shadow dispatch. Defaults to
            ``RESEARCH_APPROVED`` (the status before
            ``SHADOW_APPROVED``).
        horizon_ns: the prediction horizon for shadow inference
            (default 1 day).
    """

    def __init__(
        self,
        gateway: QuantFoundryGateway,
        registry: ModelRegistryDB,
        shadow_ledger: ShadowLedger,
        *,
        target_status: DossierStatus = DossierStatus.RESEARCH_APPROVED,
        horizon_ns: int = 86_400_000_000_000,
    ) -> None:
        self.gateway = gateway
        self.registry = registry
        self.shadow_ledger = shadow_ledger
        self.target_status = target_status
        self.horizon_ns = horizon_ns

    def run(self) -> AutoShadowReceipt:
        """Run one auto-shadow-dispatch pass.

        Returns a frozen :class:`AutoShadowReceipt`.
        """
        now_ns = time.time_ns()
        results: list[ShadowDispatchResult] = []
        dispatched = 0
        skipped = 0
        errored = 0

        eligible = self._find_eligible_versions()
        for version in eligible:
            try:
                result = self._dispatch_for_version(version)
                results.append(result)
                if result.dispatched:
                    dispatched += 1
                elif result.skipped:
                    skipped += 1
            except Exception as exc:
                results.append(
                    ShadowDispatchResult(
                        version_id=version["version_id"],
                        model_id=version["model_id"],
                        artifact_id=version.get("artifact_id", ""),
                        job_id="",
                        dispatched=False,
                        skipped=True,
                        error=str(exc),
                    )
                )
                errored += 1

        return AutoShadowReceipt(
            run_at_ns=now_ns,
            total=len(results),
            dispatched=dispatched,
            skipped=skipped,
            errored=errored,
            results=results,
        )

    def _find_eligible_versions(self) -> list[dict[str, Any]]:
        """Find versions eligible for shadow dispatch.

        A version is eligible if:
          1. Its status is ``target_status`` (default research_approved).
          2. It has no shadow predictions in the ShadowLedger for its
             model_id.
        """
        target_status_val = (
            self.target_status.value
            if hasattr(self.target_status, "value")
            else str(self.target_status)
        )

        engine = self.registry.engine
        eligible: list[dict[str, Any]] = []

        with Session(engine) as session:
            # Find all versions with the target status.
            version_rows = session.scalars(
                select(ModelVersionRow).where(ModelVersionRow.status == target_status_val)
            ).all()

            for row in version_rows:
                model_id = row.model_id
                # Check if shadow predictions already exist.
                existing = self.shadow_ledger.read_by_model(model_id)
                if existing:
                    continue  # already has shadow predictions
                eligible.append(
                    {
                        "version_id": row.version_id,
                        "model_id": model_id,
                        "artifact_id": row.artifact_id,
                        "status": row.status,
                        "version_number": row.version_number,
                    }
                )

        return eligible

    def _dispatch_for_version(self, version: dict[str, Any]) -> ShadowDispatchResult:
        """Dispatch shadow inference for a single version.

        Builds a payload similar to
        ``gateway.dispatch_shadow_inference_batch()`` and calls
        ``gateway.create_job()``.
        """
        model_id = version["model_id"]
        artifact_id = version.get("artifact_id", model_id)
        version_id = version["version_id"]

        # Build the shadow snapshot payload.
        snapshot_payload = self.gateway._build_shadow_snapshot_payload(model_id)
        job_id = f"shadow-inference-{model_id}-{uuid.uuid4().hex[:12]}"
        idempotency_key = f"shadow-dispatch-{model_id}-{time.time_ns()}"

        request_payload: dict[str, Any] = {
            "job_id": job_id,
            "artifact_ref": str(artifact_id),
            "symbols": [],
            "horizons_ns": [self.horizon_ns],
            "snapshot": snapshot_payload,
            "model_id": model_id,
        }

        receipt = self.gateway.create_job(
            job_id=job_id,
            job_type="inference",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )

        if receipt.get("ok") is False:
            return ShadowDispatchResult(
                version_id=version_id,
                model_id=model_id,
                artifact_id=artifact_id,
                job_id=job_id,
                dispatched=False,
                skipped=True,
                error=f"create_job failed: {receipt.get('error_code', 'unknown')}: {receipt.get('detail', '')}",
            )

        if receipt.get("status") == "failed":
            return ShadowDispatchResult(
                version_id=version_id,
                model_id=model_id,
                artifact_id=artifact_id,
                job_id=job_id,
                dispatched=False,
                skipped=True,
                error=f"job status is failed: {receipt.get('detail', '')}",
            )

        return ShadowDispatchResult(
            version_id=version_id,
            model_id=model_id,
            artifact_id=artifact_id,
            job_id=job_id,
            dispatched=True,
        )
