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
from quant_foundry.runpod_client import (
    BudgetGuard as DispatchBudgetGuard,
    HttpRunPodClient,
    RunPodDispatcher,
)
from quant_foundry.shadow_ledger import ShadowLedger
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
        runpod_client: Any = None,
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
        self._shadow_ledger_real: ShadowLedger | None = None
        self._runpod_client = runpod_client
        self._runpod_dispatcher: RunPodDispatcher | None = None
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

        # When mode == "runpod", wire the RunPodDispatcher with the
        # HttpRunPodClient (or an injected client for tests). The mock
        # dispatcher remains available for local_mock fallback.
        if self.mode == "runpod" and runpod_client is not None:
            dispatch_budget = DispatchBudgetGuard(
                monthly_budget_cents=(
                    budget_guard.monthly_budget_cents
                    if budget_guard is not None
                    else 0
                ),
            )
            self._runpod_dispatcher = RunPodDispatcher(
                outbox=self.outbox,
                client=runpod_client,
                mode="runpod",
                budget_guard=dispatch_budget,
            )

    @classmethod
    def from_env(cls, base_dir: pathlib.Path | str | None = None) -> QuantFoundryGateway:
        """Construct from env vars with spec defaults.

        When ``QUANT_FOUNDRY_MODE=runpod``, reads the following additional
        env vars to construct the HttpRunPodClient:

        - ``RUNPOD_API_KEY`` (required in runpod mode) — RunPod API key.
        - ``RUNPOD_ENDPOINT_ID`` (required in runpod mode) — serverless
          endpoint ID for the training or inference worker.
        - ``RUNPOD_BASE_URL`` (optional, default
          ``https://api.runpod.ai/v2``) — RunPod API base URL.
        - ``RUNPOD_TIMEOUT_SECONDS`` (optional, default ``30``) — HTTP
          request timeout for dispatch calls.
        - ``RUNPOD_COST_PER_DISPATCH_CENTS`` (optional, default ``0``) —
          estimated cost per dispatch for budget guard checks.
        """
        enabled = os.environ.get("QUANT_FOUNDRY_ENABLED", "false").lower() == "true"
        mode = os.environ.get("QUANT_FOUNDRY_MODE", "local_mock")
        shadow_only = os.environ.get("QUANT_FOUNDRY_SHADOW_ONLY", "true").lower() == "true"
        callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
        if base_dir is None:
            base_dir = os.environ.get("QUANT_FOUNDRY_BASE_DIR", "reports/quant-foundry")
        budget_guard = budget_from_env(pathlib.Path(base_dir) / "budget")

        runpod_client: HttpRunPodClient | None = None
        if mode == "runpod":
            api_key = os.environ.get("RUNPOD_API_KEY", "")
            endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")
            base_url = os.environ.get("RUNPOD_BASE_URL", "https://api.runpod.ai/v2")
            timeout_str = os.environ.get("RUNPOD_TIMEOUT_SECONDS", "30")
            cost_str = os.environ.get("RUNPOD_COST_PER_DISPATCH_CENTS", "0")
            try:
                timeout_s = float(timeout_str)
            except ValueError:
                timeout_s = 30.0
            try:
                cost_cents = int(cost_str)
            except ValueError:
                cost_cents = 0
            runpod_client = HttpRunPodClient(
                api_key=api_key,
                endpoint_id=endpoint_id,
                base_url=base_url,
                timeout_seconds=timeout_s,
                cost_per_dispatch_cents=cost_cents,
            )

        return cls(
            enabled=enabled,
            mode=mode,
            shadow_only=shadow_only,
            callback_secret=callback_secret,
            base_dir=base_dir,
            budget_guard=budget_guard,
            runpod_client=runpod_client,
        )

    # --- health / state ---

    def health(self) -> dict[str, Any]:
        """Safe health state (no secrets)."""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "shadow_only": self.shadow_only,
            "job_count": len(self.outbox.list()) if self.enabled else 0,
            "runpod_wired": self._runpod_dispatcher is not None,
        }

    def runpod_health(self) -> dict[str, Any]:
        """Check RunPod endpoint health (only in runpod mode).

        Returns a dict with ``ok``, ``status``, and ``detail``. Never
        raises — network errors are caught and reported as ``ok=False``.
        When not in runpod mode or no client is wired, returns
        ``{"ok": False, "status": "not_runpod_mode"}``.
        """
        if self.mode != "runpod" or self._runpod_client is None:
            return {"ok": False, "status": "not_runpod_mode"}
        try:
            result = self._runpod_client.check_health()  # type: ignore[union-attr]
            return {"ok": True, "status": "healthy", "detail": result}
        except Exception as exc:
            return {
                "ok": False,
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
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
        elif self.mode == "runpod" and self._runpod_dispatcher is not None:
            # Dispatch to RunPod via the HTTP client. The callback will
            # arrive asynchronously at POST /quant-foundry/callbacks/runpod.
            self._runpod_dispatcher.dispatch(
                job_id, request_payload=request_payload,
            )
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

    # --- shadow inference health (TASK-0604) ---------------------------------

    def shadow_ledger_real(self) -> ShadowLedger:
        """Return the lazily constructed real shadow prediction ledger.

        Distinct from ``self.shadow_ledger`` (an in-process ``ShadowLedgerStub``
        wired into the callback processor for local_mock mode). This is the
        durable ``ShadowLedger`` (JSONL-backed) consumed by ``shadow_health``.
        """
        if self._shadow_ledger_real is None:
            self._shadow_ledger_real = ShadowLedger(
                base_dir=self.base_dir / "shadow_ledger",
            )
        return self._shadow_ledger_real

    def shadow_health(self) -> dict[str, Any]:
        """Aggregate read-only health for the shadow inference surface.

        Returns a JSON-safe dict with documented keys; nulls for fields that
        cannot be computed from durable state. Never includes secrets or raw
        callback payloads.

        When the gateway is disabled, returns ``{"enabled": False, ...}``
        with zero counts and null metrics (no crash, no 500).
        """
        if not self.enabled:
            return {
                "enabled": False,
                "models_running": 0,
                "latest_prediction_ts": None,
                "latency_p50_ms": None,
                "latency_p95_ms": None,
                "feature_availability": None,
                "callback_rejection_rate": None,
                "settlement_lag_seconds": None,
                "circuit_breaker_state": "closed",
                "prediction_count": 0,
                "settled_count": 0,
            }

        records = self.shadow_ledger_real().list()
        prediction_count = len(records)
        models_running = len({r.model_id for r in records})

        latencies = sorted(
            float(r.latency_ms) for r in records if r.latency_ms is not None
        )
        latency_p50_ms: float | None = _percentile(latencies, 0.5) if latencies else None
        latency_p95_ms: float | None = _percentile(latencies, 0.95) if latencies else None

        latest_prediction_ts: float | None = (
            float(max(r.ts_event for r in records)) if records else None
        )

        feature_availability: float | None = _aggregate_feature_availability(records)

        # The gateway rejects bad HMAC signatures without a durable inbox
        # record (see ``receive_callback``), so we cannot compute a real
        # rejection rate from existing state. Return ``None`` with the
        # documented note that rejection tracking is not yet durable —
        # do NOT invent new storage here (out of scope).
        callback_rejection_rate: float | None = None

        # The settlement ledger is wired by TASK-0603 (separate surface) and
        # is not yet integrated into the gateway's read API. Return ``None``
        # rather than fabricating a value.
        settlement_lag_seconds: float | None = None

        # No real drift data is collected yet. The drift sentinel is consumed
        # read-only, so the circuit breaker defaults to "closed" (no drift =
        # no trip). Wire real inputs when the drift surface ships.
        circuit_breaker_state = "closed"

        return {
            "enabled": True,
            "models_running": models_running,
            "latest_prediction_ts": latest_prediction_ts,
            "latency_p50_ms": latency_p50_ms,
            "latency_p95_ms": latency_p95_ms,
            "feature_availability": feature_availability,
            "callback_rejection_rate": callback_rejection_rate,
            "settlement_lag_seconds": settlement_lag_seconds,
            "circuit_breaker_state": circuit_breaker_state,
            "prediction_count": prediction_count,
            "settled_count": 0,
        }

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


# ---------------------------------------------------------------------------
# Internal helpers (TASK-0604 shadow health aggregation)
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an already-sorted numeric list."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if pct <= 0:
        return sorted_values[0]
    if pct >= 1:
        return sorted_values[-1]
    position = pct * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _aggregate_feature_availability(records: list[Any]) -> float | None:
    """Fraction of features marked available across all stored predictions.

    Returns ``None`` when no record carries a ``feature_availability`` map —
    preserves the spec's "null for uncomputable" contract.
    """
    available = 0
    total = 0
    for r in records:
        fa = getattr(r, "feature_availability", None)
        if not fa:
            continue
        for present in fa.values():
            total += 1
            if present:
                available += 1
    if total == 0:
        return None
    return available / total
