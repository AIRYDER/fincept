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

import contextlib
import dataclasses
import os
import pathlib
from collections.abc import Mapping
from typing import Any, cast

from quant_foundry.budget import BudgetGuard
from quant_foundry.budget import from_env as budget_from_env
from quant_foundry.callback_metrics import CallbackMetricsStore
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
from quant_foundry.market_data_adapter import BarDataAdapter, alpaca_reader_from_env
from quant_foundry.mock_dispatcher import MockDispatcher
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.promotion import PromotionReviewQueue
from quant_foundry.registry import DossierRegistry
from quant_foundry.runpod_client import (
    BudgetGuard as DispatchBudgetGuard,
)
from quant_foundry.runpod_client import (
    HttpRunPodClient,
    RunPodDispatcher,
)
from quant_foundry.schemas import RunPodInferenceRequest
from quant_foundry.settlement import SettlementLedger
from quant_foundry.settlement_sweep import SettlementSweep, default_cost_model
from quant_foundry.shadow_ledger import ShadowLedger
from quant_foundry.signatures import sign_callback, verify_callback  # noqa: F401
from quant_foundry.tournament import Tournament
from quant_foundry.tournament_sweep import TournamentSweep

# `sign_callback` is imported (not used at runtime) so the callback-security
# test can monkey-patch this module's `sign_callback` attribute and assert the
# poller never signs on the Fincept side — only `verify_callback` is allowed.


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
        self._promotion_gate: Any = None
        self._alpha_genome_lab: Any = None
        self._alpha_sweep_receipts: dict[str, dict[str, Any]] = {}
        self._shadow_ledger_real: ShadowLedger | None = None
        self._callback_metrics_store: CallbackMetricsStore | None = None
        self._tournament_sweep: TournamentSweep | None = None
        self._settlement_ledger: SettlementLedger | None = None
        self._tournament: Tournament | None = None
        # --- Settlement wiring (Agent A) ---
        self._settlement_sweep: Any = None
        # --- Shadow dispatch loop (Agent C) ---
        self._shadow_dispatch_count: int = 0
        self._last_shadow_dispatch_ns: int = 0
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
                _normalize_job_type(job_type): client for job_type, client in runpod_clients.items()
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
                    budget_guard.monthly_budget_cents if budget_guard is not None else 0
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
                os.environ.get("RUNPOD_TRAINING_ENDPOINT_ID", "") or legacy_endpoint_id
            )
            inference_endpoint_id = (
                os.environ.get("RUNPOD_INFERENCE_ENDPOINT_ID", "") or legacy_endpoint_id
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
                    "budget_kill_switch" if "kill switch" in decision.reason else "budget_exceeded"
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
                receipts.append(
                    {
                        "job_id": rec.job_id,
                        "ok": False,
                        "error_code": "runpod_endpoint_not_configured",
                    }
                )
                continue
            try:
                status = client.check_status(rec.runpod_job_id)
            except Exception as exc:
                receipts.append(
                    {
                        "job_id": rec.job_id,
                        "ok": False,
                        "error_code": "runpod_status_error",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            status_value = _runpod_status_value(status)
            if status_value in {"IN_PROGRESS", "IN_QUEUE", "RUNNING", "PENDING"}:
                receipts.append(
                    {
                        "job_id": rec.job_id,
                        "ok": True,
                        "status": status_value.lower(),
                        "result": "still_running",
                    }
                )
                continue

            if status_value in {"FAILED", "CANCELLED", "CANCELED", "TIMED_OUT", "ERROR"}:
                self.outbox.update_status(
                    rec.job_id,
                    JobStatus.FAILED,
                    error_code="runpod_job_failed",
                    error_summary=str(status.get("error") or status.get("message") or status_value),
                )
                receipts.append(
                    {
                        "job_id": rec.job_id,
                        "ok": False,
                        "status": status_value.lower(),
                        "result": "failed",
                    }
                )
                continue

            if status_value not in {"COMPLETED", "SUCCEEDED", "SUCCESS"}:
                receipts.append(
                    {
                        "job_id": rec.job_id,
                        "ok": False,
                        "status": status_value.lower(),
                        "error_code": "unknown_runpod_status",
                    }
                )
                continue

            output = status.get("output")
            callback_fields = _extract_callback_fields(output if output is not None else status)
            if callback_fields is None:
                # Fail-closed: RunPod handlers MUST return signed
                # callback_payload/callback_signature/callback_ts fields. The
                # Fincept side only verifies (verify_callback) — it never signs
                # on behalf of an unsigned handler (legacy compat shim removed).
                # Metrics are observability, not security — a disk error
                # must not mask the fail-closed verdict below.
                with contextlib.suppress(OSError):
                    self.callback_metrics_store().record(
                        "rejected",
                        reason_code="missing_runpod_callback_fields",
                    )
                self.outbox.update_status(
                    rec.job_id,
                    JobStatus.FAILED,
                    error_code="missing_runpod_callback_fields",
                    error_summary="RunPod completed without callback_payload/signature/ts output",
                )
                receipts.append(
                    {
                        "job_id": rec.job_id,
                        "ok": False,
                        "error_code": "missing_runpod_callback_fields",
                    }
                )
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
        """Return the lazily constructed promotion review queue.

        The queue's gate is configured with the
        ``QUANT_FOUNDRY_PROMOTION_MIN_SETTLED`` env var (default: 10).
        See ``promotion_gate`` for full documentation on the bootstrap
        implications of lowering this threshold.
        """
        if self._promotion_queue is None:
            from quant_foundry.promotion import PromotionGate

            min_settled = int(
                os.environ.get("QUANT_FOUNDRY_PROMOTION_MIN_SETTLED", "10")
            )
            gate = PromotionGate(min_settled_count=min_settled)
            self._promotion_queue = PromotionReviewQueue(gate=gate)
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

    # --- Tournament wiring (Agent B) ----------------------------------------

    def settlement_ledger(self) -> SettlementLedger:
        """Return the lazily constructed settlement ledger."""
        if self._settlement_ledger is None:
            self._settlement_ledger = SettlementLedger(
                root=self.base_dir / "settlements",
            )
        return self._settlement_ledger

    def tournament(self) -> Tournament:
        """Return the lazily constructed tournament scorer."""
        if self._tournament is None:
            self._tournament = Tournament()
        return self._tournament

    def tournament_sweep(self) -> TournamentSweep:
        """Return the lazily constructed tournament sweep worker."""
        if self._tournament_sweep is None:
            self._tournament_sweep = TournamentSweep(
                settlement_ledger=self.settlement_ledger(),
                dossier_registry=self.dossier_registry(),
                tournament=self.tournament(),
                leaderboard=self.expanded_leaderboard(),
            )
        return self._tournament_sweep

    def run_tournament_sweep(self) -> dict[str, Any]:
        """Run one tournament sweep and return the receipt dict.

        Reads all settlement records, scores each model, populates the
        expanded leaderboard, and returns a JSON-serializable receipt
        with scored/blocked/stale model lists. Advisory-only — never
        promotes a model.
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}
        receipt = self.tournament_sweep().sweep()
        result = receipt.to_dict()
        result["enabled"] = True
        return result

    def tournament_status(self) -> dict[str, Any]:
        """Return a summary of the current tournament state.

        Includes the ranked leaderboard, scored/blocked/stale counts,
        and the last sweep timestamp. Advisory-only — no promotion.
        """
        if not self.enabled:
            return {"enabled": False, "scored": 0, "blocked": 0, "stale": 0, "leaderboard": []}
        leaderboard = self.expanded_leaderboard()
        return {
            "enabled": True,
            "scored": len(leaderboard.ranked()),
            "blocked": 0,
            "stale": len(leaderboard.stale_models()),
            "leaderboard": [e.to_dict() for e in leaderboard.ranked()],
        }

    # --- Promotion wiring (Agent B) -----------------------------------------

    def submit_promotion(
        self,
        model_id: str,
        target_level: str,
        review_note: str,
    ) -> dict[str, Any]:
        """Submit a model for promotion review.

        Builds PromotionEvidence from the dossier, tournament result,
        and sentinel receipt (if available), then submits a
        PromotionRequest to the PromotionReviewQueue. Returns the
        pending entry dict. Advisory-only — does not promote.
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}

        from quant_foundry.promotion import (
            PromotionEvidence,
            PromotionRequest,
        )

        dossier = self.dossier_registry().get(model_id)
        if dossier is None:
            return {
                "enabled": True,
                "ok": False,
                "error_code": "no_dossier",
                "detail": f"no dossier found for model_id {model_id}",
            }

        try:
            target = DossierStatus(target_level)
        except ValueError:
            return {
                "enabled": True,
                "ok": False,
                "error_code": "invalid_target_level",
                "detail": f"invalid target_level: {target_level}",
            }

        tournament_result = self._find_tournament_result(model_id)
        sentinel_receipt = self._find_sentinel_receipt(model_id)

        blocking_issues = self._build_blocking_issues(dossier, tournament_result, sentinel_receipt)

        evidence = PromotionEvidence(
            dossier=dossier,
            tournament_result=tournament_result,
            sentinel_receipt=sentinel_receipt,
            blocking_issues=blocking_issues,
        )
        request = PromotionRequest(
            model_id=model_id,
            target_level=target,
            review_note=review_note,
        )
        self.promotion_queue().submit(request, evidence)

        pending = self.promotion_queue().pending()
        entry = pending[-1] if pending else None
        if entry is None:
            return {"enabled": True, "ok": False, "error_code": "submit_failed"}
        return {"enabled": True, "ok": True, "entry": entry.model_dump(mode="json")}

    def process_promotion(
        self,
        model_id: str,
        *,
        approve: bool,
        review_note: str,
        rejection_reason: str | None = None,
    ) -> dict[str, Any]:
        """Process the next pending promotion request for a model.

        Finds the pending entry matching ``model_id``, processes it
        through the PromotionGate, and returns the receipt dict.
        The gate fails closed — missing evidence or blocking issues
        result in REJECTED, not APPROVED.
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}

        from quant_foundry.promotion import (
            PromotionRejectionReason,
        )

        pending = self.promotion_queue().pending()
        target_entry = None
        for entry in pending:
            if entry.request.model_id == model_id:
                target_entry = entry
                break

        if target_entry is None:
            return {
                "enabled": True,
                "ok": False,
                "error_code": "no_pending_request",
                "detail": f"no pending promotion request for model_id {model_id}",
            }

        if approve:
            receipt = self._process_specific_entry(target_entry)
        else:
            rejection_reason_enum = None
            if rejection_reason is not None:
                try:
                    rejection_reason_enum = PromotionRejectionReason(rejection_reason)
                except ValueError:
                    return {
                        "enabled": True,
                        "ok": False,
                        "error_code": "invalid_rejection_reason",
                        "detail": f"invalid rejection_reason: {rejection_reason}",
                    }
            receipt = self._reject_specific_entry(target_entry, rejection_reason_enum, review_note)

        return {"enabled": True, "ok": True, "receipt": receipt.to_dict()}

    def _process_specific_entry(self, entry: Any) -> Any:
        """Process a specific pending entry through the gate."""

        pending = self.promotion_queue()._pending
        idx = pending.index(entry)
        pending.pop(idx)
        gate = self.promotion_queue()._gate
        receipt = gate.evaluate(request=entry.request, evidence=entry.evidence)
        self.promotion_queue()._completed.append(receipt)
        if receipt.decision.value == "approved":
            self.dossier_registry().update_status(
                entry.request.model_id,
                entry.request.target_level,
            )
        return receipt

    def _reject_specific_entry(self, entry: Any, rejection_reason: Any, review_note: str) -> Any:
        """Reject a specific pending entry directly."""
        from quant_foundry.promotion import PromotionReceipt, ReviewDecision

        pending = self.promotion_queue()._pending
        idx = pending.index(entry)
        pending.pop(idx)

        import time as _time

        receipt = PromotionReceipt(
            decision=ReviewDecision.REJECTED,
            request=entry.request,
            review_note=review_note,
            rejection_reason=rejection_reason,
            decided_at_ns=_time.time_ns(),
        )
        self.promotion_queue()._completed.append(receipt)
        return receipt

    def _find_tournament_result(self, model_id: str) -> Any:
        """Find the tournament result for a model from the last sweep."""
        sweep = self.tournament_sweep()
        records = sweep.settlement_ledger.read_all()
        by_model = {
            r.model_id: r for r in records if r.model_id == model_id and r.status.value == "settled"
        }
        if not by_model:
            return None
        from quant_foundry.outcomes import SettlementStatus

        model_records = [
            r for r in records if r.model_id == model_id and r.status == SettlementStatus.SETTLED
        ]
        if len(model_records) < sweep.min_settled_samples:
            return None
        scoring_input = sweep._build_scoring_input(
            model_id=model_id,
            records=model_records,
            now_ns=int(__import__("time").time_ns()),
            last_settled_at_ns=max((r.settled_at_ns or 0) for r in model_records),
            is_stale=False,
        )
        return sweep.tournament.score(scoring_input)

    def _find_sentinel_receipt(self, model_id: str) -> Any:
        """Find a sentinel receipt for a model (None if not available)."""
        return None

    def _build_blocking_issues(
        self, dossier: Any, tournament_result: Any, sentinel_receipt: Any
    ) -> list[Any]:
        """Build BlockingIssue list from dossier, tournament, and sentinel."""
        from quant_foundry.promotion import BlockingIssue
        from quant_foundry.sentinel import SentinelSeverity

        issues: list[BlockingIssue] = []

        if dossier is not None:
            for bi in dossier.blocking_issues:
                issues.append(
                    BlockingIssue(
                        code=str(bi.get("code", "unknown")),
                        severity=SentinelSeverity.BLOCKING,
                        message=str(bi.get("note") or bi.get("code", "blocking issue")),
                    )
                )

        if tournament_result is not None:
            for bi in tournament_result.blocking_issues:
                issues.append(
                    BlockingIssue(
                        code=str(bi.get("code", "unknown")),
                        severity=SentinelSeverity.BLOCKING,
                        message=str(bi.get("message", "tournament blocking issue")),
                    )
                )

        if sentinel_receipt is not None:
            for issue in sentinel_receipt.issues:
                issues.append(
                    BlockingIssue(
                        code=str(issue.code),
                        severity=issue.severity,
                        message=str(issue.message),
                    )
                )

        return issues

    def promotion_gate(self) -> Any:
        """Return the lazily constructed promotion gate.

        Exposes the same ``PromotionGate`` that the review queue uses.
        The Alpha Genome Lab (TASK-1005) requires a gate for its
        evidence-backed registration — every candidate recipe must pass
        through this gate, no shortcut, no bypass.

        The minimum settled-prediction count required for promotion is
        configurable via the ``QUANT_FOUNDRY_PROMOTION_MIN_SETTLED`` env
        var (default: 10). Setting this to 0 allows bootstrap promotion
        of newly trained models into shadow inference without prior
        settlement evidence — this is intended ONLY for the initial
        bootstrap phase when no shadow predictions exist yet. Once real
        settlements accumulate, raise this back to 10 to restore the
        full evidence requirement.

        System impact:
        - Lowering this threshold weakens the evidence requirement for
          ALL promotion levels (research_approved, shadow_approved,
          paper_approved). The gate applies the same threshold
          regardless of target level.
        - A model promoted with a low threshold will still be
          authority=SHADOW_ONLY and cannot reach live trading without
          further human approval.
        - The promotion receipt records the decision but does NOT
          record the threshold value — operators should audit env vars
          when reviewing promotion history.
        """
        if self._promotion_gate is None:
            from quant_foundry.promotion import PromotionGate

            min_settled = int(
                os.environ.get("QUANT_FOUNDRY_PROMOTION_MIN_SETTLED", "10")
            )
            self._promotion_gate = PromotionGate(min_settled_count=min_settled)
        return self._promotion_gate

    # --- Alpha Genome Lab wiring (TASK-1005) ------------------------------

    def alpha_genome_lab(
        self,
        *,
        dispatcher: Any = None,
        tournament_probe: Any = None,
    ) -> Any:
        """Return the lazily constructed Alpha Genome Lab (TASK-1005).

        The lab is **opt-in**: constructing it does not start any work.
        Operators trigger sweeps via ``start_alpha_sweep(...)``. Every
        candidate recipe flows through ``PromotionGate.evaluate()`` —
        no shortcut, no bypass (per the TASK-1005 acceptance criteria).

        Args:
            dispatcher: optional training dispatcher (a callable taking
                a ``Recipe`` and returning a TrainingOutcome). If None,
                a built-in mock is used that produces a benign
                TrainingOutcome so the sweep can be observed end-to-end
                without GPU spend. Wire a real dispatcher when RunPod
                is the dispatch target.
            tournament_probe: optional callable taking ``recipe_id`` and
                returning a tournament score (or None). When None, no
                early-stop decision is made — the lab only enforces
                budget limits.

        Returns:
            An ``AlphaGenomeLab`` instance ready to ``run_sweep``.

        Invariants (enforced by the underlying ``AlphaGenomeLab``):
        - No recipe can bypass the gate (``gate.evaluate(...)`` is the
          only registration path).
        - No recipe can be registered with authority above
          ``SHADOW_ONLY`` (alpha-genome recipes are SHADOW_ONLY by
          construction — promotion to paper_approved requires the same
          human approval path as any other model).
        - Budget exhaustion stops new trials, doesn't kill running ones.
        - No secrets in any receipt.
        """
        if self._alpha_genome_lab is None:
            from quant_foundry.alpha_genome import (
                AlphaGenomeLab,
                EarlyStopper,
                TrialBudget,
            )

            lab_dispatcher = dispatcher if dispatcher is not None else _alpha_default_dispatcher
            lab_tournament_probe = (
                tournament_probe
                if tournament_probe is not None
                else _alpha_default_tournament_probe
            )
            self._alpha_genome_lab = AlphaGenomeLab(
                gate=self.promotion_gate(),
                budget=TrialBudget(),
                early_stopper=EarlyStopper(),
                dispatcher=lab_dispatcher,
                tournament_probe=lab_tournament_probe,
                registry=_AlphaDossierUpsertAdapter(self.dossier_registry()),
            )
        return self._alpha_genome_lab

    def start_alpha_sweep(
        self,
        *,
        seed_recipe: Any,
        n_recipes: int,
        sweep_id: str | None = None,
        dispatcher: Any = None,
        tournament_probe: Any = None,
    ) -> dict[str, Any]:
        """Start an Alpha Genome Lab sweep and return the receipt as JSON.

        The sweep runs synchronously: ``run_sweep`` iterates ``n_recipes``
        mutations from the seed, dispatches each through the wired
        dispatcher, evaluates through the gate, and either registers the
        survivor via the dossier registry or discards the rest with a
        receipt. The full per-trial list is returned.

        The sweep's overall lifecycle is bounded — it runs to completion
        or stops at the budget ceiling. There is no long-running daemon.

        Args:
            seed_recipe: a ``Recipe`` instance to mutate from.
            n_recipes: number of candidate recipes to generate (>0).
            sweep_id: optional caller-supplied sweep id; if None, a
                deterministic id is derived from the seed + timestamp.
            dispatcher: optional per-call dispatcher override.
            tournament_probe: optional per-call tournament probe override.

        Returns:
            JSON-safe dict with the full ``SweepReceipt`` plus an
            ``enabled`` flag. On error, ``error_code`` + ``detail``.
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}

        lab = self.alpha_genome_lab(
            dispatcher=dispatcher,
            tournament_probe=tournament_probe,
        )
        try:
            receipt = lab.run_sweep(
                seed_recipe=seed_recipe,
                n_recipes=n_recipes,
                sweep_id=sweep_id,
            )
        except (TypeError, ValueError) as exc:
            return {
                "enabled": True,
                "ok": False,
                "error_code": "invalid_sweep_request",
                "detail": str(exc),
            }
        except Exception as exc:
            return {
                "enabled": True,
                "ok": False,
                "error_code": "sweep_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }

        payload = _sweep_receipt_to_dict(receipt)
        # Stash for status lookups. In-memory only — operator persistence
        # happens via the per-trial dossier registrations.
        self._alpha_sweep_receipts[receipt.sweep_id] = payload
        return {"enabled": True, "ok": True, "sweep": payload}

    def alpha_sweep_status(self, sweep_id: str) -> dict[str, Any] | None:
        """Return a stored sweep receipt, or None if unknown.

        Receipts are stored in-memory after ``start_alpha_sweep`` and
        are cleared on process restart. The authoritative audit trail is
        the per-trial dossier registrations (via ``DossierRegistry``).
        """
        if not self.enabled:
            return None
        return self._alpha_sweep_receipts.get(sweep_id)

    def list_alpha_sweeps(self) -> list[dict[str, Any]]:
        """Return every in-memory sweep receipt. Empty when disabled."""
        if not self.enabled:
            return []
        return list(self._alpha_sweep_receipts.values())

    def register_recipe_candidate(self, dossier: Any) -> dict[str, Any]:
        """Register a dossier produced by a recipe candidate.

        This is the explicit dossier registration contract for the Alpha
        Genome Lab. It is wired to the same ``DossierRegistry`` as every
        other model — there is no separate registry, no shortcut.

        Returns the registered dossier as a JSON-safe dict. Raises
        ``ValueError`` on a content-hash mismatch for an existing
        model_id (security event).
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}
        registered = self.dossier_registry().register(dossier)
        return {
            "enabled": True,
            "ok": True,
            "dossier": registered.model_dump(mode="json"),
        }

    # --- Settlement wiring (Agent A) ----------------------------------------

    def settlement_sweep(self) -> SettlementSweep:
        """Return the lazily constructed settlement sweep worker."""
        if self._settlement_sweep is None:
            self._settlement_sweep = SettlementSweep(
                shadow_ledger=self.shadow_ledger_real(),
                settlement_ledger=self.settlement_ledger(),
                market_data_adapter=BarDataAdapter(
                    alpaca_reader=alpaca_reader_from_env(),
                ),
                cost_model=default_cost_model(),
            )
        return cast(SettlementSweep, self._settlement_sweep)

    def run_settlement_sweep(self, now_ns: int | None = None) -> dict[str, Any]:
        """Run one settlement sweep and return the receipt dict.

        Sweeps all shadow predictions, settles expired ones, and returns
        a JSON-serializable receipt with settled / pending_time /
        pending_data / failed counts. Idempotent — safe to rerun.
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}
        receipt = self.settlement_sweep().sweep(now_ns=now_ns)
        return receipt.to_dict()

    def settlement_status(self) -> dict[str, Any]:
        """Return a summary of the current settlement state.

        Returns settled / pending_time / pending_data / failed counts
        from the settlement ledger. Empty when disabled.
        """
        if not self.enabled:
            return {
                "enabled": False,
                "settled_count": 0,
                "pending_time_count": 0,
                "pending_data_count": 0,
                "total": 0,
            }
        records = self.settlement_ledger().read_all()
        settled_count = sum(1 for r in records if r.status.value == "settled")
        pending_time_count = sum(1 for r in records if r.status.value == "pending_time")
        pending_data_count = sum(1 for r in records if r.status.value == "pending_data")
        return {
            "enabled": True,
            "settled_count": settled_count,
            "pending_time_count": pending_time_count,
            "pending_data_count": pending_data_count,
            "total": len(records),
        }

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

    def callback_metrics_store(self) -> CallbackMetricsStore:
        """Return the lazily constructed durable callback-metrics store.

        Writes JSONL at ``<base_dir>/callback_metrics/callback_metrics.jsonl``
        (under ``base_dir`` so tests get an isolated tmp_path). Used by
        ``receive_callback`` / ``poll_runpod_results`` to record
        ``received`` / ``accepted`` / ``rejected`` events and by
        ``shadow_health`` to compute a rolling rejection rate.
        """
        if self._callback_metrics_store is None:
            self._callback_metrics_store = CallbackMetricsStore(
                metrics_dir=self.base_dir / "callback_metrics",
            )
        return self._callback_metrics_store

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

        latencies = sorted(float(r.latency_ms) for r in records if r.latency_ms is not None)
        latency_p50_ms: float | None = _percentile(latencies, 0.5) if latencies else None
        latency_p95_ms: float | None = _percentile(latencies, 0.95) if latencies else None

        latest_prediction_ts: float | None = (
            float(max(r.ts_event for r in records)) if records else None
        )

        feature_availability: float | None = _aggregate_feature_availability(records)

        # The gateway rejects bad HMAC signatures without a durable inbox
        # record (see ``receive_callback``), so the rejection rate is
        # computed from the append-only ``CallbackMetricsStore`` (JSONL at
        # ``<base_dir>/callback_metrics/callback_metrics.jsonl``) rather
        # than the inbox. ``received`` events are excluded from the
        # denominator — only ``accepted`` + ``rejected`` count. Return
        # ``None`` only when no callback events have been recorded at all;
        # otherwise surface the numeric rate (even if 0.0).
        metrics_store = self.callback_metrics_store()
        if metrics_store.has_any_events():
            callback_rejection_rate: float | None = metrics_store.rejection_rate()
        else:
            callback_rejection_rate = None

        # --- Settlement wiring (Agent A) ---
        # Read real settled_count and settlement_lag_seconds from the
        # settlement ledger.
        settlement_records = self.settlement_ledger().read_all()
        settled_records = [r for r in settlement_records if r.status.value == "settled"]
        settled_count = len(settled_records)
        if settled_records and settled_records[0].settled_at_ns is not None:
            import time as _time

            settlement_lag_seconds = (_time.time_ns() - settled_records[0].settled_at_ns) / 1e9
        else:
            settlement_lag_seconds = None

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
            "settled_count": settled_count,
        }

    # --- shadow inference dispatch loop (Agent C) ---------------------------

    def dispatch_shadow_inference_batch(self) -> dict[str, Any]:
        """Dispatch one batch of shadow inference jobs for SHADOW_APPROVED models.

        Queries the dossier registry for models with status
        ``SHADOW_APPROVED`` or higher, builds a feature snapshot for each,
        and dispatches an inference job via ``create_job``. Only runs in
        ``runpod_shadow`` or ``runpod_research`` mode. Errors for one model
        are caught and recorded — a single model failing does not stop the
        rest of the batch.

        Returns a JSON-safe dispatch receipt:
        ``{"dispatched": N, "skipped": M, "job_ids": [...], "errors": [...]}``
        with an ``enabled`` / ``skipped`` flag when the gateway is disabled
        or not in shadow mode.
        """
        if not self.enabled:
            return {"enabled": False}

        if self.mode not in {"runpod_shadow", "runpod_research"}:
            return {"enabled": True, "skipped": True, "reason": "not in shadow mode"}

        import time as _time
        import uuid as _uuid

        dossiers = self.list_dossiers(status=DossierStatus.SHADOW_APPROVED)
        dispatched = 0
        skipped = 0
        job_ids: list[str] = []
        errors: list[dict[str, Any]] = []

        for dossier in dossiers:
            model_id = str(dossier.get("model_id") or "")
            if not model_id:
                skipped += 1
                continue
            try:
                snapshot_payload = self._build_shadow_snapshot_payload(model_id)
                job_id = f"shadow-inference-{model_id}-{_uuid.uuid4().hex[:12]}"
                idempotency_key = f"shadow-dispatch-{model_id}-{_time.time_ns()}"
                request_payload: dict[str, Any] = {
                    "job_id": job_id,
                    "artifact_ref": str(dossier.get("artifact_manifest_id") or model_id),
                    "symbols": [],
                    "horizons_ns": [86_400_000_000_000],
                    "snapshot": snapshot_payload,
                    "model_id": model_id,
                }
                receipt = self.create_job(
                    job_id=job_id,
                    job_type="inference",
                    idempotency_key=idempotency_key,
                    request_payload=request_payload,
                )
                if receipt.get("ok") is False:
                    errors.append(
                        {
                            "model_id": model_id,
                            "error_code": str(receipt.get("error_code") or "unknown"),
                            "detail": str(receipt.get("detail") or ""),
                        }
                    )
                    skipped += 1
                    continue
                if receipt.get("status") == "failed":
                    errors.append(
                        {
                            "model_id": model_id,
                            "error_code": "job_failed",
                            "detail": str(receipt.get("detail") or "job status is failed"),
                        }
                    )
                    skipped += 1
                    continue
                job_ids.append(job_id)
                dispatched += 1
            except Exception as exc:
                errors.append(
                    {
                        "model_id": model_id,
                        "error_code": type(exc).__name__,
                        "detail": str(exc),
                    }
                )
                skipped += 1

        self._shadow_dispatch_count += dispatched
        if dispatched > 0:
            self._last_shadow_dispatch_ns = _time.time_ns()

        return {
            "enabled": True,
            "dispatched": dispatched,
            "skipped": skipped,
            "job_ids": job_ids,
            "errors": errors,
        }

    def _build_shadow_snapshot_payload(self, model_id: str) -> dict[str, Any]:
        """Build a feature snapshot payload for a model.

        Uses ``FeatureSnapshotExport`` when feature rows are available from
        the feature lake. When no rows are available, returns a minimal
        empty snapshot so the inference worker can abstain safely rather
        than crash.
        """
        import time as _time

        decision_time = int(_time.time_ns())
        rows = self._collect_feature_rows(model_id)
        if rows:
            snapshot = export_feature_snapshot(
                rows=tuple(rows),
                decision_time=decision_time,
            )
            return snapshot.model_dump(mode="json")
        return {
            "symbols": [],
            "features": {},
            "availability": {},
            "ts_event": decision_time,
            "freshness_ns": 0,
        }

    def _collect_feature_rows(self, model_id: str) -> list[FeatureRow]:
        """Collect feature rows for a model from the feature lake.

        Returns an empty list when no feature lake is wired or no rows are
        available. This is the extension point for a real feature lake
        adapter — the default returns no rows so the dispatch loop can run
        end-to-end without a live feature store.
        """
        feature_lake = getattr(self, "_feature_lake", None)
        if feature_lake is None:
            return []
        reader = getattr(feature_lake, "read_rows", None)
        if reader is None:
            return []
        try:
            return list(reader(model_id=model_id))
        except Exception:
            return []

    @property
    def shadow_dispatch_status(self) -> dict[str, Any]:
        """Return the current shadow dispatch loop status.

        Returns a JSON-safe dict with the cumulative dispatch count, the
        last dispatch timestamp (ns), and the enabled flag. Never includes
        secrets or job payloads.
        """
        return {
            "dispatch_count": self._shadow_dispatch_count,
            "last_dispatch_ns": self._last_shadow_dispatch_ns,
            "enabled": self.enabled,
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

        # Record the inbound event first so the rejection rate's
        # denominator reflects every callback we saw (accepted + rejected
        # are recorded below). Best-effort: a metrics write failure must
        # not block the security path, but we do NOT silently swallow it
        # at the store level (the store raises on write failure). Suppress
        # OSError here so a disk error does not turn a bad-signature
        # reject into a 500.
        metrics = self.callback_metrics_store()
        with contextlib.suppress(OSError):
            metrics.record("received", reason_code=None)

        # The job must exist in the outbox (callback for unknown job = reject).
        ob_rec = self.outbox.get(job_id)
        if ob_rec is None:
            with contextlib.suppress(OSError):
                metrics.record("rejected", reason_code="unknown_job")
            return {
                "enabled": True,
                "ok": False,
                "error_code": "unknown_job",
                "detail": f"no outbox record for job_id {job_id}",
            }

        # HMAC verify FIRST (constant-time, skew-checked) via TASK-0303.
        # We verify before touching the inbox so a bad signature never
        # creates a durable record (fail-closed, no side effect).
        signature_valid = verify_callback(
            payload,
            signature,
            secret=self.callback_secret,
            ts=ts,
            job_id=job_id,
        )

        if not signature_valid:
            # Bad signature: reject immediately without recording in the
            # inbox. No domain effect, no durable trace of the bad payload.
            with contextlib.suppress(OSError):
                metrics.record("rejected", reason_code="bad_signature")
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
            with contextlib.suppress(OSError):
                metrics.record("rejected", reason_code="payload_hash_mismatch")
            return {
                "enabled": True,
                "ok": False,
                "error_code": "payload_hash_mismatch",
                "detail": str(exc),
            }

        # Process the callback (idempotent + fail-closed on schema/etc).
        proc_receipt = self.processor.process(job_id)
        with contextlib.suppress(OSError):
            metrics.record("accepted", reason_code=None)
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
# Internal helpers (TASK-1005 Alpha Genome Lab integration)
# ---------------------------------------------------------------------------


class _AlphaDossierUpsertAdapter:
    """Adapter that exposes ``upsert(dossier)`` on a ``DossierRegistry``.

    ``AlphaGenomeLab.run_sweep`` calls ``registry.upsert(dossier)`` to
    register a candidate recipe's dossier. The canonical registry exposes
    ``register(...)`` (idempotent, security-checked). This adapter bridges
    the two names without forcing the lab to know about registry internals.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def upsert(self, dossier: Any) -> Any:
        """Forward to ``DossierRegistry.register``."""
        return self._registry.register(dossier)


@dataclasses.dataclass
class _AlphaMockTrainingOutcome:
    """Mock TrainingOutcome used by the default dispatcher.

    Carries the minimum fields ``AlphaGenomeLab.run_sweep`` reads off the
    outcome (``model_id``, ``cost_cents``, ``dossier_evidence``). The
    dossier_evidence is intentionally None so the gate rejects with
    ``NO_DOSSIER`` — the safe default path. A real dispatcher must supply
    a real ``DossierRecord``.
    """

    model_id: str
    cost_cents: int = 0
    duration_seconds: float = 0.0
    dossier_evidence: Any = None
    tournament_result: Any = None
    sentinel_receipt: Any = None


def _alpha_default_dispatcher(recipe: Any) -> Any:
    """Default dispatcher for the Alpha Genome Lab.

    Returns a benign TrainingOutcome so the sweep can be observed
    end-to-end without GPU spend. The lab's gate rejects with
    ``NO_DOSSIER`` because ``dossier_evidence`` is None — this is the
    safe path. Operators wire a real dispatcher (RunPod or local training)
    in production.
    """
    return _AlphaMockTrainingOutcome(
        model_id=f"alpha-mock-{recipe.recipe_id}",
        cost_cents=0,
        duration_seconds=0.0,
        dossier_evidence=None,
        tournament_result=None,
        sentinel_receipt=None,
    )


def _alpha_default_tournament_probe(recipe_id: str) -> None:
    """Default tournament probe — always returns None (no early stop)."""
    return None


def _sweep_receipt_to_dict(receipt: Any) -> dict[str, Any]:
    """Convert a SweepReceipt dataclass to a JSON-safe dict.

    The trial_receipts list is converted to a list of dicts; the rest of
    the top-level fields are scalars. No secrets — the SweepReceipt
    carries only recipe ids, hashes, and counts.
    """
    trials: list[dict[str, Any]] = []
    for tr in receipt.trial_receipts:
        trials.append(
            {
                "recipe_id": tr.recipe_id,
                "parent_recipe_id": tr.parent_recipe_id,
                "status": tr.status.value,
                "reason": tr.reason,
                "model_id": tr.model_id,
                "cost_cents": tr.cost_cents,
                "duration_seconds": tr.duration_seconds,
                "promotion_decision": tr.promotion_decision,
                "sweep_id": tr.sweep_id,
            }
        )
    return {
        "sweep_id": receipt.sweep_id,
        "seed_recipe_id": receipt.seed_recipe_id,
        "n_recipes": receipt.n_recipes,
        "n_registered": receipt.n_registered,
        "n_rejected": receipt.n_rejected,
        "n_killed_early": receipt.n_killed_early,
        "n_discarded": receipt.n_discarded,
        "sweep_cost_cents": receipt.sweep_cost_cents,
        "started_at_ns": receipt.started_at_ns,
        "ended_at_ns": receipt.ended_at_ns,
        "trial_receipts": trials,
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
    if not isinstance(ts, (int, str)):
        return None
    try:
        callback_ts = int(ts)
    except (TypeError, ValueError):
        return None
    return payload, signature, callback_ts


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
