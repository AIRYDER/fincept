"""
quant_foundry.gateway — Quant Foundry gateway wiring (TASK-0306).

Wires the durability + mock loop pieces from TASK-0304/0305 into a single
facade that the FastAPI route (`api.routes.quant_foundry`) calls. The
gateway owns the outbox, inbox, mock dispatcher, callback processor, and
the shadow/dossier stubs, plus config (enabled / mode / shadow_only /
callback_secret / base_dir).

Config is read from environment variables (NOT from `fincept_core.Settings`)
to keep this module file-disjoint from the shared config file:
  QUANT_FOUNDRY_ENABLED   (default "false")
  QUANT_FOUNDRY_MODE      (default "local_mock")
  QUANT_FOUNDRY_SHADOW_ONLY (default "true")
  QUANT_FOUNDRY_CALLBACK_SECRET (default "")
  QUANT_FOUNDRY_BASE_DIR  (default "reports/quant-foundry")

Invariants:
- Disabled by default. When disabled, operator endpoints return a safe
  disabled state and NO jobs are created or processed.
- `local_mock` mode runs the full loop synchronously on `create_job`
  (enqueue -> dispatch -> process) to prove the contract end-to-end.
- Shadow-only is enforced structurally by the stubs (TASK-0305); the
  gateway never writes to `sig.predict` or any trading stream.
- The callback endpoint verifies HMAC signatures via `verify_callback`
  (TASK-0303) and records the verdict in the inbox; the processor is
  fail-closed on bad signatures.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

from quant_foundry.budget import BudgetGuard
from quant_foundry.budget import from_env as budget_from_env
from quant_foundry.callbacks import CallbackProcessor, DossierStub, ShadowLedgerStub
from quant_foundry.dossier import DossierStatus
from quant_foundry.inbox import CallbackInbox
from quant_foundry.leaderboard_expanded import ExpandedLeaderboard
from quant_foundry.mock_dispatcher import MockDispatcher
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.promotion import PromotionReviewQueue
from quant_foundry.registry import DossierRegistry
from quant_foundry.signatures import verify_callback


class QuantFoundryGateway:
    """Facade over the outbox + inbox + dispatcher + processor + stubs.

    Construct with explicit config for tests, or via `from_env()` for the
    live route (which reads env vars with spec defaults).
    """

    def __init__(
        self,
        *,
        enabled: bool,
        mode: str,
        shadow_only: bool,
        callback_secret: str,
        base_dir: pathlib.Path | str,
        budget_guard: BudgetGuard | None = None,
    ) -> None:
        self.enabled = enabled
        self.mode = mode
        self.shadow_only = shadow_only
        self.callback_secret = callback_secret
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.budget_guard = budget_guard

        self.outbox = JobOutbox(base_dir=self.base_dir / "outbox")
        self.inbox = CallbackInbox(base_dir=self.base_dir / "inbox")
        self.shadow_ledger = ShadowLedgerStub()
        self.dossier_store = DossierStub()
        self._dossier_registry: DossierRegistry | None = None
        self._expanded_leaderboard: ExpandedLeaderboard | None = None
        self._promotion_queue: PromotionReviewQueue | None = None
        self.dispatcher = MockDispatcher(
            outbox=self.outbox,
            inbox=self.inbox,
            callback_secret=callback_secret,
            base_dir=self.base_dir,
        )
        self.processor = CallbackProcessor(
            outbox=self.outbox,
            inbox=self.inbox,
            callback_secret=callback_secret,
            shadow_ledger=self.shadow_ledger,
            dossier_store=self.dossier_store,
        )

    @classmethod
    def from_env(cls, base_dir: pathlib.Path | str | None = None) -> QuantFoundryGateway:
        """Construct from env vars with spec defaults."""
        enabled = os.environ.get("QUANT_FOUNDRY_ENABLED", "false").lower() == "true"
        mode = os.environ.get("QUANT_FOUNDRY_MODE", "local_mock")
        shadow_only = os.environ.get("QUANT_FOUNDRY_SHADOW_ONLY", "true").lower() == "true"
        callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
        if base_dir is None:
            base_dir = os.environ.get("QUANT_FOUNDRY_BASE_DIR", "reports/quant-foundry")
        budget_guard = budget_from_env(pathlib.Path(base_dir) / "budget")
        return cls(
            enabled=enabled,
            mode=mode,
            shadow_only=shadow_only,
            callback_secret=callback_secret,
            base_dir=base_dir,
            budget_guard=budget_guard,
        )

    # --- health / state ---

    def health(self) -> dict[str, Any]:
        """Safe health state (no secrets)."""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "shadow_only": self.shadow_only,
            "job_count": len(self.outbox.list()) if self.enabled else 0,
        }

    def heartbeats(self) -> list[dict[str, Any]]:
        """Worker heartbeats. In local_mock mode there are no external
        workers, so this returns an empty list (the mock worker is
        implicit). The future RunPod path populates this from the inbox /
        a heartbeat stream."""
        if not self.enabled:
            return []
        return []

    # --- job lifecycle ---

    def create_job(
        self,
        *,
        job_id: str,
        job_type: str,
        idempotency_key: str,
        request_payload: Any,
        priority: int = 0,
        budget_cents: int | None = None,
    ) -> dict[str, Any]:
        """Create a job. In local_mock mode, runs the full loop synchronously.

        Budget enforcement (fail-closed): if a ``budget_guard`` is configured,
        the estimated cost (``budget_cents``) is reserved BEFORE the job is
        enqueued. A rejected reservation returns an ``ok=False`` envelope with
        an ``error_code`` and the job is never enqueued or dispatched. Zero-cost
        jobs (``budget_cents`` None/0, e.g. local mock) are always allowed.
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}
        if self.budget_guard is not None:
            decision = self.budget_guard.check_and_reserve(
                amount_cents=budget_cents or 0,
                job_type=job_type,
            )
            if not decision.allowed:
                error_code = (
                    "budget_kill_switch"
                    if "kill switch" in decision.reason
                    else "budget_exceeded"
                )
                return {
                    "enabled": True,
                    "ok": False,
                    "job_id": job_id,
                    "error_code": error_code,
                    "detail": decision.reason,
                    "budget_cents": budget_cents,
                    "remaining_cents": decision.remaining_cents,
                    "monthly_budget_cents": decision.monthly_budget_cents,
                    "mode": self.mode,
                }
        self.outbox.enqueue(
            job_id=job_id,
            job_type=job_type,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            priority=priority,
            budget_cents=budget_cents,
        )
        if self.mode == "local_mock":
            self.dispatcher.dispatch(job_id, request_payload=request_payload)
            self.processor.process(job_id)
        rec = self.outbox.get(job_id)
        return {
            "enabled": True,
            "job_id": job_id,
            "status": rec.status.value if rec is not None else None,
            "mode": self.mode,
        }

    def list_jobs(self, *, status: JobStatus | None = None) -> list[dict[str, Any]]:
        """List jobs (optionally filtered by status). Empty when disabled."""
        if not self.enabled:
            return []
        return [r.model_dump() for r in self.outbox.list(status=status)]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Return a job detail dict, or None if unknown / disabled."""
        if not self.enabled:
            return None
        rec = self.outbox.get(job_id)
        if rec is None:
            return None
        return rec.model_dump()

    # --- dossier / tournament / promotion reads ---

    def dossier_registry(self) -> DossierRegistry:
        """Return the lazily constructed real dossier registry."""
        if self._dossier_registry is None:
            self._dossier_registry = DossierRegistry(self.base_dir / "dossier_registry")
        return self._dossier_registry

    def expanded_leaderboard(self) -> ExpandedLeaderboard:
        """Return the lazily constructed expanded leaderboard."""
        if self._expanded_leaderboard is None:
            self._expanded_leaderboard = ExpandedLeaderboard()
        return self._expanded_leaderboard

    def promotion_queue(self) -> PromotionReviewQueue:
        """Return the lazily constructed promotion review queue."""
        if self._promotion_queue is None:
            self._promotion_queue = PromotionReviewQueue()
        return self._promotion_queue

    def list_dossiers(self, *, status: DossierStatus | None = None) -> list[dict[str, Any]]:
        """List persisted dossiers from the real registry. Empty when disabled."""
        if not self.enabled:
            return []
        return [
            dossier.model_dump(mode="json")
            for dossier in self.dossier_registry().list(status=status)
        ]

    def get_dossier(self, model_id: str) -> dict[str, Any] | None:
        """Return a persisted dossier dict, or None if unknown / disabled."""
        if not self.enabled:
            return None
        dossier = self.dossier_registry().get(model_id)
        if dossier is None:
            return None
        return dossier.model_dump(mode="json")

    def tournament_leaderboard(self) -> list[dict[str, Any]]:
        """Return the ranked expanded leaderboard. Empty when disabled."""
        if not self.enabled:
            return []
        return [entry.to_dict() for entry in self.expanded_leaderboard().ranked()]

    def pending_promotions(self) -> list[dict[str, Any]]:
        """Return pending promotion review queue entries. Empty when disabled."""
        if not self.enabled:
            return []
        return [entry.model_dump(mode="json") for entry in self.promotion_queue().pending()]

    def completed_promotions(self) -> list[dict[str, Any]]:
        """Return completed promotion receipts. Empty when disabled."""
        if not self.enabled:
            return []
        return [receipt.to_dict() for receipt in self.promotion_queue().completed()]

    # --- callback ingestion (HMAC auth, NOT bearer) ---

    def receive_callback(
        self,
        *,
        job_id: str,
        payload: bytes,
        signature: str,
        ts: int,
        worker_id: str = "external",
    ) -> dict[str, Any]:
        """Receive an external callback. Verifies HMAC signature FIRST,
        then records in the inbox and processes. Fail-closed on bad
        signature or payload hash mismatch (security event).

        Returns a receipt dict. The caller (route) maps non-OK results to
        HTTP error codes.
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}

        # The job must exist in the outbox (callback for unknown job = reject).
        ob_rec = self.outbox.get(job_id)
        if ob_rec is None:
            return {"enabled": True, "ok": False, "error_code": "unknown_job",
                    "detail": f"no outbox record for job_id {job_id}"}

        # HMAC verify FIRST (constant-time, skew-checked) via TASK-0303.
        # We verify before touching the inbox so a bad signature never
        # creates a durable record (fail-closed, no side effect).
        signature_valid = verify_callback(
            payload, signature,
            secret=self.callback_secret, ts=ts, job_id=job_id,
        )

        if not signature_valid:
            # Bad signature: reject immediately without recording in the
            # inbox. No domain effect, no durable trace of the bad payload.
            return {
                "enabled": True,
                "ok": False,
                "error_code": "bad_signature",
                "detail": "callback signature verification failed",
            }

        # Valid signature: record the callback in the inbox (durable,
        # idempotent on hash). The inbox's diff-hash guard is a security
        # feature — if the same job_id already has a callback with a
        # DIFFERENT payload hash, that's a tamper/replay event. We catch
        # it and return a clean security error (no crash).
        try:
            self.inbox.receive(
                job_id=job_id,
                idempotency_key=ob_rec.idempotency_key,
                signature_valid=signature_valid,
                payload=payload,
                worker_id=worker_id,
            )
        except ValueError as exc:
            return {
                "enabled": True,
                "ok": False,
                "error_code": "payload_hash_mismatch",
                "detail": str(exc),
            }

        # Process the callback (idempotent + fail-closed on schema/etc).
        proc_receipt = self.processor.process(job_id)
        return {
            "enabled": True,
            "ok": True,
            "job_id": job_id,
            "inbox_status": proc_receipt["inbox_status"],
            "outbox_status": proc_receipt["outbox_status"],
            "result": proc_receipt["result"],
        }
