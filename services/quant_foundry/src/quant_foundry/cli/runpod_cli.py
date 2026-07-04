"""
quant_foundry.cli.runpod_cli — unified RunPod CLI surface (T-OP.1).

This module replaces the scattered entry points into the RunPod training
pipeline with **one safe command surface**. The guiding invariant:

    **No command trains locally.**

Local execution is limited to manifest validation, request construction,
and receipt verification. All training is dispatched remotely to RunPod
(or a mock dispatcher in tests).

Commands
--------
- ``dataset register``  — register a dataset in the registry (local).
- ``dataset upload``    — validate a manifest and construct an upload
  request (local).
- ``train canary``      — dispatch a canary training job to RunPod
  (remote). Budget capped at ``canary_budget_usd``. Requires a
  registered dataset (fail-closed if a raw CSV path is provided).
- ``train production``  — dispatch a production training job to RunPod
  (remote). Budget capped at ``default_budget_usd``. Requires a
  registered dataset (fail-closed if a raw CSV path is provided).
- ``train status``      — show dispatch, queue, worker, callback,
  artifact verification, and final eligibility states (local; queries
  the job ledger).
- ``train verify``      — verify an artifact for a completed job
  (local).
- ``train cost``        — show a cost estimate for a job (local).

Fail-closed semantics
---------------------
Production and canary train commands require a registered ``dataset_id``.
If a raw CSV path is provided instead, the preflight check raises
``ValueError`` *before* dispatch — the remote worker is never contacted
with an unregistered raw file. This is the "one safe path" guarantee:
operators cannot accidentally train production on an unvetted CSV.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from quant_foundry.dataset_manifest import (
    DatasetRegistry,
    ReadinessLevel,
    SourceReceipt,
    TrainingMode,
    UploadReceipt,
    _is_raw_data_uri,
    _is_registry_eligible_id,
)
from quant_foundry.job_ledger import (
    JobLedgerState,
    TrainingJobLedger,
)

# ---------------------------------------------------------------------------
# Dispatch protocol (mockable remote surface)
# ---------------------------------------------------------------------------


class DispatchFn(Protocol):
    """Callable that dispatches a job to RunPod (or a mock).

    The CLI never calls the real RunPod HTTP API directly. Instead it
    accepts a ``dispatch_fn`` (injected at construction or per-call) so
    tests can substitute a deterministic mock. The function receives the
    resolved job id, the request payload, and the budget cap in cents,
    and returns a dict with at least ``runpod_job_id``, ``status``,
    ``cost_cents``, and ``duration_seconds``.
    """

    def __call__(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int,
    ) -> dict[str, Any]: ...


def _default_dispatch_fn(
    *,
    job_id: str,
    request_payload: dict[str, Any],
    budget_cents: int,
) -> dict[str, Any]:
    """Default dispatch function.

    In production this would be replaced by an HTTP call to RunPod. For
    safety, the default raises ``RuntimeError`` so that a CLI constructed
    without an explicit ``dispatch_fn`` cannot silently "succeed" by
    doing nothing. Tests inject a mock.
    """
    raise RuntimeError(
        "no dispatch_fn configured: RunPodCLI requires an explicit "
        "dispatch_fn for remote training commands (inject a mock in tests)"
    )


# ---------------------------------------------------------------------------
# Pydantic v2 models
# ---------------------------------------------------------------------------


class CLIConfig(BaseModel):
    """Frozen configuration for :class:`RunPodCLI`.

    All fields are validated at construction. The config is immutable
    (``frozen=True``) and rejects unknown fields (``extra="forbid"``) so
    a misconfigured CLI fails loudly rather than silently using defaults.

    Fields:
        runpod_api_key_env: name of the environment variable holding the
            RunPod API key. The key is *read* from the environment at
            dispatch time, never stored on the config.
        callback_secret_env: name of the environment variable holding the
            HMAC callback secret.
        default_gpu_type: the default GPU type for RunPod pods.
        default_budget_usd: the budget cap (USD) for production training.
        canary_budget_usd: the budget cap (USD) for canary training.
        receipt_dir: directory where training receipts are written.
        require_registered_dataset: when True (default), production and
            canary train commands fail-closed if a raw CSV path is
            provided instead of a registered dataset id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runpod_api_key_env: str = "RUNPOD_API_KEY"
    callback_secret_env: str = "CALLBACK_SECRET"
    default_gpu_type: str = "RTX_4090"
    default_budget_usd: float = 10.0
    canary_budget_usd: float = 1.0
    receipt_dir: str = "reports/runpod-training"
    require_registered_dataset: bool = True

    @field_validator("default_budget_usd")
    @classmethod
    def _default_budget_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"default_budget_usd must be > 0; got {v}")
        return v

    @field_validator("canary_budget_usd")
    @classmethod
    def _canary_budget_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"canary_budget_usd must be > 0; got {v}")
        return v


class CommandSpec(BaseModel):
    """Description of one CLI command.

    Frozen + ``extra="forbid"``. Used by :func:`list_commands` and
    :func:`render_help` to describe the available command surface.

    Fields:
        command: the command string (e.g. ``"train production"``).
        description: a human-readable summary.
        requires_registered_dataset: True if the command requires a
            registered dataset id (fail-closed on raw CSV).
        trains_remotely: True for train commands that dispatch to RunPod;
            False for local commands (register, upload, status, verify,
            cost).
        budget_cap: the budget cap (USD) for this command, or None for
            commands without a budget.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    description: str
    requires_registered_dataset: bool
    trains_remotely: bool
    budget_cap: float | None = None


class CommandResult(BaseModel):
    """Result of one CLI command invocation.

    Frozen + ``extra="forbid"``. Returned by :meth:`RunPodCLI.dispatch`
    and every ``cmd_*`` handler.

    Fields:
        command: the command string that was invoked.
        success: True if the command completed without error.
        message: a human-readable summary of the outcome.
        job_id: the job id (for train commands), or None.
        receipt_path: the path to a written receipt, or None.
        error: an error message, or None on success.
        duration_seconds: wall-clock duration of the command.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    success: bool
    message: str
    job_id: str | None = None
    receipt_path: str | None = None
    error: str | None = None
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------

_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command="dataset register",
        description=(
            "Register a dataset in the registry. Local operation — "
            "validates the dataset id and records manifest/data URIs."
        ),
        requires_registered_dataset=False,
        trains_remotely=False,
        budget_cap=None,
    ),
    CommandSpec(
        command="dataset upload",
        description=(
            "Validate a manifest and construct an upload request. "
            "Local operation — no remote training."
        ),
        requires_registered_dataset=False,
        trains_remotely=False,
        budget_cap=None,
    ),
    CommandSpec(
        command="train canary",
        description=(
            "Dispatch a canary training job to RunPod. Requires a "
            "registered dataset id (fail-closed on raw CSV). Budget "
            "capped at canary_budget_usd."
        ),
        requires_registered_dataset=True,
        trains_remotely=True,
        budget_cap=None,  # filled per-config at list_commands time
    ),
    CommandSpec(
        command="train production",
        description=(
            "Dispatch a production training job to RunPod. Requires a "
            "registered dataset id (fail-closed on raw CSV). Budget "
            "capped at default_budget_usd."
        ),
        requires_registered_dataset=True,
        trains_remotely=True,
        budget_cap=None,
    ),
    CommandSpec(
        command="train status",
        description=(
            "Show dispatch, queue, worker, callback, artifact "
            "verification, and final eligibility states for a job. "
            "Local operation — queries the job ledger."
        ),
        requires_registered_dataset=False,
        trains_remotely=False,
        budget_cap=None,
    ),
    CommandSpec(
        command="train verify",
        description=(
            "Verify an artifact for a completed job. Local operation — artifact verification."
        ),
        requires_registered_dataset=False,
        trains_remotely=False,
        budget_cap=None,
    ),
    CommandSpec(
        command="train cost",
        description=(
            "Show a cost estimate for a job. Local operation — cost report from the job ledger."
        ),
        requires_registered_dataset=False,
        trains_remotely=False,
        budget_cap=None,
    ),
)

# Map of command string -> CommandSpec for quick lookup.
_COMMAND_MAP: dict[str, CommandSpec] = {spec.command: spec for spec in _COMMAND_SPECS}


def list_commands(config: CLIConfig | None = None) -> list[CommandSpec]:
    """Return all available commands with descriptions.

    If ``config`` is provided, the ``budget_cap`` for train commands is
    filled from the config (canary → ``canary_budget_usd``, production →
    ``default_budget_usd``). Without a config, the budget caps are
    ``None``.

    Args:
        config: optional :class:`CLIConfig` to populate budget caps.

    Returns:
        A list of :class:`CommandSpec` instances.
    """
    if config is None:
        return list(_COMMAND_SPECS)
    specs: list[CommandSpec] = []
    for spec in _COMMAND_SPECS:
        if spec.command == "train canary":
            specs.append(spec.model_copy(update={"budget_cap": config.canary_budget_usd}))
        elif spec.command == "train production":
            specs.append(spec.model_copy(update={"budget_cap": config.default_budget_usd}))
        else:
            specs.append(spec)
    return specs


def render_help(config: CLIConfig | None = None) -> str:
    """Return formatted help text showing all commands and usage.

    Args:
        config: optional :class:`CLIConfig` to populate budget caps in
            the command listing.

    Returns:
        A multi-line help string.
    """
    lines: list[str] = []
    lines.append("RunPod CLI — unified training command surface")
    lines.append("=" * 52)
    lines.append("")
    lines.append("Usage: dispatch(command, args)")
    lines.append("")
    lines.append("Commands:")
    for spec in list_commands(config):
        remote_tag = "remote" if spec.trains_remotely else "local"
        budget_tag = (
            f"  [budget cap: ${spec.budget_cap:.2f}]" if spec.budget_cap is not None else ""
        )
        reg_tag = "  [requires registered dataset]" if spec.requires_registered_dataset else ""
        lines.append(f"  {spec.command:<20} ({remote_tag}){budget_tag}{reg_tag}")
        lines.append(f"      {spec.description}")
    lines.append("")
    lines.append("Safety invariants:")
    lines.append("  - No command trains locally (all training is remote).")
    lines.append("  - Production/canary train commands require a registered dataset_id")
    lines.append("    (fail-closed if a raw CSV path is provided).")
    lines.append("  - Preflight checks run before dispatch; failures never")
    lines.append("    contact the remote worker.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Preflight validation
# ---------------------------------------------------------------------------

# Required arguments per command (beyond the command string itself).
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "dataset register": (
        "dataset_id",
        "manifest_uri",
        "data_uri",
    ),
    "dataset upload": (
        "dataset_id",
        "manifest_uri",
        "data_uri",
    ),
    "train canary": ("dataset_id",),
    "train production": ("dataset_id",),
    "train status": ("job_id",),
    "train verify": ("job_id",),
    "train cost": ("job_id",),
}

# Commands that require a registered dataset (fail-closed on raw CSV).
_REQUIRES_REGISTERED: frozenset[str] = frozenset({"train canary", "train production"})


def _looks_like_raw_csv(value: str) -> bool:
    """Return True if ``value`` looks like a raw CSV/data file path.

    A registered dataset id is an opaque slug (no path separators, no
    raw-data extensions). A raw CSV path ends in ``.csv``, contains a
    path separator, or uses ``inline://``.
    """
    if not value:
        return False
    if _is_raw_data_uri(value):
        return True
    if not _is_registry_eligible_id(value):
        return True
    return False


def validate_preflight(command: str, args: dict, config: CLIConfig) -> None:
    """Run fail-closed preflight checks for ``command``.

    For production/canary train commands:
    - A ``dataset_id`` must be provided (not a raw CSV path). If a raw
      CSV path is provided, raise ``ValueError``.
    - If ``config.require_registered_dataset`` is True, the dataset id
      must be a registry-eligible slug (not a raw file path).

    For all commands:
    - Required arguments (per :data:`_REQUIRED_ARGS`) must be present and
      non-empty.

    Fail-closed: any unmet requirement raises ``ValueError`` *before*
    dispatch. The remote worker is never contacted with invalid input.

    Args:
        command: the command string (e.g. ``"train production"``).
        args: the arguments dict for the command.
        config: the :class:`CLIConfig`.

    Raises:
        ValueError: if any preflight check fails.
        KeyError: if the command is unknown (re-raised as ValueError).
    """
    if command not in _COMMAND_MAP:
        raise ValueError(f"unknown command: {command!r}")

    # Check required args are present and non-empty.
    required = _REQUIRED_ARGS.get(command, ())
    missing: list[str] = []
    for key in required:
        val = args.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(key)
    if missing:
        raise ValueError(f"command {command!r} missing required args: {missing}")

    # Fail-closed: train commands require a registered dataset id, not a
    # raw CSV path.
    if command in _REQUIRES_REGISTERED and config.require_registered_dataset:
        dataset_id = args.get("dataset_id", "")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError(
                f"command {command!r} requires a dataset_id (got empty/missing dataset_id)"
            )
        if _looks_like_raw_csv(dataset_id):
            raise ValueError(
                "production commands require a registered dataset_id, "
                f"not a raw CSV: {dataset_id!r}"
            )


# ---------------------------------------------------------------------------
# Status report formatting
# ---------------------------------------------------------------------------


def format_status_report(status: dict) -> str:
    """Format a job status dict as readable text showing all states.

    The status dict (typically from :meth:`TrainingJobLedger.trace`)
    contains the ledger record fields plus a ``trajectory`` and
    ``terminal`` flag. This function renders a human-readable report
    showing:

    - dispatch state (dispatched / runpod_job_id)
    - queue state (queued / outbox_id)
    - worker state (runpod_running)
    - callback state (callbacks received)
    - artifact verification state (artifact_id / artifact_verified)
    - final eligibility state (terminal / rejected / failed / expired)

    Args:
        status: a dict with ledger record fields.

    Returns:
        A multi-line readable status report.
    """
    lines: list[str] = []
    ledger_id = status.get("ledger_id", "?")
    state = status.get("state", "?")
    outbox_id = status.get("outbox_id")
    runpod_job_id = status.get("runpod_job_id")
    dataset_id = status.get("dataset_id")
    artifact_id = status.get("artifact_id")
    callbacks = status.get("callbacks", ()) or ()
    failures = status.get("failures", ()) or ()
    cost_cents = status.get("cost_cents", 0)
    duration = status.get("duration_seconds", 0.0)
    retries = status.get("retries", 0)
    terminal = status.get("terminal", False)
    trajectory = status.get("trajectory", ()) or ()

    lines.append(f"Job Status Report — {ledger_id}")
    lines.append("=" * 40)
    lines.append(f"  Current state : {state}")
    lines.append(f"  Terminal      : {'yes' if terminal else 'no'}")
    lines.append("")
    lines.append("Identifiers:")
    lines.append(f"  outbox_id      : {outbox_id}")
    lines.append(f"  runpod_job_id  : {runpod_job_id}")
    lines.append(f"  dataset_id     : {dataset_id}")
    lines.append(f"  artifact_id    : {artifact_id}")
    lines.append("")
    lines.append("State trajectory:")
    if trajectory:
        for step in trajectory:
            s = step.get("state", "?")
            ts = step.get("ts_ns")
            lines.append(f"  -> {s}" + (f"  (ts={ts})" if ts is not None else ""))
    else:
        lines.append("  (no transitions recorded)")
    lines.append("")
    lines.append("Callbacks:")
    if callbacks:
        for cb in callbacks:
            lines.append(f"  - {cb}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Failures:")
    if failures:
        for f in failures:
            code = f.get("error_code", "?")
            msg = f.get("error_message", "")
            lines.append(f"  - [{code}] {msg}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Cost & duration:")
    lines.append(f"  cost_cents     : {cost_cents}")
    lines.append(f"  duration_seconds: {duration}")
    lines.append(f"  retries        : {retries}")
    lines.append("")
    # Final eligibility summary.
    if terminal:
        if state == JobLedgerState.ARTIFACT_VERIFIED.value:
            lines.append("Final eligibility: ELIGIBLE (artifact verified)")
        elif state == JobLedgerState.REJECTED.value:
            lines.append("Final eligibility: REJECTED")
        elif state == JobLedgerState.FAILED.value:
            lines.append("Final eligibility: FAILED")
        elif state == JobLedgerState.EXPIRED.value:
            lines.append("Final eligibility: EXPIRED")
        else:
            lines.append(f"Final eligibility: terminal ({state})")
    else:
        lines.append("Final eligibility: PENDING (not terminal)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# RunPodCLI
# ---------------------------------------------------------------------------


class RunPodCLI:
    """Unified RunPod CLI dispatcher.

    One safe command surface for all RunPod training operations. No
    command trains locally — local execution is limited to manifest
    validation, request construction, and receipt verification.

    The CLI is constructed with a :class:`CLIConfig` and optional
    dependencies (a :class:`DatasetRegistry`, a
    :class:`TrainingJobLedger`, and a ``dispatch_fn``). These are
    injected for testability; in production they default to fresh
    instances backed by the config's paths.

    Args:
        config: the :class:`CLIConfig`.
        registry: optional :class:`DatasetRegistry`. If None, an
            in-memory registry is created.
        ledger: optional :class:`TrainingJobLedger`. If None, an
            in-memory ledger is created (base_dir from a temp path).
        dispatch_fn: optional callable matching :class:`DispatchFn`.
            Required for train commands; the default raises
            ``RuntimeError`` so a misconfigured CLI cannot silently
            "succeed".
    """

    def __init__(
        self,
        config: CLIConfig,
        *,
        registry: DatasetRegistry | None = None,
        ledger: TrainingJobLedger | None = None,
        dispatch_fn: DispatchFn | Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.config: CLIConfig = config
        self.registry: DatasetRegistry = registry if registry is not None else DatasetRegistry()
        self.ledger: TrainingJobLedger = (
            ledger if ledger is not None else TrainingJobLedger(base_dir=config.receipt_dir)
        )
        self._dispatch_fn: Callable[..., dict[str, Any]] = (
            dispatch_fn if dispatch_fn is not None else _default_dispatch_fn
        )

    # --- dispatch --------------------------------------------------------

    def dispatch(self, command: str, args: dict) -> CommandResult:
        """Parse ``command`` and dispatch to the appropriate handler.

        Runs :func:`validate_preflight` before dispatch (fail-closed).
        Returns a :class:`CommandResult` describing the outcome. Any
        exception from a handler is caught and converted into a failed
        :class:`CommandResult` (so the CLI never crashes the caller).

        Args:
            command: the command string (e.g. ``"train production"``).
            args: the arguments dict for the command.

        Returns:
            A :class:`CommandResult`.
        """
        start = time.monotonic()
        try:
            validate_preflight(command, args, self.config)
        except ValueError as exc:
            return CommandResult(
                command=command,
                success=False,
                message="preflight validation failed",
                error=str(exc),
                duration_seconds=time.monotonic() - start,
            )

        handler = self._HANDLERS.get(command)
        if handler is None:
            return CommandResult(
                command=command,
                success=False,
                message=f"unknown command: {command!r}",
                error=f"unknown command: {command!r}",
                duration_seconds=time.monotonic() - start,
            )

        try:
            result: CommandResult = handler(self, args)
            # Fill in duration if the handler left it at 0.
            if result.duration_seconds == 0.0:
                result = result.model_copy(update={"duration_seconds": time.monotonic() - start})
            return result
        except Exception as exc:
            return CommandResult(
                command=command,
                success=False,
                message=f"command {command!r} failed",
                error=f"{type(exc).__name__}: {exc}",
                duration_seconds=time.monotonic() - start,
            )

    # --- handlers --------------------------------------------------------

    def cmd_dataset_register(self, args: dict) -> CommandResult:
        """Register a dataset in the registry.

        Local operation (no remote training). Validates the dataset id
        and records the manifest/data URIs (and optional hashes, quality
        report, source receipts, readiness level).

        Required args: ``dataset_id``, ``manifest_uri``, ``data_uri``.
        Optional args: ``manifest_sha256``, ``data_sha256``,
        ``quality_report_uri``, ``quality_report_sha256``,
        ``source_receipts`` (list of dicts), ``readiness_level``.
        """
        dataset_id = args["dataset_id"]
        manifest_uri = args["manifest_uri"]
        data_uri = args["data_uri"]
        source_receipts_raw = args.get("source_receipts")
        source_receipts: tuple[SourceReceipt, ...] = ()
        if source_receipts_raw:
            source_receipts = tuple(
                SourceReceipt(**sr) if isinstance(sr, dict) else sr for sr in source_receipts_raw
            )
        entry = self.registry.register(
            dataset_id=dataset_id,
            manifest_uri=manifest_uri,
            data_uri=data_uri,
            manifest_sha256=args.get("manifest_sha256"),
            data_sha256=args.get("data_sha256"),
            quality_report_uri=args.get("quality_report_uri"),
            quality_report_sha256=args.get("quality_report_sha256"),
            source_receipts=source_receipts,
            readiness_level=args.get("readiness_level", ReadinessLevel.L1_RAW),
        )
        return CommandResult(
            command="dataset register",
            success=True,
            message=(
                f"dataset {dataset_id!r} registered at version "
                f"{entry.version} (readiness {entry.readiness_level.value})"
            ),
        )

    def cmd_dataset_upload(self, args: dict) -> CommandResult:
        """Validate a manifest and construct an upload request.

        Local operation (manifest validation + upload request
        construction). Does not perform the actual upload — it validates
        that the dataset is registered and the manifest/data URIs are
        consistent, then returns the constructed upload request payload.

        Required args: ``dataset_id``, ``manifest_uri``, ``data_uri``.
        Optional args: ``data_sha256``, ``issued_at``, ``expires_at``.
        """
        dataset_id = args["dataset_id"]
        # Verify the dataset is registered (fail-closed).
        if not self.registry.is_registered(dataset_id):
            raise ValueError(
                f"dataset {dataset_id!r} is not registered (upload requires a registered dataset)"
            )
        entry = self.registry.inspect(dataset_id)
        # Validate manifest_uri / data_uri consistency with the registry.
        if entry.manifest_uri != args["manifest_uri"]:
            raise ValueError(
                f"manifest_uri mismatch: args have {args['manifest_uri']!r}, "
                f"registry has {entry.manifest_uri!r}"
            )
        if entry.data_uri != args["data_uri"]:
            raise ValueError(
                f"data_uri mismatch: args have {args['data_uri']!r}, "
                f"registry has {entry.data_uri!r}"
            )
        # Construct the upload request payload (not sent anywhere).
        now = int(time.time())
        issued_at = args.get("issued_at", now)
        expires_at = args.get("expires_at", now + 86_400)
        receipt = UploadReceipt(
            receipt_id=f"upload-{dataset_id}-{now}",
            dataset_id=dataset_id,
            data_uri=entry.data_uri,
            data_sha256=args.get("data_sha256", entry.data_sha256),
            issued_at=issued_at,
            expires_at=expires_at,
        )
        {
            "dataset_id": dataset_id,
            "manifest_uri": entry.manifest_uri,
            "data_uri": entry.data_uri,
            "receipt_id": receipt.receipt_id,
            "receipt_hash": receipt.receipt_hash(),
            "issued_at": receipt.issued_at,
            "expires_at": receipt.expires_at,
        }
        return CommandResult(
            command="dataset upload",
            success=True,
            message=(
                f"upload request constructed for dataset {dataset_id!r} "
                f"(receipt {receipt.receipt_id})"
            ),
        )

    def cmd_train_canary(self, args: dict) -> CommandResult:
        """Dispatch a canary training job to RunPod.

        Requires a registered dataset (fail-closed if a raw CSV path is
        provided — enforced by :func:`validate_preflight`). Budget
        capped at ``config.canary_budget_usd``. Returns the job id.

        Required args: ``dataset_id``.
        Optional args: ``model_type``, ``hyperparams``, ``job_id``.
        """
        return self._dispatch_train(
            command="train canary",
            mode=TrainingMode.CANARY,
            budget_usd=self.config.canary_budget_usd,
            args=args,
        )

    def cmd_train_production(self, args: dict) -> CommandResult:
        """Dispatch a production training job to RunPod.

        Requires a registered dataset (fail-closed if a raw CSV path is
        provided — enforced by :func:`validate_preflight`). Budget
        capped at ``config.default_budget_usd``. Returns the job id.

        Required args: ``dataset_id``.
        Optional args: ``model_type``, ``hyperparams``, ``job_id``.
        """
        return self._dispatch_train(
            command="train production",
            mode=TrainingMode.PRODUCTION,
            budget_usd=self.config.default_budget_usd,
            args=args,
        )

    def _dispatch_train(
        self,
        *,
        command: str,
        mode: TrainingMode,
        budget_usd: float,
        args: dict,
    ) -> CommandResult:
        """Shared dispatch logic for canary/production train commands.

        Validates the dataset against the registry (fail-closed), builds
        the request payload, dispatches via ``dispatch_fn``, records the
        job in the ledger, and returns a :class:`CommandResult`.
        """
        dataset_id = args["dataset_id"]
        # Registry-side gate (fail-closed for production).
        self.registry.dispatch_training(dataset_id, mode)
        entry = self.registry.inspect(dataset_id)

        # Resolve job id.
        job_id = args.get("job_id") or f"job-{dataset_id}-{int(time.time())}"

        # Build the request payload (never includes the API key).
        request_payload: dict[str, Any] = {
            "job_id": job_id,
            "dataset_id": dataset_id,
            "manifest_uri": entry.manifest_uri,
            "data_uri": entry.data_uri,
            "mode": mode.value,
            "gpu_type": self.config.default_gpu_type,
            "model_type": args.get("model_type", "lightgbm"),
            "hyperparams": args.get("hyperparams", {}),
        }

        # Budget cap in cents.
        budget_cents = int(budget_usd * 100)

        # Dispatch (mock in tests; the default fn raises).
        dispatch_result = self._dispatch_fn(
            job_id=job_id,
            request_payload=request_payload,
            budget_cents=budget_cents,
        )
        runpod_job_id = dispatch_result.get("runpod_job_id")
        status = dispatch_result.get("status", "dispatched")
        cost_cents = dispatch_result.get("cost_cents", 0)
        duration = dispatch_result.get("duration_seconds", 0.0)

        # Record in the job ledger.
        self.ledger.create_row(outbox_id=job_id, dataset_id=dataset_id)
        self.ledger.update_state(
            ledger_id=job_id,
            new_state=JobLedgerState.DISPATCHED,
            runpod_job_id=runpod_job_id,
        )
        if cost_cents or duration:
            self.ledger.record_cost(job_id, cost_cents, duration)

        return CommandResult(
            command=command,
            success=True,
            message=(
                f"{mode.value} training job dispatched: job_id={job_id}, "
                f"runpod_job_id={runpod_job_id}, status={status}, "
                f"budget_cap=${budget_usd:.2f}"
            ),
            job_id=job_id,
        )

    def cmd_train_status(self, args: dict) -> CommandResult:
        """Show dispatch, queue, worker, callback, artifact verification,
        and final eligibility states for a job.

        Local operation — queries the job ledger.

        Required args: ``job_id``.
        """
        job_id = args["job_id"]
        trace = self.ledger.trace(job_id)
        if trace is None:
            raise ValueError(f"unknown job_id: {job_id!r} (not in ledger)")
        report = format_status_report(trace)
        return CommandResult(
            command="train status",
            success=True,
            message=report,
            job_id=job_id,
        )

    def cmd_train_verify(self, args: dict) -> CommandResult:
        """Verify an artifact for a completed job.

        Local operation — artifact verification. Checks that the job has
        reached the ``artifact_verified`` terminal state (or records the
        artifact if one is provided).

        Required args: ``job_id``.
        Optional args: ``artifact_id`` (if provided, records it in the
          ledger and transitions to ``artifact_verified``).
        """
        job_id = args["job_id"]
        rec = self.ledger.get(job_id)
        if rec is None:
            raise ValueError(f"unknown job_id: {job_id!r} (not in ledger)")
        artifact_id = args.get("artifact_id")
        if artifact_id:
            self.ledger.record_artifact(job_id, artifact_id)
            rec = self.ledger.get(job_id)
            assert rec is not None
        if rec.state == JobLedgerState.ARTIFACT_VERIFIED:
            return CommandResult(
                command="train verify",
                success=True,
                message=(f"artifact verified for job {job_id!r}: artifact_id={rec.artifact_id}"),
                job_id=job_id,
            )
        return CommandResult(
            command="train verify",
            success=False,
            message=(f"job {job_id!r} is not artifact-verified (current state: {rec.state.value})"),
            job_id=job_id,
            error=f"not verified (state={rec.state.value})",
        )

    def cmd_train_cost(self, args: dict) -> CommandResult:
        """Show a cost estimate for a job.

        Local operation — cost report from the job ledger.

        Required args: ``job_id``.
        """
        job_id = args["job_id"]
        rec = self.ledger.get(job_id)
        if rec is None:
            raise ValueError(f"unknown job_id: {job_id!r} (not in ledger)")
        cost_usd = rec.cost_cents / 100.0
        return CommandResult(
            command="train cost",
            success=True,
            message=(
                f"cost report for job {job_id!r}: "
                f"${cost_usd:.2f} ({rec.cost_cents} cents), "
                f"duration={rec.duration_seconds}s, retries={rec.retries}"
            ),
            job_id=job_id,
        )

    # --- handler table ---------------------------------------------------

    _HANDLERS: dict[str, Callable[..., CommandResult]] = {}  # filled below

    def _resolve_api_key(self) -> str | None:
        """Read the RunPod API key from the configured env var.

        Returns None if the env var is not set. The key is never stored
        on the CLI or logged.
        """
        return os.environ.get(self.config.runpod_api_key_env)


# Wire up the handler table (after the class is defined).
RunPodCLI._HANDLERS = {
    "dataset register": RunPodCLI.cmd_dataset_register,
    "dataset upload": RunPodCLI.cmd_dataset_upload,
    "train canary": RunPodCLI.cmd_train_canary,
    "train production": RunPodCLI.cmd_train_production,
    "train status": RunPodCLI.cmd_train_status,
    "train verify": RunPodCLI.cmd_train_verify,
    "train cost": RunPodCLI.cmd_train_cost,
}
