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
from collections.abc import Mapping
from typing import Any

from quant_foundry.budget import BudgetGuard
from quant_foundry.budget import from_env as budget_from_env
from quant_foundry.callbacks import (
    CallbackProcessor,
    DurableDossierStore,
    DurableShadowLedgerStore,
)
from quant_foundry.dossier import DossierStatus
from quant_foundry.feature_lake import FeatureRow, FeatureValue
from quant_foundry.feature_snapshot_export import export_feature_snapshot
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
from quant_foundry.schemas import RunPodInferenceRequest
from quant_foundry.shadow_ledger import ShadowLedger
from quant_foundry.signatures import sign_callback, verify_callback


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
        runpod_clients: Mapping[str, Any] | None = None,
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
        self._dossier_registry: DossierRegistry | None = None
        self._expanded_leaderboard: ExpandedLeaderboard | None = None
        self._promotion_queue: PromotionReviewQueue | None = None
        self._shadow_ledger_real: ShadowLedger | None = None
        self.shadow_ledger = DurableShadowLedgerStore(self.shadow_ledger_real())
        self.dossier_store = DurableDossierStore(self.dossier_registry())
        self._runpod_client = runpod_client
        self._runpod_dispatcher: RunPodDispatcher | None = None
        self._runpod_clients: dict[str, Any] = {}
        self._runpod_dispatchers: dict[str, RunPodDispatcher] = {}
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

        if runpod_clients is not None:
            self._runpod_clients = {
                _normalize_job_type(job_type): client
                for job_type, client in runpod_clients.items()
            }
        elif runpod_client is not None:
            self._runpod_clients = {
                "training": runpod_client,
                "inference": runpod_client,
            }

        if self._runpod_clients:
            self._runpod_client = next(iter(self._runpod_clients.values()))

        if self._is_runpod_mode():
            dispatch_budget = DispatchBudgetGuard(
                monthly_budget_cents=(
                    budget_guard.monthly_budget_cents
                    if budget_guard is not None
                    else 0
                ),
            )
            for job_type, client in self._runpod_clients.items():
                dispatcher = RunPodDispatcher(
                    outbox=self.outbox,
                    client=client,
                    mode="runpod",
                    budget_guard=dispatch_budget,
                    endpoint_id=_client_endpoint_id(client),
                )
                self._runpod_dispatchers[job_type] = dispatcher
            if self._runpod_dispatchers:
                self._runpod_dispatcher = next(iter(self._runpod_dispatchers.values()))

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

        runpod_clients: dict[str, HttpRunPodClient] = {}
        if _is_runpod_mode_value(mode):
            api_key = os.environ.get("RUNPOD_API_KEY", "")
            legacy_endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")
            training_endpoint_id = (
                os.environ.get("RUNPOD_TRAINING_ENDPOINT_ID", "")
                or legacy_endpoint_id
            )
            inference_endpoint_id = (
                os.environ.get("RUNPOD_INFERENCE_ENDPOINT_ID", "")
                or legacy_endpoint_id
            )
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
            if training_endpoint_id:
                runpod_clients["training"] = HttpRunPodClient(
                    api_key=api_key,
                    endpoint_id=training_endpoint_id,
                    base_url=base_url,
                    timeout_seconds=timeout_s,
                    cost_per_dispatch_cents=cost_cents,
                )
            if inference_endpoint_id:
                runpod_clients["inference"] = HttpRunPodClient(
                    api_key=api_key,
                    endpoint_id=inference_endpoint_id,
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
            runpod_clients=runpod_clients,
        )

    # --- health / state ---

    def health(self) -> dict[str, Any]:
        """Safe health state (no secrets)."""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "shadow_only": self.shadow_only,
            "job_count": len(self.outbox.list()) if self.enabled else 0,
            "runpod_wired": bool(self._runpod_dispatchers),
            "runpod_routes": {
                job_type: _client_endpoint_id(client)
                for job_type, client in self._runpod_clients.items()
            },
        }

    def runpod_health(self) -> dict[str, Any]:
        """Check RunPod endpoint health (only in runpod mode).

        Returns a dict with ``ok``, ``status``, and ``detail``. Never
        raises — network errors are caught and reported as ``ok=False``.
        When not in runpod mode or no client is wired, returns
        ``{"ok": False, "status": "not_runpod_mode"}``.
        """
        if not self._is_runpod_mode() or not self._runpod_clients:
            return {"ok": False, "status": "not_runpod_mode"}
        details: dict[str, Any] = {}
        ok = True
        for job_type, client in self._runpod_clients.items():
            try:
                details[job_type] = client.check_health()
            except Exception as exc:
                ok = False
                details[job_type] = f"{type(exc).__name__}: {exc}"
        return {
            "ok": ok,
            "status": "healthy" if ok else "error",
            "detail": details,
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

        try:
            dispatch_payload = self._prepare_dispatch_payload(
                job_type=job_type,
                request_payload=request_payload,
            )
        except (TypeError, ValueError) as exc:
            return {
                "enabled": True,
                "ok": False,
                "job_id": job_id,
                "error_code": "invalid_request_payload",
                "detail": str(exc),
                "mode": self.mode,
            }

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
            request_payload=dispatch_payload,
            priority=priority,
            budget_cents=budget_cents,
        )
        if self.mode == "local_mock":
            self.dispatcher.dispatch(job_id, request_payload=request_payload)
            self.processor.process(job_id)
        elif self._is_runpod_mode():
            dispatcher = self._dispatcher_for_job_type(job_type)
            if dispatcher is None:
                self.outbox.update_status(
                    job_id,
                    JobStatus.FAILED,
                    error_code="runpod_endpoint_not_configured",
                    error_summary=f"no RunPod endpoint configured for job_type={job_type}",
                )
            else:
                dispatcher.dispatch(job_id, request_payload=dispatch_payload)
        rec = self.outbox.get(job_id)
        return {
            "enabled": True,
            "job_id": job_id,
            "status": rec.status.value if rec is not None else None,
            "mode": self.mode,
        }

    def poll_runpod_results(self) -> list[dict[str, Any]]:
        if not self.enabled or not self._is_runpod_mode():
            return []

        receipts: list[dict[str, Any]] = []
        running = self.outbox.list(status=JobStatus.RUNNING)
        for rec in running:
            if rec.runpod_job_id is None:
                continue
            client = self._runpod_clients.get(_normalize_job_type(rec.job_type))
            if client is None:
                receipts.append({
                    "job_id": rec.job_id,
                    "ok": False,
                    "error_code": "runpod_endpoint_not_configured",
                })
                continue
            try:
                status = client.check_status(rec.runpod_job_id)
            except Exception as exc:
                receipts.append({
                    "job_id": rec.job_id,
                    "ok": False,
                    "error_code": "runpod_status_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                })
                continue

            status_value = _runpod_status_value(status)
            if status_value in {"IN_PROGRESS", "IN_QUEUE", "RUNNING", "PENDING"}:
                receipts.append({
                    "job_id": rec.job_id,
                    "ok": True,
                    "status": status_value.lower(),
                    "result": "still_running",
                })
                continue

            if status_value in {"FAILED", "CANCELLED", "CANCELED", "TIMED_OUT", "ERROR"}:
                self.outbox.update_status(
                    rec.job_id,
                    JobStatus.FAILED,
                    error_code="runpod_job_failed",
                    error_summary=str(status.get("error") or status.get("message") or status_value),
                )
                receipts.append({
                    "job_id": rec.job_id,
                    "ok": False,
                    "status": status_value.lower(),
                    "result": "failed",
                })
                continue

            if status_value not in {"COMPLETED", "SUCCEEDED", "SUCCESS"}:
                receipts.append({
                    "job_id": rec.job_id,
                    "ok": False,
                    "status": status_value.lower(),
                    "error_code": "unknown_runpod_status",
                })
                continue

            output = status.get("output")
            callback_fields = _extract_callback_fields(output if output is not None else status)
            if callback_fields is None:
                # Backward-compat: old handler returns unsigned "callback" dict.
                # Sign on the trusted Fincept side (HTTPS to RunPod API is authenticated).
                callback_fields = _compat_sign_callback(
                    output if output is not None else status,
                    secret=self.callback_secret,
                    job_id=rec.job_id,
                )
            if callback_fields is None:
                self.outbox.update_status(
                    rec.job_id,
                    JobStatus.FAILED,
                    error_code="missing_runpod_callback_fields",
                    error_summary="RunPod completed without callback_payload/signature/ts output",
                )
                receipts.append({
                    "job_id": rec.job_id,
                    "ok": False,
                    "error_code": "missing_runpod_callback_fields",
                })
                continue

            payload_text, signature, callback_ts = callback_fields
            receipt = self.receive_callback(
                job_id=rec.job_id,
                payload=payload_text.encode("utf-8"),
                signature=signature,
                ts=callback_ts,
                worker_id="runpod-poller",
            )
            receipt["runpod_job_id"] = rec.runpod_job_id
            receipts.append(receipt)
        return receipts

    def _prepare_dispatch_payload(
        self,
        *,
        job_type: str,
        request_payload: Any,
    ) -> Any:
        if not self._is_runpod_mode() or _normalize_job_type(job_type) != "inference":
            return request_payload
        if not isinstance(request_payload, dict):
            raise TypeError("RunPod inference payload must be a JSON object")
        if "request" in request_payload and "snapshot" in request_payload:
            return dict(request_payload)

        snapshot_payload = request_payload.get("snapshot")
        if snapshot_payload is None:
            rows_payload = request_payload.get("feature_rows")
            if rows_payload is None:
                raise ValueError(
                    "RunPod inference payload requires either snapshot or feature_rows"
                )
            decision_time = _decision_time_from_payload(request_payload, rows_payload)
            expected_features_payload = request_payload.get("expected_features")
            expected_features = None
            if expected_features_payload is not None:
                expected_features = tuple(str(name) for name in expected_features_payload)
            rows = tuple(_feature_row_from_payload(row) for row in rows_payload)
            snapshot_payload = export_feature_snapshot(
                rows=rows,
                decision_time=decision_time,
                expected_features=expected_features,
            ).model_dump(mode="json")

        request_data = {
            field: request_payload[field]
            for field in RunPodInferenceRequest.model_fields
            if field in request_payload
        }
        request = RunPodInferenceRequest.model_validate(request_data)
        return {
            "request": request.model_dump(mode="json"),
            "snapshot": snapshot_payload,
            "model_id": str(request_payload.get("model_id") or request.artifact_ref),
        }

    def _is_runpod_mode(self) -> bool:
        return _is_runpod_mode_value(self.mode)

    def _dispatcher_for_job_type(self, job_type: str) -> RunPodDispatcher | None:
        return self._runpod_dispatchers.get(_normalize_job_type(job_type))

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
                payload_ref=self._write_callback_payload(job_id, payload),
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

    def _write_callback_payload(self, job_id: str, payload: bytes) -> str:
        payload_dir = self.base_dir / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = job_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        payload_path = payload_dir / f"{safe_name}.json"
        payload_path.write_bytes(payload)
        return str(payload_path)


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


def _is_runpod_mode_value(mode: str) -> bool:
    return mode in {"runpod", "runpod_research", "runpod_shadow"}


def _normalize_job_type(job_type: str) -> str:
    return str(job_type).lower()


def _client_endpoint_id(client: Any) -> str | None:
    endpoint_id = getattr(client, "endpoint_id", None)
    if endpoint_id is None:
        endpoint_id = getattr(client, "_endpoint_id", None)
    if endpoint_id is None:
        return None
    return str(endpoint_id)


def _runpod_status_value(status: dict[str, Any]) -> str:
    value = status.get("status") or status.get("state") or status.get("runtimeStatus")
    if value is None:
        return "UNKNOWN"
    return str(value).upper()


def _extract_callback_fields(output: Any) -> tuple[str, str, int] | None:
    if not isinstance(output, dict):
        return None
    nested_output = output.get("output")
    if isinstance(nested_output, dict):
        nested_fields = _extract_callback_fields(nested_output)
        if nested_fields is not None:
            return nested_fields

    payload = output.get("callback_payload")
    signature = output.get("callback_signature")
    ts = output.get("callback_ts")
    if not isinstance(payload, str) or not isinstance(signature, str):
        return None
    try:
        callback_ts = int(ts)
    except (TypeError, ValueError):
        return None
    return payload, signature, callback_ts


def _compat_sign_callback(
    output: Any,
    *,
    secret: str,
    job_id: str,
) -> tuple[str, str, int] | None:
    """Backward-compat: old handler returns unsigned 'callback' dict.

    Signs the callback on the trusted Fincept side. Valid because the
    transport is authenticated HTTPS to the RunPod API. Once deployed
    handlers return signed callback_payload/signature/ts, this path
    is never hit.
    """
    if not isinstance(output, dict):
        return None
    callback_dict = output.get("callback")
    if not isinstance(callback_dict, dict):
        return None
    import json as _json
    import time as _time
    payload_text = _json.dumps(callback_dict, separators=(",", ":"), sort_keys=True)
    callback_ts = int(_time.time())
    signature = sign_callback(
        payload_text.encode("utf-8"),
        secret=secret,
        ts=callback_ts,
        job_id=job_id,
    )
    return payload_text, signature, callback_ts


def _decision_time_from_payload(
    request_payload: dict[str, Any],
    rows_payload: Any,
) -> int:
    raw_decision_time = request_payload.get("decision_time")
    if raw_decision_time is not None:
        return int(raw_decision_time)
    if not isinstance(rows_payload, (list, tuple)) or not rows_payload:
        raise ValueError("feature_rows must be a non-empty list when decision_time is omitted")
    first_row = rows_payload[0]
    if isinstance(first_row, FeatureRow):
        return int(first_row.decision_time)
    if isinstance(first_row, dict) and "decision_time" in first_row:
        return int(first_row["decision_time"])
    raise ValueError("decision_time is required when feature_rows lack decision_time")


def _feature_row_from_payload(row: Any) -> FeatureRow:
    if isinstance(row, FeatureRow):
        return row
    if not isinstance(row, dict):
        raise TypeError("feature_rows entries must be objects")
    features_payload = row.get("features")
    if not isinstance(features_payload, (list, tuple)):
        raise TypeError("feature_rows[].features must be a list")
    features = tuple(
        feature if isinstance(feature, FeatureValue) else FeatureValue(**feature)
        for feature in features_payload
    )
    return FeatureRow(
        symbol=str(row["symbol"]),
        event_ts=int(row["event_ts"]),
        decision_time=int(row["decision_time"]),
        features=features,
        label_horizon_ns=int(row.get("label_horizon_ns", 86_400_000_000_000)),
    )
