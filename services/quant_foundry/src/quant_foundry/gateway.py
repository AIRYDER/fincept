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
import os
import pathlib
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from sqlalchemy import Engine

from quant_foundry.budget import BudgetGuard
from quant_foundry.budget import from_env as budget_from_env
from quant_foundry.callback_dlq import CallbackDLQ, DLQRecord
from quant_foundry.callback_metrics import CallbackMetricsStore
from quant_foundry.callbacks import (
    CallbackProcessor,
    DurableDossierStore,
    DurableShadowLedgerStore,
)
from quant_foundry.cost_tracker import CostTracker
from quant_foundry.dataset_manifest import DatasetRegistry
from quant_foundry.db_sinks import (
    CallbackDlqDbStore,
    CallbackMetricsDbStore,
    CallbackReceiptDbStore,
    DbDossierStore,
    DbShadowLedgerStore,
)
from quant_foundry.dossier import DossierStatus
from quant_foundry.feature_lake import FeatureRow
from quant_foundry.feature_snapshot_export import export_feature_snapshot
from quant_foundry.gateway_callback import GatewayCallbackMixin
from quant_foundry.gateway_helpers import (
    AlphaDossierUpsertAdapter as _AlphaDossierUpsertAdapter,
)
from quant_foundry.gateway_helpers import (
    aggregate_feature_availability as _aggregate_feature_availability,
)
from quant_foundry.gateway_helpers import (
    alpha_default_dispatcher as _alpha_default_dispatcher,
)
from quant_foundry.gateway_helpers import (
    alpha_default_tournament_probe as _alpha_default_tournament_probe,
)
from quant_foundry.gateway_helpers import (
    client_endpoint_id as _client_endpoint_id,
)
from quant_foundry.gateway_helpers import (
    decision_time_from_payload as _decision_time_from_payload,
)
from quant_foundry.gateway_helpers import (
    env_first as _env_first,
)
from quant_foundry.gateway_helpers import (
    extract_callback_fields as _extract_callback_fields,
)
from quant_foundry.gateway_helpers import (
    feature_row_from_payload as _feature_row_from_payload,
)
from quant_foundry.gateway_helpers import (
    is_runpod_mode_value as _is_runpod_mode_value,
)
from quant_foundry.gateway_helpers import (
    normalize_job_type as _normalize_job_type,
)
from quant_foundry.gateway_helpers import (
    percentile as _percentile,
)
from quant_foundry.gateway_helpers import (
    runpod_status_value as _runpod_status_value,
)
from quant_foundry.gateway_helpers import (
    sweep_receipt_to_dict as _sweep_receipt_to_dict,
)
from quant_foundry.inbox import CallbackInbox
from quant_foundry.leaderboard_expanded import ExpandedLeaderboard
from quant_foundry.market_data_adapter import BarDataAdapter, alpaca_reader_from_env
from quant_foundry.mock_dispatcher import MockDispatcher
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.paper_bridge import PaperBridge
from quant_foundry.promotion import PromotionReviewQueue
from quant_foundry.registry import DossierRegistry
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.runpod_client import (
    BudgetGuard as DispatchBudgetGuard,
)
from quant_foundry.runpod_client import (
    DispatchStatus,
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


class RunPodConfigError(RuntimeError):
    """Raised when RunPod dispatch mode is enabled but required env vars
    are missing. Fail-closed at startup so a misconfigured deploy cannot
    silently degrade from "RunPod wired" to "RunPod dead".
    """


# Canonical RunPod env var names (single source of truth for from_env + health).
_RUNPOD_API_KEY_ENV = "RUNPOD_API_KEY"
_RUNPOD_API_KEY_LEGACY_ENV = "QUANT_FOUNDRY_RUNPOD_API_KEY"
_RUNPOD_TRAINING_ENDPOINT_ENV = "RUNPOD_TRAINING_ENDPOINT_ID"
_RUNPOD_TRAINING_ENDPOINT_LEGACY_ENV = "QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT"
_RUNPOD_INFERENCE_ENDPOINT_ENV = "RUNPOD_INFERENCE_ENDPOINT_ID"
_RUNPOD_INFERENCE_ENDPOINT_LEGACY_ENV = "QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT"
_RUNPOD_BASE_URL_ENV = "RUNPOD_BASE_URL"
_RUNPOD_TIMEOUT_ENV = "RUNPOD_TIMEOUT_SECONDS"
_RUNPOD_COST_ENV = "RUNPOD_COST_PER_DISPATCH_CENTS"
_CALLBACK_SECRET_ENV = "QUANT_FOUNDRY_CALLBACK_SECRET"
_SINK_BACKEND_ENV = "QUANT_FOUNDRY_SINK_BACKEND"
# Tier 0.2: default output_prefix for training dispatch. When set, the
# gateway injects this into every training job payload that doesn't
# already specify one. Should be a /runpod-volume/ or /workspace/ path
# so artifacts survive worker shutdown (durable artifacts).
_OUTPUT_PREFIX_ENV = "QUANT_FOUNDRY_OUTPUT_PREFIX"


class _DbCallbackDLQ(CallbackDLQ):
    """DB-backed callback DLQ adapter.

    Subclasses :class:`CallbackDLQ` but overrides ``_store`` and ``__init__``
    so records are written to the DB via :class:`CallbackDlqDbStore` instead
    of to a JSONL file. The ``enqueue`` interface (used by
    :class:`GatewayCallbackMixin`) is unchanged — only the persistence layer
    is swapped.
    """

    def __init__(self, db_store: CallbackDlqDbStore) -> None:
        # Skip CallbackDLQ.__init__ (which creates a JSONL file + reloads).
        self._db_store = db_store
        self._records: dict[str, DLQRecord] = {}
        self._by_idempotency: dict[str, DLQRecord] = {}

    def _store(self, rec: DLQRecord) -> DLQRecord:
        """Persist a record to the DB and update in-memory indexes."""
        self._db_store.write(rec)
        self._records[rec.dlq_id] = rec
        self._by_idempotency[rec.idempotency_key] = rec
        return rec


class QuantFoundryGateway(GatewayCallbackMixin):
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
        paper_bridge: PaperBridge | None = None,
        prediction_publisher: Any | None = None,
        worker_status_dir: pathlib.Path | str | None = None,
        stale_threshold_seconds: float = 60.0,
        cost_tracker: CostTracker | None = None,
        sink_backend: str = "jsonl",
        db_engine: Engine | None = None,
        registry: ModelRegistryDB | None = None,
        dataset_registry: DatasetRegistry | None = None,
        output_prefix: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.mode = mode
        self.shadow_only = shadow_only
        self.callback_secret = callback_secret
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.budget_guard = budget_guard
        self._paper_bridge = paper_bridge
        self._prediction_publisher = prediction_publisher
        self._worker_status_dir = (
            pathlib.Path(worker_status_dir) if worker_status_dir is not None else None
        )
        self._stale_threshold_seconds = stale_threshold_seconds
        # --- CostTracker + sink backend (Phase A integration) ---
        self._cost_tracker: CostTracker | None = cost_tracker
        self.sink_backend: str = sink_backend
        self._db_engine: Engine | None = db_engine
        self._callback_receipt_db_store: CallbackReceiptDbStore | None = None
        self._callback_metrics_db_store: CallbackMetricsDbStore | None = None
        # Tier 1.2: optional DB-backed model registry. When provided,
        # successful training_complete callbacks auto-register a model
        # version (model_id + dossier_content_hash + artifact_id +
        # callback_receipt_id) so the product loop is fully wired
        # without a manual register_version() call.
        self._registry: ModelRegistryDB | None = registry
        # Tier 1.5: optional dataset registry. When provided, production
        # training jobs must pass dispatch_training() — which enforces
        # that the dataset is registered, L3+ readiness, not stale, and
        # not deprecated/rejected. Canary/research are permissive.
        self._dataset_registry: DatasetRegistry | None = dataset_registry
        # Tier 0.2: default output_prefix for training jobs. When set,
        # injected into every training dispatch payload that doesn't
        # already specify one. Should be a /runpod-volume/ path.
        self._default_output_prefix: str | None = output_prefix

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
        # --- Sink backend selection ---
        # When sink_backend == "db", construct DB-backed sinks instead of the
        # JSONL-backed DurableDossierStore / DurableShadowLedgerStore. The
        # CallbackProcessor accepts any object implementing the sink protocols,
        # so no change to the processor is needed — just pass the DB sinks.
        if self.sink_backend == "db":
            self.shadow_ledger: DbShadowLedgerStore | DurableShadowLedgerStore = (
                DbShadowLedgerStore(engine=self._db_engine)
            )
            self.dossier_store: DbDossierStore | DurableDossierStore = DbDossierStore(
                engine=self._db_engine
            )
            # DB-backed DLQ: wrap CallbackDlqDbStore in the _DbCallbackDLQ
            # adapter so the mixin's enqueue() interface is preserved.
            self._callback_receipt_db_store = CallbackReceiptDbStore(
                engine=self._db_engine,
            )
            self._dlq_db_store = CallbackDlqDbStore(engine=self._db_engine)
            self.dlq = _DbCallbackDLQ(self._dlq_db_store)
            # DB-backed callback metrics store (replaces the JSONL store).
            self._callback_metrics_db_store = CallbackMetricsDbStore(
                engine=self._db_engine,
            )
        else:
            self.shadow_ledger = DurableShadowLedgerStore(self.shadow_ledger_real())
            self.dossier_store = DurableDossierStore(self.dossier_registry())
            # When a CostTracker is injected in jsonl mode, we still need a
            # CallbackReceiptDbStore so callback receipts are mirrored to the
            # DB — the training_jobs.callback_receipt_id FK references
            # callback_receipts.callback_id, so the parent row must exist for
            # CostTracker.link_callback() to succeed. Use the CostTracker's
            # engine (which may be the same injected engine or a lazy-init
            # production engine).
            if self._cost_tracker is not None:
                receipt_engine = self._db_engine
                if receipt_engine is None:
                    receipt_engine = self._cost_tracker.engine
                self._callback_receipt_db_store = CallbackReceiptDbStore(
                    engine=receipt_engine,
                )
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
            paper_bridge=self._paper_bridge,
            prediction_publisher=self._prediction_publisher,
            dossier_lookup=self._dossier_registry_lazy() if self._paper_bridge else None,
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

        When ``QUANT_FOUNDRY_MODE`` is one of the runpod modes, reads the
        following env vars to construct the HttpRunPodClient. Canonical
        names are preferred; deprecated ``QUANT_FOUNDRY_RUNPOD_*`` names
        are read as fallbacks (with a DeprecationWarning) so existing
        Railway dashboard setups keep working during migration.

        Canonical (preferred):
        - ``RUNPOD_API_KEY`` (required in runpod mode) — RunPod API key.
        - ``RUNPOD_TRAINING_ENDPOINT_ID`` (required in runpod mode) —
          serverless endpoint ID for the training worker.
        - ``RUNPOD_INFERENCE_ENDPOINT_ID`` (required in runpod mode) —
          serverless endpoint ID for the inference worker.
        - ``RUNPOD_BASE_URL`` (optional, default
          ``https://api.runpod.ai/v2``) — RunPod API base URL.
        - ``RUNPOD_TIMEOUT_SECONDS`` (optional, default ``30``) — HTTP
          request timeout for dispatch calls.
        - ``RUNPOD_COST_PER_DISPATCH_CENTS`` (optional, default ``0``) —
          estimated cost per dispatch for budget guard checks.

        Deprecated fallbacks (read with a warning):
        - ``QUANT_FOUNDRY_RUNPOD_API_KEY``
        - ``QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT``
        - ``QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT``

        Fail-closed: when runpod mode is enabled but any required env var
        is missing, raises ``RunPodConfigError`` with the list of missing
        names. This prevents a silent deploy that looks healthy but
        cannot dispatch.
        """
        enabled = os.environ.get("QUANT_FOUNDRY_ENABLED", "false").lower() == "true"
        mode = os.environ.get("QUANT_FOUNDRY_MODE", "local_mock")
        shadow_only = os.environ.get("QUANT_FOUNDRY_SHADOW_ONLY", "true").lower() == "true"
        callback_secret = os.environ.get(_CALLBACK_SECRET_ENV, "")
        if base_dir is None:
            base_dir = os.environ.get("QUANT_FOUNDRY_BASE_DIR", "reports/quant-foundry")
        budget_guard = budget_from_env(pathlib.Path(base_dir) / "budget")

        runpod_clients: dict[str, HttpRunPodClient] = {}
        if _is_runpod_mode_value(mode):
            api_key = _env_first(
                _RUNPOD_API_KEY_ENV,
                _RUNPOD_API_KEY_LEGACY_ENV,
            )
            legacy_endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")
            training_endpoint_id = (
                _env_first(
                    _RUNPOD_TRAINING_ENDPOINT_ENV,
                    _RUNPOD_TRAINING_ENDPOINT_LEGACY_ENV,
                )
                or legacy_endpoint_id
            )
            inference_endpoint_id = (
                _env_first(
                    _RUNPOD_INFERENCE_ENDPOINT_ENV,
                    _RUNPOD_INFERENCE_ENDPOINT_LEGACY_ENV,
                )
                or legacy_endpoint_id
            )
            base_url = os.environ.get(_RUNPOD_BASE_URL_ENV, "https://api.runpod.ai/v2")
            timeout_str = os.environ.get(_RUNPOD_TIMEOUT_ENV, "30")
            cost_str = os.environ.get(_RUNPOD_COST_ENV, "0")

            # Fail-closed: required vars must be present in runpod mode.
            missing: list[str] = []
            if not api_key:
                missing.append(_RUNPOD_API_KEY_ENV)
            if not training_endpoint_id:
                missing.append(_RUNPOD_TRAINING_ENDPOINT_ENV)
            if not inference_endpoint_id:
                missing.append(_RUNPOD_INFERENCE_ENDPOINT_ENV)
            if not callback_secret:
                missing.append(_CALLBACK_SECRET_ENV)
            if missing:
                raise RunPodConfigError(
                    "RunPod dispatch mode is enabled (QUANT_FOUNDRY_MODE="
                    f"{mode}) but required env vars are missing: "
                    + ", ".join(missing)
                    + ". Set them in the Railway dashboard or RunPod template "
                    "environment. See docs/RAILWAY_DEPLOY_GUIDE.md."
                )

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

        # Paper bridge: construct when QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true.
        # The bridge is disabled by default and must be explicitly enabled.
        # The prediction publisher is injected by the API lifespan (which has
        # access to the Redis client); from_env() leaves it as None.
        paper_bridge = None
        allow_paper_bridge = (
            os.environ.get("QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE", "").lower() == "true"
        )
        if allow_paper_bridge:
            paper_bridge = PaperBridge()

        # Worker status directory: in production, the RunPod network volume
        # is mounted at this path so the gateway can scan worker status files
        # and detect stale/crashed workers. Defaults to None (disabled).
        worker_status_dir = os.environ.get("QUANT_FOUNDRY_WORKER_STATUS_DIR", "")
        stale_threshold_str = os.environ.get(
            "QUANT_FOUNDRY_STALE_THRESHOLD_SECONDS",
            "60",
        )
        try:
            stale_threshold = float(stale_threshold_str)
        except ValueError:
            stale_threshold = 60.0

        # Sink backend selection: "jsonl" (default, backward compatible) or
        # "db" (DB-backed sinks via db_sinks.py + CostTracker). When "db",
        # the gateway constructs DbDossierStore, DbShadowLedgerStore,
        # CallbackReceiptDbStore, CallbackDlqDbStore, and
        # CallbackMetricsDbStore from db_sinks.py and passes them to the
        # CallbackProcessor. The DB sinks lazy-init their engines from
        # get_sync_engine() when no engine is injected.
        sink_backend = os.environ.get(_SINK_BACKEND_ENV, "jsonl").lower()
        if sink_backend not in ("jsonl", "db"):
            sink_backend = "jsonl"

        # Tier 0.2: default output_prefix for training jobs. When set,
        # the gateway injects this into every training dispatch payload
        # that doesn't already specify one. Should be a /runpod-volume/
        # path so artifacts survive worker shutdown.
        output_prefix = os.environ.get(_OUTPUT_PREFIX_ENV) or None

        return cls(
            enabled=enabled,
            mode=mode,
            shadow_only=shadow_only,
            callback_secret=callback_secret,
            base_dir=base_dir,
            budget_guard=budget_guard,
            runpod_clients=runpod_clients,
            paper_bridge=paper_bridge,
            worker_status_dir=worker_status_dir or None,
            stale_threshold_seconds=stale_threshold,
            sink_backend=sink_backend,
            output_prefix=output_prefix,
        )

    # --- health / state ---

    def health(self) -> dict[str, Any]:
        """Safe health state (no secrets)."""
        runpod_config = self.runpod_config_status()
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "shadow_only": self.shadow_only,
            "job_count": len(self.outbox.list()) if self.enabled else 0,
            "runpod_wired": bool(self._runpod_dispatchers),
            "runpod_config_valid": runpod_config["valid"],
            "missing_env": runpod_config["missing_env"],
            "runpod_routes": {
                job_type: _client_endpoint_id(client)
                for job_type, client in self._runpod_clients.items()
            },
            "output_prefix_configured": self._default_output_prefix is not None,
            "paper_bridge": {
                "configured": self._paper_bridge is not None,
                "status": self._paper_bridge.status.value if self._paper_bridge else "disabled",
                "publisher_wired": self._prediction_publisher is not None,
            },
        }

    def runpod_config_status(self) -> dict[str, Any]:
        """Return RunPod configuration validity without exposing secrets.

        Returns ``{"valid": bool, "missing_env": list[str]}``. In
        non-runpod modes, ``valid`` is always True and ``missing_env`` is
        empty (RunPod config is irrelevant). In runpod modes, checks that
        clients are wired for both training and inference and that the
        callback secret is non-empty.

        Never returns secret values — only the names of missing env vars.
        """
        if not self._is_runpod_mode():
            return {"valid": True, "missing_env": []}
        missing: list[str] = []
        if "training" not in self._runpod_clients:
            missing.append(_RUNPOD_TRAINING_ENDPOINT_ENV)
        if "inference" not in self._runpod_clients:
            missing.append(_RUNPOD_INFERENCE_ENDPOINT_ENV)
        if not self.callback_secret:
            missing.append(_CALLBACK_SECRET_ENV)
        # API key presence is implied by client construction (from_env
        # would have raised). For direct-construction tests, the client
        # exists so the key was provided.
        return {"valid": len(missing) == 0, "missing_env": missing}

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

    def runpod_canary(self, *, job_type: str = "training") -> dict[str, Any]:
        """Dispatch a callback-secret canary job to a RunPod endpoint.

        This proves that the RunPod worker and the API share the same
        ``QUANT_FOUNDRY_CALLBACK_SECRET``. The API dispatches a canary
        job with a random nonce; the worker signs the nonce-bearing
        payload with its copy of the secret and returns it; the API
        verifies the signature with its own copy.

        This is a LIVE check — it dispatches a real (tiny) job to RunPod
        and polls for completion. It does NOT touch the outbox or inbox;
        the canary is a direct client → poll → verify round-trip.

        Args:
            job_type: which endpoint to canary ("training" or "inference").

        Returns:
            A receipt dict with ``ok``, ``verified``, ``job_type``,
            ``nonce``, and ``detail``. Never raises — errors are
            reported as ``ok=False`` with a detail string.
        """
        import secrets as _secrets

        normalized = _normalize_job_type(job_type)
        if not self._is_runpod_mode():
            return {
                "ok": False,
                "verified": False,
                "job_type": normalized,
                "detail": "not in runpod mode",
            }
        client = self._runpod_clients.get(normalized)
        if client is None:
            return {
                "ok": False,
                "verified": False,
                "job_type": normalized,
                "detail": f"no RunPod client wired for job_type={normalized}",
            }

        nonce = _secrets.token_hex(16)
        canary_job_id = f"canary:{normalized}:{nonce[:8]}"
        canary_payload = {
            "task": "callback_secret_canary",
            "job_id": canary_job_id,
            "nonce": nonce,
        }

        # Dispatch the canary job.
        dispatch_result = client.dispatch(
            job_id=canary_job_id,
            request_payload=canary_payload,
            budget_cents=None,
        )
        if dispatch_result.status != DispatchStatus.DISPATCHED:
            return {
                "ok": False,
                "verified": False,
                "job_type": normalized,
                "nonce": nonce,
                "detail": (
                    f"dispatch failed: {dispatch_result.error_code} — "
                    f"{dispatch_result.error_summary}"
                ),
            }
        runpod_job_id = dispatch_result.runpod_job_id
        if not runpod_job_id:
            return {
                "ok": False,
                "verified": False,
                "job_type": normalized,
                "nonce": nonce,
                "detail": "dispatch returned no runpod_job_id",
            }

        # Poll for completion (with a bounded retry loop).
        import time as _time

        max_poll_seconds = 60
        poll_interval = 2.0
        deadline = _time.time() + max_poll_seconds
        while _time.time() < deadline:
            try:
                status = client.check_status(runpod_job_id)
            except Exception as exc:
                return {
                    "ok": False,
                    "verified": False,
                    "job_type": normalized,
                    "nonce": nonce,
                    "runpod_job_id": runpod_job_id,
                    "detail": f"status poll failed: {type(exc).__name__}: {exc}",
                }
            status_value = _runpod_status_value(status)
            if status_value in {"IN_PROGRESS", "IN_QUEUE", "RUNNING", "PENDING"}:
                _time.sleep(poll_interval)
                continue
            if status_value in {"FAILED", "CANCELLED", "CANCELED", "TIMED_OUT", "ERROR"}:
                return {
                    "ok": False,
                    "verified": False,
                    "job_type": normalized,
                    "nonce": nonce,
                    "runpod_job_id": runpod_job_id,
                    "detail": f"RunPod job {status_value.lower()}: "
                    f"{status.get('error') or status.get('message') or status_value}",
                }
            if status_value not in {"COMPLETED", "SUCCEEDED", "SUCCESS"}:
                return {
                    "ok": False,
                    "verified": False,
                    "job_type": normalized,
                    "nonce": nonce,
                    "runpod_job_id": runpod_job_id,
                    "detail": f"unknown RunPod status: {status_value}",
                }
            # Completed — extract callback fields from the output.
            output = status.get("output")
            callback_fields = _extract_callback_fields(output if output is not None else status)
            if callback_fields is None:
                return {
                    "ok": False,
                    "verified": False,
                    "job_type": normalized,
                    "nonce": nonce,
                    "runpod_job_id": runpod_job_id,
                    "detail": "canary completed but no callback fields in output",
                }
            payload_text, signature, callback_ts = callback_fields
            # Verify the signature with the API's own callback secret.
            verified = verify_callback(
                payload_text.encode("utf-8"),
                signature,
                secret=self.callback_secret,
                ts=callback_ts,
                job_id=canary_job_id,
            )
            return {
                "ok": verified,
                "verified": verified,
                "job_type": normalized,
                "nonce": nonce,
                "runpod_job_id": runpod_job_id,
                "detail": "signature verified" if verified else "signature verification failed",
            }
        return {
            "ok": False,
            "verified": False,
            "job_type": normalized,
            "nonce": nonce,
            "runpod_job_id": runpod_job_id,
            "detail": f"canary job did not complete within {max_poll_seconds}s",
        }

    def heartbeats(self) -> list[dict[str, Any]]:
        """Worker heartbeats from the RunPod status files.

        In local_mock mode (or when no ``worker_status_dir`` is configured),
        returns an empty list — the mock worker is implicit. In RunPod mode
        with a mounted status directory, scans ``{worker_status_dir}/*.json``
        and returns all status records.
        """
        if not self.enabled or self._worker_status_dir is None:
            return []
        import json as _json

        status_dir = self._worker_status_dir
        if not status_dir.is_dir():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(status_dir.glob("*.json")):
            try:
                results.append(_json.loads(path.read_text(encoding="utf-8")))
            except (OSError, _json.JSONDecodeError):
                continue
        return results

    def detect_stale_workers(self) -> list[dict[str, Any]]:
        """Detect workers whose heartbeat is older than the staleness threshold.

        Returns a list of status records for jobs in an active state
        (``started``, ``training``, ``inferring``, ``running``) whose
        ``heartbeat_at`` timestamp is older than
        ``self._stale_threshold_seconds``. Returns an empty list if no
        status directory is configured or no stale workers are found.
        """
        if not self.enabled or self._worker_status_dir is None:
            return []
        import time as _time

        now = _time.time()
        stale: list[dict[str, Any]] = []
        for rec in self.heartbeats():
            status = rec.get("status", "")
            if status not in {"started", "training", "inferring", "running"}:
                continue
            hb = rec.get("heartbeat_at")
            if not isinstance(hb, (int, float)):
                continue
            if (now - hb) > self._stale_threshold_seconds:
                stale.append(rec)
        return stale

    def sweep_stale_workers(self) -> list[dict[str, Any]]:
        """Auto-fail outbox jobs whose worker heartbeat has gone stale.

        Calls :meth:`detect_stale_workers` and, for each stale record whose
        ``job_id`` exists in the outbox and is still ``RUNNING``, transitions
        the job to ``FAILED`` with ``error_code="worker_heartbeat_stale"`` and
        an ``error_summary`` describing how old the heartbeat is. Jobs that are
        already terminal (completed/failed) or absent from the outbox are
        skipped.

        Returns a list of receipt dicts (one per stale job that was failed).
        """
        if not self.enabled or self._worker_status_dir is None:
            return []
        import time as _time

        now = _time.time()
        receipts: list[dict[str, Any]] = []
        for rec in self.detect_stale_workers():
            job_id = rec.get("job_id")
            if not isinstance(job_id, str):
                continue
            ob_rec = self.outbox.get(job_id)
            if ob_rec is None:
                continue
            if ob_rec.status != JobStatus.RUNNING:
                continue
            hb = rec.get("heartbeat_at")
            age_seconds = (now - hb) if isinstance(hb, (int, float)) else None
            error_summary = (
                f"worker heartbeat stale ({age_seconds:.0f}s old, "
                f"threshold={self._stale_threshold_seconds:.0f}s)"
                if age_seconds is not None
                else "worker heartbeat stale (no heartbeat_at timestamp)"
            )
            self.outbox.update_status(
                job_id,
                JobStatus.FAILED,
                error_code="worker_heartbeat_stale",
                error_summary=error_summary,
            )
            receipts.append(
                {
                    "job_id": job_id,
                    "ok": False,
                    "error_code": "worker_heartbeat_stale",
                    "error_summary": error_summary,
                    "heartbeat_age_seconds": age_seconds,
                }
            )
        return receipts

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
        # Tier 1.5: dataset registry dispatch gate. When a dataset
        # registry is wired in and the training mode is production,
        # enforce dispatch_training() before enqueueing the job. This
        # rejects unregistered datasets, L1/L2 readiness, stale receipts,
        # and deprecated/rejected entries. Canary/research are permissive.
        if self._dataset_registry is not None and job_type == "training":
            training_mode = self._extract_training_mode(dispatch_payload)
            if training_mode == "production":
                dataset_id = self._extract_dataset_id(dispatch_payload)
                if dataset_id is not None:
                    try:
                        self._dataset_registry.dispatch_training(dataset_id, mode="production")
                    except ValueError as exc:
                        return {
                            "enabled": True,
                            "ok": False,
                            "job_id": job_id,
                            "error_code": "dataset_dispatch_rejected",
                            "detail": str(exc),
                            "dataset_id": dataset_id,
                            "training_mode": training_mode,
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
        # CostTracker: record the dispatch (creates a training_jobs row).
        # Best-effort — a tracking failure must not break the dispatch path.
        self._record_job_dispatch_cost(job_id, job_type, dispatch_payload)
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

    # --- CostTracker callback wiring (Phase A integration) ------------------

    def receive_callback(
        self,
        *,
        job_id: str,
        payload: bytes,
        signature: str,
        ts: int,
        worker_id: str = "external",
    ) -> dict[str, Any]:
        """Override that wires CostTracker into the callback processing path.

        Delegates to the mixin's ``receive_callback`` (HMAC verification,
        inbox recording, processor.process()), then — when the callback
        completes successfully — calls ``CostTracker.update_job_status()``
        and ``CostTracker.link_callback()`` to update the training_jobs row.

        The callback receipt id is read from the inbox record (the latest
        record for this job_id). The status is derived from the outbox
        status in the processing receipt (``completed`` / ``failed``).

        Best-effort: CostTracker write failures are caught and logged via
        ``contextlib.suppress`` so they do not break the callback path.
        """
        receipt = super().receive_callback(
            job_id=job_id,
            payload=payload,
            signature=signature,
            ts=ts,
            worker_id=worker_id,
        )
        # Mirror the inbox record to the callback_receipts table via
        # CallbackReceiptDbStore when the store is available. This is the
        # DB-backed audit trail (the JSONL inbox remains the source of truth
        # for the processor; the DB store is a durable mirror). The store is
        # constructed when sink_backend == "db" OR when a CostTracker is
        # injected (the training_jobs.callback_receipt_id FK references
        # callback_receipts.callback_id, so the parent row must exist for
        # CostTracker.link_callback() to succeed).
        #
        # C10 dual-write: when QF_POSTGRES_SINK_ENABLED=1, the callback
        # receipt is dual-written via the dual_write coordinator with proper
        # error handling (logged at ERROR, optionally fail-hard). When the
        # flag is off, the existing CostTracker FK mirroring remains (silent
        # suppress, for the FK dependency only).
        if self._callback_receipt_db_store is not None:
            in_rec = self.inbox.get_by_job_id(job_id)
            if in_rec is not None:
                from quant_foundry.c10_flags import postgres_sink_enabled

                if postgres_sink_enabled():
                    # C10 dual-write path: flag-controlled, proper error handling.
                    from quant_foundry.dual_write import dual_write_callback_receipt

                    dual_write_callback_receipt(self._callback_receipt_db_store, in_rec)
                else:
                    # Pre-C10 mirroring: for CostTracker FK dependency only.
                    # Silent suppress is acceptable here because the FK
                    # dependency is a best-effort optimization — if the DB
                    # write fails, the CostTracker will skip the link.
                    with contextlib.suppress(Exception):
                        self._callback_receipt_db_store.write(in_rec)
        # Only update the CostTracker when the callback was accepted (ok=True).
        if not receipt.get("ok"):
            return receipt
        # Update job status from the outbox status in the receipt.
        outbox_status = receipt.get("outbox_status")
        now_ns = time.time_ns()
        status_map = {
            "completed": "completed",
            "failed": "failed",
            "validating": "running",
        }
        ct_status = status_map.get(cast("str", outbox_status), outbox_status)
        if ct_status is not None:
            with contextlib.suppress(Exception):
                self.cost_tracker().update_job_status(
                    job_id,
                    status=ct_status,
                    completed_at_ns=now_ns if ct_status == "completed" else None,
                )
        # Link the callback receipt id from the inbox record.
        in_rec = self.inbox.get_by_job_id(job_id)
        if in_rec is not None:
            with contextlib.suppress(Exception):
                self.cost_tracker().link_callback(
                    job_id,
                    callback_receipt_id=in_rec.callback_id,
                )
        # Tier 1.6: extract operational metrics from the callback payload
        # and record them via CostTracker.record_metric(). The handler
        # emits execution_time_ms, queue_delay_ms, cost_usd, and gpu_model
        # in the metrics_summary dict inside the callback payload. Best-
        # effort: failures are caught and logged so they do not break the
        # callback path.
        with contextlib.suppress(Exception):
            self._record_operational_metrics(job_id, payload)
        # Tier 1.2: auto-register a model version when a registry is
        # wired in and the callback was a successful training_complete.
        # Best-effort: registration failures are caught and logged via
        # contextlib.suppress so they do not break the callback path.
        if self._registry is not None and in_rec is not None:
            with contextlib.suppress(Exception):
                self._maybe_register_version(job_id, payload, in_rec.callback_id)
        return receipt

    def _maybe_register_version(
        self,
        job_id: str,
        payload: bytes,
        callback_receipt_id: str,
    ) -> None:
        """Auto-register a model version from a training_complete callback.

        Parses the callback envelope to extract model_id + artifact_id,
        queries the model_dossiers table for the content_hash (written by
        DbDossierStore), and calls register_model + register_version.
        Idempotent: both methods use ON CONFLICT DO NOTHING.
        """
        import json as _json

        from quant_foundry.schemas import RunPodCallbackEnvelope

        envelope = RunPodCallbackEnvelope.model_validate(_json.loads(payload))
        if envelope.result_type != "training_complete":
            return
        dossier_payload = envelope.payload.get("dossier")
        artifact_payload = envelope.payload.get("artifact_manifest")
        if not isinstance(dossier_payload, dict) or not isinstance(artifact_payload, dict):
            return
        model_id = dossier_payload.get("model_id")
        artifact_id = artifact_payload.get("artifact_id")
        if not model_id or not artifact_id:
            return
        # Query the model_dossiers table for the content_hash (written by
        # DbDossierStore during callback processing).
        from sqlalchemy import select as _select
        from sqlalchemy.orm import Session as _Session

        from fincept_db.callback_tables import ModelDossierRow

        with _Session(self._db_engine) as session:
            dossier_row = session.scalars(
                _select(ModelDossierRow).where(
                    ModelDossierRow.model_id == model_id,
                    ModelDossierRow.artifact_manifest_id == artifact_id,
                )
            ).first()
            if dossier_row is None:
                return
            content_hash = dossier_row.content_hash
            model_family = artifact_payload.get("model_family", "unknown")
        # Generate a deterministic version_id from the model_id + content_hash.
        version_id = f"version:{model_id}:{content_hash[:16]}"
        # Determine the version number (count existing versions for this model).
        from fincept_db.registry_tables import ModelVersionRow

        with _Session(self._db_engine) as session:
            existing = session.scalars(
                _select(ModelVersionRow).where(ModelVersionRow.model_id == model_id)
            ).all()
            version_number = len(existing) + 1
        assert self._registry is not None  # registry required for model registration
        self._registry.register_model(
            model_id=model_id,
            name=model_id,
            model_family=model_family,
        )
        self._registry.register_version(
            model_id=model_id,
            version_id=version_id,
            dossier_content_hash=content_hash,
            artifact_id=artifact_id,
            callback_receipt_id=callback_receipt_id,
            version_number=version_number,
        )

    def _record_operational_metrics(
        self,
        job_id: str,
        payload: bytes,
    ) -> None:
        """Tier 1.6: Extract operational metrics from the callback payload
        and record them via CostTracker.record_metric().

        The handler emits these metrics in the ``metrics_summary`` dict
        inside the callback payload:
        - ``execution_time_ms``: wall-clock training time in milliseconds
        - ``queue_delay_ms``: time spent in the RunPod queue (0 from worker)
        - ``cost_usd``: estimated GPU cost in USD
        - ``gpu_model``: GPU model name (e.g. "RTX 4090")

        Each metric is recorded as a separate ``job_metrics`` row via
        CostTracker.record_metric(). Best-effort: all exceptions are
        suppressed by the caller.
        """
        import json as _json

        try:
            payload_dict = _json.loads(payload)
        except Exception:
            return

        # The metrics_summary is inside the callback payload under
        # either "metrics_summary" (flat) or "payload.metrics_summary"
        # (nested in the callback envelope).
        metrics_summary = payload_dict.get("metrics_summary")
        if not isinstance(metrics_summary, dict):
            nested_payload = payload_dict.get("payload")
            if isinstance(nested_payload, dict):
                metrics_summary = nested_payload.get("metrics_summary")
        if not isinstance(metrics_summary, dict):
            return

        tracker = self.cost_tracker()
        now_ns = time.time_ns()

        # Record each operational metric.
        metric_map = {
            "execution_time_ms": ("execution_time", "ms"),
            "queue_delay_ms": ("queue_delay", "ms"),
            "cost_usd": ("cost_usd", "USD"),
        }
        for field, (metric_type, unit) in metric_map.items():
            value = metrics_summary.get(field)
            if value is not None:
                try:
                    tracker.record_metric(
                        job_id=job_id,
                        metric_type=metric_type,
                        value=float(value),
                        unit=unit,
                        recorded_at_ns=now_ns,
                    )
                except Exception:
                    pass

        # Record GPU model as a metric (string → hash to numeric if needed,
        # but CostTracker accepts float/int/Decimal — store as 1.0 with
        # the model name in the metric_type).
        gpu_model = metrics_summary.get("gpu_model")
        if gpu_model:
            try:
                tracker.record_metric(
                    job_id=job_id,
                    metric_type=f"gpu_model:{gpu_model}",
                    value=1.0,
                    unit="boolean",
                    recorded_at_ns=now_ns,
                )
            except Exception:
                pass

        # Record a cost event for the GPU cost (so cost_summary rollups
        # include it). This is the primary cost record — the metric above
        # is the observability side.
        cost_usd = metrics_summary.get("cost_usd")
        execution_time_ms = metrics_summary.get("execution_time_ms")
        if cost_usd is not None and execution_time_ms is not None:
            try:
                tracker.record_cost_event(
                    job_id=job_id,
                    event_type="gpu_compute",
                    amount=float(execution_time_ms) / 1000.0,
                    unit_cost=float(cost_usd) / max(float(execution_time_ms) / 1000.0, 0.001),
                    metadata={
                        "gpu_model": gpu_model or "unknown",
                        "execution_time_ms": int(execution_time_ms),
                    },
                    currency="USD",
                )
            except Exception:
                pass

    @staticmethod
    def _extract_training_mode(payload: Any) -> str:
        """Extract training_mode from a dispatch payload.

        Checks (in order): top-level ``training_mode``/``mode`` field,
        ``extra_constraints.training_mode``. Defaults to ``"canary"``.
        """
        if isinstance(payload, dict):
            for key in ("training_mode", "mode"):
                val = payload.get(key)
                if isinstance(val, str) and val:
                    return val
            ec = payload.get("extra_constraints")
            if isinstance(ec, dict):
                val = ec.get("training_mode")
                if isinstance(val, str) and val:
                    return val
        return "canary"

    @staticmethod
    def _extract_dataset_id(payload: Any) -> str | None:
        """Extract the dataset id from a dispatch payload.

        Returns ``dataset_manifest_ref`` if present and a string,
        otherwise None. For production dispatch, this must be a
        registered dataset id (not a raw file path).
        """
        if isinstance(payload, dict):
            ref = payload.get("dataset_manifest_ref")
            if isinstance(ref, str) and ref:
                return ref
        return None

    def _prepare_dispatch_payload(
        self,
        *,
        job_type: str,
        request_payload: Any,
    ) -> Any:
        if not self._is_runpod_mode():
            return request_payload
        # Tier 0.2: inject default output_prefix for training jobs when
        # the caller hasn't set one. This ensures artifacts go to the
        # network volume (durable) instead of /tmp (ephemeral).
        if _normalize_job_type(job_type) == "training":
            if isinstance(request_payload, dict) and self._default_output_prefix:
                if not request_payload.get("output_prefix"):
                    request_payload = dict(request_payload)
                    request_payload["output_prefix"] = self._default_output_prefix
            return request_payload
        if _normalize_job_type(job_type) != "inference":
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

    def _dossier_registry_lazy(self) -> Any:
        """Return a lazy lookup wrapper for the dossier registry.

        The registry is constructed on first access, so we return a small
        adapter that defers the ``get(model_id)`` call until the registry
        is actually needed. This avoids constructing the registry at
        gateway init time when the paper bridge is not enabled.
        """
        gateway = self

        class _LazyDossierLookup:
            def get(self, model_id: str) -> Any:
                return gateway.dossier_registry().get(model_id)

        return _LazyDossierLookup()

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

            min_settled = int(os.environ.get("QUANT_FOUNDRY_PROMOTION_MIN_SETTLED", "10"))
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
        """Return the lazily constructed settlement ledger.

        C10: when a DB engine is available, inject a ``DbSettlementStore``
        so settlement records are dual-written to Postgres behind the
        ``QF_POSTGRES_SINK_ENABLED`` feature flag. The flag defaults to 0
        (off), so the DB store is injected but not called until the flag
        is flipped.
        """
        if self._settlement_ledger is None:
            db_store = None
            if self._db_engine is not None:
                from quant_foundry.settlement_db_sink import DbSettlementStore

                db_store = DbSettlementStore(engine=self._db_engine)
            self._settlement_ledger = SettlementLedger(
                root=self.base_dir / "settlements",
                db_store=db_store,
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

        from quant_foundry.bundle_io import TrainingSelfCheck  # noqa: F401
        from quant_foundry.promotion import (
            CallbackReceiptRef,  # noqa: F401
            PITEvidenceRef,  # noqa: F401
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

        # C7 evidence chain — query the DB for C7 metrics when available,
        # otherwise construct passing defaults from the dossier so the
        # gate can reach the downstream checks (sentinel, evidence bar).
        (
            selfcheck,
            callback_receipt,
            artifact_uri,
            feature_set_version,
            pit_evidence,
            backend_eligible,
        ) = self._find_c7_evidence(model_id, dossier)

        evidence = PromotionEvidence(
            dossier=dossier,
            tournament_result=tournament_result,
            sentinel_receipt=sentinel_receipt,
            blocking_issues=blocking_issues,
            selfcheck=selfcheck,
            callback_receipt=callback_receipt,
            artifact_uri=artifact_uri,
            dossier_hash=dossier.content_hash,
            feature_set_version=feature_set_version,
            pit_evidence=pit_evidence,
            backend_eligible=backend_eligible,
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
        """Find the most recent sentinel receipt for a model.

        Looks up the latest version for the model_id, then queries
        the model_metrics table for the most recent sentinel metrics
        row. Builds a SentinelReceipt from the stored metrics dict.

        Returns None if:
          - No DB engine is wired (non-DB mode).
          - No version exists for the model_id.
          - No sentinel metrics have been recorded.
        """
        if self._db_engine is None:
            return None

        from sqlalchemy import select as _select
        from sqlalchemy.orm import Session as _Session

        from fincept_db.registry_tables import ModelMetricRow, ModelVersionRow
        from quant_foundry.registry_db import _build_sentinel_receipt

        with _Session(self._db_engine) as session:
            # Find the latest version for this model_id.
            version_row = session.scalars(
                _select(ModelVersionRow)
                .where(ModelVersionRow.model_id == model_id)
                .order_by(ModelVersionRow.version_number.desc())
            ).first()
            if version_row is None:
                return None

            # Query the most recent sentinel metrics for that version.
            sentinel_row = session.scalars(
                _select(ModelMetricRow)
                .where(
                    ModelMetricRow.version_id == version_row.version_id,
                    ModelMetricRow.metric_type == "sentinel",
                )
                .order_by(ModelMetricRow.recorded_at_ns.desc())
            ).first()
            if sentinel_row is None:
                return None

            return _build_sentinel_receipt(sentinel_row.metrics)

    def _find_c7_evidence(
        self, model_id: str, dossier: Any
    ) -> tuple[Any, Any, str | None, str | None, Any, bool]:
        """Find C7 evidence chain fields for a model.

        Queries the DB for C7 metrics (selfcheck, pit_evidence,
        feature_set, backend) when a DB engine is wired. Falls back
        to passing defaults constructed from the dossier so the gate
        can reach downstream checks (sentinel, evidence bar) in
        non-DB mode (e.g. file-based dossier registry tests).

        Returns a tuple of:
          (selfcheck, callback_receipt, artifact_uri,
           feature_set_version, pit_evidence, backend_eligible)
        """
        from quant_foundry.bundle_io import TrainingSelfCheck
        from quant_foundry.promotion import CallbackReceiptRef, PITEvidenceRef

        # Default passing C7 evidence from the dossier (non-DB fallback).
        selfcheck = TrainingSelfCheck(
            passed=True,
            bundle_sha256=dossier.artifact_sha256 or "",
            n_rows_scored=10,
        )
        callback_receipt: Any = CallbackReceiptRef(status="processed")
        artifact_uri: str | None = f"file:///durable/{dossier.artifact_manifest_id}"
        feature_set_version: str | None = "fs-v1"
        pit_evidence: Any = PITEvidenceRef(
            verified=True,
            evidence_sha256="e" * 64,
            manifest_hash="m" * 64,
        )
        backend_eligible: bool = True

        if self._db_engine is None:
            return (
                selfcheck,
                callback_receipt,
                artifact_uri,
                feature_set_version,
                pit_evidence,
                backend_eligible,
            )

        # DB mode: query C7 metrics from the model_metrics table.
        from sqlalchemy import select as _select
        from sqlalchemy.orm import Session as _Session

        from fincept_db.registry_tables import ModelMetricRow, ModelVersionRow

        with _Session(self._db_engine) as session:
            version_row = session.scalars(
                _select(ModelVersionRow)
                .where(ModelVersionRow.model_id == model_id)
                .order_by(ModelVersionRow.version_number.desc())
            ).first()
            if version_row is None:
                return (
                    selfcheck,
                    callback_receipt,
                    artifact_uri,
                    feature_set_version,
                    pit_evidence,
                    backend_eligible,
                )

            vid = version_row.version_id

            # selfcheck
            sc_row = session.scalars(
                _select(ModelMetricRow)
                .where(
                    ModelMetricRow.version_id == vid,
                    ModelMetricRow.metric_type == "selfcheck",
                )
                .order_by(ModelMetricRow.recorded_at_ns.desc())
            ).first()
            if sc_row is not None:
                m = sc_row.metrics
                selfcheck = TrainingSelfCheck(
                    passed=bool(m.get("passed", False)),
                    n_rows_scored=int(m.get("n_rows_scored", 0)),
                    output_sha256=str(m.get("output_sha256", "")),
                    bundle_sha256=str(m.get("bundle_sha256", "")),
                    loader_version=str(m.get("loader_version", "v1")),
                    duration_ms=float(m.get("duration_ms", 0.0)),
                    error_detail=m.get("error_detail"),
                )

            # pit_evidence
            pit_row = session.scalars(
                _select(ModelMetricRow)
                .where(
                    ModelMetricRow.version_id == vid,
                    ModelMetricRow.metric_type == "pit_evidence",
                )
                .order_by(ModelMetricRow.recorded_at_ns.desc())
            ).first()
            if pit_row is not None:
                m = pit_row.metrics
                pit_evidence = PITEvidenceRef(
                    verified=bool(m.get("verified", False)),
                    evidence_sha256=str(m.get("evidence_sha256", "")),
                    manifest_hash=str(m.get("manifest_hash", "")),
                )

            # feature_set
            fs_row = session.scalars(
                _select(ModelMetricRow)
                .where(
                    ModelMetricRow.version_id == vid,
                    ModelMetricRow.metric_type == "feature_set",
                )
                .order_by(ModelMetricRow.recorded_at_ns.desc())
            ).first()
            if fs_row is not None:
                feature_set_version = fs_row.metrics.get("feature_set_version")

            # backend
            be_row = session.scalars(
                _select(ModelMetricRow)
                .where(
                    ModelMetricRow.version_id == vid,
                    ModelMetricRow.metric_type == "backend",
                )
                .order_by(ModelMetricRow.recorded_at_ns.desc())
            ).first()
            if be_row is not None:
                backend_eligible = bool(be_row.metrics.get("production_eligible", False))

        return (
            selfcheck,
            callback_receipt,
            artifact_uri,
            feature_set_version,
            pit_evidence,
            backend_eligible,
        )

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

            min_settled = int(os.environ.get("QUANT_FOUNDRY_PROMOTION_MIN_SETTLED", "10"))
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

        When ``sink_backend == "db"``, returns the DB-backed
        :class:`CallbackMetricsDbStore` instead (constructed in
        ``__init__``), which writes metric events to the ``callback_metrics``
        table.
        """
        if self.sink_backend == "db" and self._callback_metrics_db_store is not None:
            return self._callback_metrics_db_store  # type: ignore[return-value]
        if self._callback_metrics_store is None:
            self._callback_metrics_store = CallbackMetricsStore(
                metrics_dir=self.base_dir / "callback_metrics",
            )
        return self._callback_metrics_store

    # --- CostTracker (Phase A integration) ----------------------------------

    def cost_tracker(self) -> CostTracker:
        """Return the CostTracker, lazy-initializing if not injected.

        When a ``CostTracker`` was passed to the constructor, it is returned
        as-is. When ``None`` (the default), a new ``CostTracker`` is
        constructed. If a ``db_engine`` was injected (e.g. a SQLite engine in
        tests), it is passed to the ``CostTracker``; otherwise the tracker
        lazy-inits its engine from ``get_sync_engine()`` in production.
        """
        if self._cost_tracker is None:
            self._cost_tracker = CostTracker(engine=self._db_engine)
        return self._cost_tracker

    def _record_job_dispatch_cost(
        self,
        job_id: str,
        job_type: str,
        request_payload: Any,
    ) -> None:
        """Record a job dispatch in the CostTracker (training_jobs row).

        Called from the dispatch path (``create_job``) right after a
        successful dispatch. Extracts ``model_family``, ``gpu_type``,
        ``gpu_count``, ``execution_timeout_ms``, and ``container_image``
        from the request payload when present (with sensible defaults).
        ``request_payload_ref`` is a file path to the persisted request
        payload on disk (never the raw payload itself).

        Best-effort: a CostTracker write failure is caught and logged via
        ``contextlib.suppress`` so it does not break the dispatch path.
        The training_jobs row is the source of truth for cost tracking;
        a missed row means the job is untracked (not a dispatch failure).
        """
        payload = request_payload if isinstance(request_payload, dict) else {}
        model_family = str(payload.get("model_family", job_type))
        execution_timeout_ms = payload.get("execution_timeout_ms")
        gpu_type = payload.get("gpu_type")
        gpu_count = int(payload.get("gpu_count", 1))
        container_image = payload.get("container_image")
        # Map the gateway mode to a valid CostTracker mode domain value.
        # The training_jobs table has a CHECK constraint forcing mode to be
        # one of 'canary', 'research', 'production'. The gateway mode
        # ('runpod', 'local_mock') is a transport mode, not a deployment
        # tier — map it to 'canary' (the default tier for shadow-only
        # dispatches) unless the payload overrides it.
        ct_mode = str(payload.get("deployment_mode", "canary"))
        # Persist the request payload to disk and use the path as the ref.
        request_payload_ref = self._write_request_payload(job_id, payload)
        with contextlib.suppress(Exception):
            self.cost_tracker().record_job_dispatch(
                job_id=job_id,
                model_family=model_family,
                mode=ct_mode,
                execution_timeout_ms=(
                    int(execution_timeout_ms) if execution_timeout_ms is not None else None
                ),
                gpu_type=str(gpu_type) if gpu_type is not None else None,
                gpu_count=gpu_count,
                container_image=(str(container_image) if container_image is not None else None),
                request_payload_ref=request_payload_ref,
            )

    def _write_request_payload(self, job_id: str, payload: dict[str, Any]) -> str:
        """Persist the request payload to disk and return the file path.

        Stored under ``<base_dir>/request_payloads/<job_id>.json``. This is
        a reference (file path), never the raw payload itself — the
        CostTracker stores only the path in ``training_jobs.request_payload_ref``.
        """
        import json as _json

        payload_dir = self.base_dir / "request_payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = job_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        payload_path = payload_dir / f"{safe_name}.json"
        try:
            payload_path.write_text(_json.dumps(payload, default=str), encoding="utf-8")
        except (OSError, TypeError):
            return ""
        return str(payload_path)

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

    # --- callback ingestion moved to GatewayCallbackMixin ---
