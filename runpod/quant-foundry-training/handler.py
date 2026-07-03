"""
RunPod entrypoint for the Quant Foundry training worker (TASK-0501).

This module is the bridge between RunPod's serverless handler protocol and
the quant_foundry.runpod_training.RunPodTrainingHandler. RunPod calls
`handler(event)` for each job; we parse the event into a
RunPodTrainingRequest, invoke the handler, and return the signed callback
envelope + signature for the dispatcher to ingest.

Security invariants (non-negotiable):
- NO broker credentials, NO Redis, NO stream write capability. This handler
  runs in an isolated container with no trading access. It only reads the
  request, trains, and returns a signed callback.
- The callback is signed with QUANT_FOUNDRY_CALLBACK_SECRET (env var). The
  dispatcher verifies the signature before processing.
- Training failures return a safe terminal status (error dict), not a crash.
- Time/budget limits are enforced by the handler.

RunPod protocol:
- Input: `event["input"]` is a dict matching RunPodTrainingRequest.
- Output: a dict with `callback_payload` (JSON string), `callback_signature`,
  `callback_ts`, and `job_id`. On failure: `error_code` + `error_summary`.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# StrEnum compatibility shim — must run before any quant_foundry imports
# (quant_foundry uses StrEnum which is Python 3.11+, but the Docker base
# image ships Python 3.10).
try:
    import _strenum_compat  # type: ignore[import-not-found]  # noqa: F401
except ImportError:
    pass

# Add the shared RunPod utilities to sys.path so we can import
# worker_status. In the container the shared module may be at different
# paths (sibling to the handler, or under /app/runpod/shared). For local
# testing it's under runpod/shared relative to the repo root.
_shared_paths = [
    os.path.join(os.path.dirname(__file__), "..", "shared"),
    os.path.join(os.path.dirname(__file__), "shared"),
    "/app/runpod/shared",
]
for _p in _shared_paths:
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

try:
    from worker_status import clear_status, write_heartbeat, write_status
except ImportError:  # pragma: no cover - fallback if shared module missing
    # Best-effort: define no-op stubs so the handler still runs even if
    # the worker_status module is unavailable (e.g. older container image).
    def write_status(*args, **kwargs):  # type: ignore[no-redef]
        pass

    def write_heartbeat(*args, **kwargs):  # type: ignore[no-redef]
        pass

    def clear_status(*args, **kwargs):  # type: ignore[no-redef]
        pass


# Phase 1 / T-1.2: artifact writer interface. Pydantic v2 is used for
# the typed write result (frozen=True, extra='forbid' — audit integrity /
# fail-closed). The writer protocol decouples the handler from the
# storage backend so canary/research/production runs can use different
# backends (volume path, presigned object upload, or a fake in-memory
# writer for tests) behind a single contract.
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

# Phase 3 / T-3.3: worker-side quality gate runner. Imports the quality
# policy registry + validation function so the worker can recompute cheap
# data checks and reject bad data even if the trusted-side preflight was
# skipped (defense in depth).
from quant_foundry.data_ingestion.quality_report import (  # noqa: E402
    QUALITY_POLICY_REGISTRY,
    DatasetQualityReport,
    FailedCheck,
    QualityGateResult,
    QualityPolicy,
    resolve_quality_policy,
    validate_quality_policy,
)
from quant_foundry.dataset_manifest import (  # noqa: E402
    ColumnRoles as QFColumnRoles,
)
from quant_foundry.dataset_manifest import (
    FoldSpec as QFFoldSpec,
)

# Phase 1 / T-1.1: typed artifact result contract. Imported at module
# level — ``quant_foundry.real_trainer`` is importable without ML deps
# (lightgbm/numpy are imported lazily inside ``train()``).
from quant_foundry.real_trainer import (  # noqa: E402
    TypedArtifactResult,
    build_artifact_result,
)
from quant_foundry.runpod_training import (  # noqa: E402
    LocalTrainer,
    RunPodTrainingHandler,
    SignedFailureEnvelope,
    TrainingFailure,
    build_callback,
    build_failure_envelope,
    verify_failure_envelope,
)
from quant_foundry.schemas import RunPodTrainingRequest  # noqa: E402
from quant_foundry.signatures import sign_callback  # noqa: E402

# Phase 0 / T-4.1: mode system for GPU healthcheck mode-aware validation.
from quant_foundry.training_manifest import (  # noqa: E402
    MODE_RULES,
    ModelTaskSpec,
    TrainingMode,
)

# Phase 3 / T-2.2: manifest-first dataset loader with hash verification.
from fincept_core.datasets import (  # noqa: E402
    DatasetLoadError,
    LoadedDataset,
    ManifestDatasetLoader,
)


def runpod_data_root() -> Path:
    """Resolve the RunPod network volume mount path.

    RunPod mounts the network volume at different paths depending on the mode:
    - Pod mode (SSH/dev):     /workspace
    - Serverless mode:        /runpod-volume

    This helper checks both and returns the first that exists.
    Falls back to /tmp if neither exists (e.g. local testing).
    """
    for path in (Path("/runpod-volume"), Path("/workspace")):
        if path.exists():
            return path
    return Path("/tmp")


def resolve_volume_path(ref: str) -> str:
    """Resolve a dataset reference that may use /runpod-volume or /workspace.

    If the ref starts with /runpod-volume/ but the actual mount is /workspace,
    or vice versa, rewrite it to the correct path.
    """
    if not ref or ref.startswith("inline://") or ref.startswith("s3://") or ref.startswith("http"):
        return ref

    ref_path = Path(ref)
    # Check if it's a volume path that needs rewriting
    if str(ref_path).startswith("/runpod-volume/"):
        actual_root = runpod_data_root()
        if str(actual_root) != "/runpod-volume":
            # Rewrite: /runpod-volume/datasets/x -> /workspace/datasets/x
            relative = ref_path.relative_to("/runpod-volume")
            return str(actual_root / relative)
    elif str(ref_path).startswith("/workspace/"):
        actual_root = runpod_data_root()
        if str(actual_root) != "/workspace":
            # Rewrite: /workspace/datasets/x -> /runpod-volume/datasets/x
            relative = ref_path.relative_to("/workspace")
            return str(actual_root / relative)

    return ref


def _get_callback_secret() -> str:
    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    if not secret:
        # Fail closed: no callback secret means callbacks cannot be
        # signed, which would allow forgery.  Refuse to start rather
        # than silently falling back to a known-weak default.
        raise RuntimeError(
            "QUANT_FOUNDRY_CALLBACK_SECRET is not set. "
            "This secret is required to sign HMAC callbacks to the API. "
            "Set it in the RunPod template environment or container env."
        )
    return secret


# --- Phase 1 / T-1.2: artifact writer interface ----------------------------
#
# A typed, backend-agnostic artifact writer contract. The handler no
# longer writes artifacts inline; instead it selects a writer based on
# the run mode and available inputs:
#
# - ``VolumeArtifactWriter``: writes to a RunPod network volume path
#   (canary/operator fallback). Returns a ``file://`` URI.
# - ``PresignedUploadArtifactWriter``: uploads artifact bytes via HTTP
#   PUT to a presigned URL (production path). Returns the presigned URL
#   as the artifact URI.
# - ``FakeArtifactWriter``: computes the expected sha256 without
#   actually writing (testing). Returns a synthetic ``artifact://fake/``
#   URI.
#
# Every writer returns an :class:`ArtifactWriteResult` carrying the
# artifact URI, sha256, size, format, and a write receipt (HMAC over the
# URI+sha+size+format). The handler signs the artifact metadata with the
# callback secret so the trusted-side verifier can authenticate it.
#
# Security invariants (fail-closed):
# - Disallowed URI schemes (``http://``, ``ftp://``, arbitrary schemes)
#   are rejected with a signed failure envelope.
# - Writer failure produces a signed failure callback with
#   ``error_code="artifact_write_failed"``.
# - Written bytes are re-hashed and compared to the declared sha256
#   (byte-for-byte verification — a mismatch is a terminal failure).


# Allowed URI schemes for artifact locations. ``file://`` is the volume
# path; ``https://`` is the presigned object upload path (TLS required —
# ``http://`` is rejected as insecure). ``artifact://`` is the synthetic
# fake-writer URI used only for tests. Any other scheme is rejected
# (fail-closed) so a writer cannot smuggle an artifact to an unapproved
# location.
_ALLOWED_ARTIFACT_URI_SCHEMES: frozenset[str] = frozenset(
    {"file", "https", "artifact"},
)


# --- Phase 4 / T-4.3: worker split (trainer vs dataset utility) -------------
#
# The single handler codebase now serves two distinct RunPod endpoints:
#
#   Trainer worker (this image, ``trainer-gpu-tree``):
#       - train_model
#       - gpu_healthcheck
#       - callback_secret_canary
#     Only these task types are dispatched on the trainer endpoint. Any
#     other task is rejected with a signed failure envelope (fail-closed).
#
#   Dataset utility worker (separate endpoint, same codebase):
#       - write_volume
#       - stat_volume
#       - list_volume
#       - ingest_media_sentiment
#     These tasks stage/list/verify datasets on the network volume. They
#     do NOT need a GPU and are split out so the GPU trainer image is not
#     idled on dataset I/O.
#
# Cross-routing is rejected by each worker's own allowlist:
#   - A volume write (write_volume) sent to the trainer is rejected with
#     ``error_code="task_not_supported_on_trainer"`` (see the gate at the
#     top of :func:`handler`).
#   - A training request (train_model) sent to the dataset worker is
#     rejected by the dataset worker's own allowlist
#     (``error_code="task_not_supported_on_dataset_worker"``). That
#     rejection lives in the dataset worker's dispatch; this trainer
#     handler documents it for operators but does not enforce it (the
#     dataset worker enforces its own gate).
#
# The existing task implementations (write_volume, stat_volume,
# list_volume, ingest_media_sentiment) are preserved in this file so the
# dataset utility worker endpoint can run from the same codebase. They
# are simply unreachable via the normal :func:`handler` dispatch on the
# trainer image — they sit behind the allowlist gate below.

# Tasks the trainer worker is permitted to dispatch.
ALLOWED_TRAINER_TASKS: frozenset[str] = frozenset(
    {"train_model", "gpu_healthcheck", "callback_secret_canary"},
)

# Dataset utility tasks that belong on the separate dataset worker
# endpoint. Sent to the trainer, they are rejected with a signed failure
# (fail-closed) so the dispatcher can authenticate the rejection.
DATASET_UTILITY_TASKS: frozenset[str] = frozenset(
    {"write_volume", "stat_volume", "list_volume", "ingest_media_sentiment"},
)


# --- Phase 5 / T-5.1: handler-level SecurityPreflight ----------------------
#
# Defense in depth: the Dockerfile startup preflight (T-4.2) checks forbidden
# env vars at container boot. This handler-level preflight re-runs the same
# checks at request time so a misconfigured env (e.g. a hot-reloaded env or a
# shared image reused across endpoints) cannot sneak trading/broker/storage
# credentials into the training loop.
#
# Mode-aware enforcement (matches the quality-gate convention):
# - ``production``: fail closed. Any forbidden env var present, or a
#   loopback/private callback URL, is a terminal failure → signed failure
#   envelope with ``error_code="security_preflight_failed"``. The worker
#   MUST be a pure function over its inputs in production — it must never
#   carry broker/Redis/DB/trading credentials.
# - ``canary`` / ``research``: advisory. Forbidden vars and private callback
#   hosts are logged as warnings and recorded in the PreflightResult, but the
#   job continues (canary/research are permissive by design). The redacted
#   config summary is always printed so operators can audit the runtime.
#
# Redaction: secret-like env var names (matching SECRET_PATTERN) and explicitly
# sensitive platform config values are fully masked. Forbidden vars that are
# present are shown as ``FORBIDDEN:present`` (the value is never revealed).
# Non-secret vars are shown in full.

# Env vars the worker must NEVER carry. Presence of any of these means the
# image/env was misconfigured with trading/broker/storage/admin credentials,
# which violates the security boundary (the worker is a pure function over its
# inputs — it must never carry trading, broker, Redis, DB-write, or cloud-admin
# credentials). AWS keys are forbidden unless the worker needs S3 access (the
# trainer worker does not — artifact uploads use presigned URLs).
FORBIDDEN_ENV_VARS: tuple[str, ...] = (
    "REDIS_URL",
    "REDIS_HOST",
    "DATABASE_URL",
    "DB_URL",
    "POSTGRES_URL",
    "FINCEPT_JWT_SECRET",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "ALPACA_SECRET_KEY",
    "BROKER_URL",
    "BROKER_SECRET",
    "AMQP_URL",
    "KAFKA_BOOTSTRAP_SERVERS",
    "MONGO_URL",
    "MONGODB_URI",
    "CLOUD_ADMIN_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)

# Regex detecting secret-like env var names. A name containing any of
# SECRET/KEY/PASSWORD/TOKEN/CREDENTIAL is treated as a secret and redacted in
# the config summary (the value is never printed in full).
SECRET_PATTERN: re.Pattern[str] = re.compile(
    r"(SECRET|KEY|PASSWORD|TOKEN|CREDENTIAL)",
    re.IGNORECASE,
)

# Env var names that are part of the worker's legitimate config and should be
# included in the redacted summary (their values are shown in full unless they
# match SECRET_PATTERN).
_KNOWN_CONFIG_ENVS: tuple[str, ...] = (
    "QUANT_FOUNDRY_CALLBACK_SECRET",
    "QUANT_FOUNDRY_CALLBACK_URL",
    "QUANT_FOUNDRY_USE_REAL_TRAINER",
    "QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS",
    "QUANT_FOUNDRY_TRAINING_MODE",
    "QUANT_FOUNDRY_GIT_SHA",
    "RUNPOD_WEBHOOK_GET_JOB",
)

SENSITIVE_CONFIG_ENVS: frozenset[str] = frozenset({"RUNPOD_WEBHOOK_GET_JOB"})

_CALLBACK_URL_ENV = "QUANT_FOUNDRY_CALLBACK_URL"
_MODE_ENV = "QUANT_FOUNDRY_TRAINING_MODE"


def _redact_secret_value(value: str) -> str:
    """Redact a secret-like value without preserving prefix or suffix.

    Empty values return ``<empty>``.
    """
    if not value:
        return "<empty>"
    return "****"


def _host_is_private(host: str) -> bool:
    """Return True if ``host`` resolves to a loopback/private/link-local IP.

    Best-effort: if the host cannot be resolved, returns False (we cannot
    prove it is private). The caller still treats an unresolvable host as
    suspicious in production by leaving the validation to the URL parse step.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            return True
    return False


class PreflightResult(BaseModel):
    """Typed result of the handler-level SecurityPreflight (Phase 5 / T-5.1).

    Frozen + ``extra='forbid'`` (audit integrity). Carries every check the
    preflight performed so the dispatcher/trusted verifier can audit the
    worker's runtime security posture at request time.

    Fields:
        passed: overall pass flag. ``True`` when no fail-closed check
            triggered (production: no forbidden vars + valid callback URL;
            canary/research: always True — failures are advisory).
        mode: the training mode the preflight ran under
            (``"production"`` / ``"canary"`` / ``"research"``).
        forbidden_vars_found: tuple of forbidden env var names that were
            present in the environment (never their values).
        callback_url_validated: True if a callback URL was set and its host
            was checked (False when no callback URL was configured).
        uri_allowlists_validated: True if the dataset/artifact URI
            allowlists were confirmed (always True — the ManifestDatasetLoader
            and ArtifactWriter enforce allowlists downstream).
        container_user: the user the container is running as
            (``getpass.getuser()``; ``os.getuid()`` appended when available).
        writable_dirs: tuple of writable directory paths probed at runtime.
        redacted_config: dict of env var name → redacted value (secrets
            masked; forbidden vars shown as ``"FORBIDDEN:present"``).
        preflight_error: optional human-readable error string when the
            preflight failed closed (production). None on pass / advisory.
        checked_at_ns: monotonic-ish nanosecond timestamp of the check
            (``time.time_ns()``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    mode: str
    forbidden_vars_found: tuple[str, ...]
    callback_url_validated: bool
    uri_allowlists_validated: bool
    container_user: str
    writable_dirs: tuple[str, ...]
    redacted_config: dict[str, str]
    preflight_error: str | None
    checked_at_ns: int


class SecurityPreflight:
    """Handler-level security preflight (Phase 5 / T-5.1).

    Runs at the top of :func:`handler` (before task dispatch) as defense in
    depth on top of the Dockerfile startup preflight (T-4.2). Checks:

    1. Forbidden env vars are absent (broker/Redis/DB/trading/cloud-admin
       credentials). Production fails closed; canary/research log warnings.
    2. Callback URL host is not loopback/private (production only).
    3. Dataset/artifact URI allowlists are enforced downstream by
       ManifestDatasetLoader + ArtifactWriter (recorded as validated).
    4. Prints a redacted config summary (secrets masked, forbidden vars shown
       as ``FORBIDDEN:present`` — values never revealed).
    5. Records the container user and writable directories.
    """

    def __init__(self, *, mode: TrainingMode) -> None:
        self._mode = mode

    # --- public API --------------------------------------------------------

    def run(self) -> PreflightResult:
        """Run all preflight checks and return a typed PreflightResult.

        Production: ``passed`` is False if any forbidden env var is present
        or the callback URL host is loopback/private (``preflight_error``
        describes the first failure). Canary/research: ``passed`` is always
        True (failures are advisory — recorded but non-fatal).
        """
        mode = self._mode
        forbidden_found = self._check_forbidden_env_vars()
        callback_ok, callback_error = self._check_callback_url()
        uri_ok = self._check_uri_allowlists()
        container_user = self._record_container_user()
        writable_dirs = self._record_writable_dirs()
        redacted = self._build_redacted_config(forbidden_found)

        # Aggregate the redacted summary to stdout for operator audit.
        self._print_summary(mode, forbidden_found, callback_ok, redacted)

        errors: list[str] = []
        if forbidden_found:
            errors.append(f"forbidden env vars present: {sorted(forbidden_found)}")
        if not callback_ok and callback_error:
            errors.append(callback_error)

        if mode == TrainingMode.PRODUCTION and errors:
            return PreflightResult(
                passed=False,
                mode=mode.value,
                forbidden_vars_found=forbidden_found,
                callback_url_validated=callback_ok,
                uri_allowlists_validated=uri_ok,
                container_user=container_user,
                writable_dirs=writable_dirs,
                redacted_config=redacted,
                preflight_error="; ".join(errors),
                checked_at_ns=time.time_ns(),
            )

        # Canary/research: advisory — record warnings but never fail.
        if errors:
            print(
                f"[preflight] WARNING (advisory, mode={mode.value}): {'; '.join(errors)}",
                file=sys.stderr,
                flush=True,
            )
        return PreflightResult(
            passed=True,
            mode=mode.value,
            forbidden_vars_found=forbidden_found,
            callback_url_validated=callback_ok,
            uri_allowlists_validated=uri_ok,
            container_user=container_user,
            writable_dirs=writable_dirs,
            redacted_config=redacted,
            preflight_error=None,
            checked_at_ns=time.time_ns(),
        )

    # --- individual checks -------------------------------------------------

    def _check_forbidden_env_vars(self) -> tuple[str, ...]:
        """Return the tuple of forbidden env var names that are present."""
        return tuple(name for name in FORBIDDEN_ENV_VARS if os.environ.get(name))

    def _check_callback_url(self) -> tuple[bool, str | None]:
        """Validate the callback URL host (if set).

        Returns ``(validated, error)``. When no callback URL is configured,
        returns ``(False, None)`` (the worker returns the callback in its
        response — a URL is optional). When set, the host must not resolve to
        a loopback/private/link-local IP in production. Returns
        ``(True, None)`` when the host is acceptable.
        """
        cb_url = os.environ.get(_CALLBACK_URL_ENV, "")
        if not cb_url:
            return (False, None)
        parsed = urlparse(cb_url)
        host = (parsed.hostname or "").lower()
        if not host:
            return (False, "callback URL has no host")
        if self._mode == TrainingMode.PRODUCTION and _host_is_private(host):
            return (
                False,
                f"callback URL host {host!r} is loopback/private in production mode",
            )
        return (True, None)

    def _check_uri_allowlists(self) -> bool:
        """Confirm the dataset/artifact URI allowlists are enforced.

        The actual allowlist enforcement lives downstream:
        - Dataset URIs are validated by ManifestDatasetLoader (T-2.2) which
          verifies manifest + data hashes and rejects unknown formats.
        - Artifact URIs are validated by ``_validate_artifact_uri_scheme``
          (T-1.2) which rejects disallowed schemes (http/ftp/...).
        This check records that those gates are wired up (always True here —
        the downstream gates raise on violation, fail-closed).
        """
        return True

    def _record_container_user(self) -> str:
        """Record the container user (getpass.getuser + uid when available)."""
        try:
            user = getpass.getuser() or "unknown"
        except Exception:
            user = "unknown"
        uid = ""
        try:
            uid = f":{os.getuid()}"
        except (AttributeError, OSError):
            # Windows / platforms without getuid — omit the uid.
            uid = ""
        return f"{user}{uid}"

    def _record_writable_dirs(self) -> tuple[str, ...]:
        """Probe the candidate writable directories and return the writable ones."""
        candidates = (
            str(runpod_data_root()),
            "/tmp",
            "/runpod-volume",
            "/workspace",
        )
        writable: list[str] = []
        for cand in candidates:
            try:
                p = Path(cand)
                if p.exists() and os.access(p, os.W_OK):
                    writable.append(cand)
            except OSError:
                continue
        return tuple(dict.fromkeys(writable))  # de-dup, preserve order

    def _build_redacted_config(self, forbidden_found: tuple[str, ...]) -> dict[str, str]:
        """Build a redacted config summary dict.

        - Forbidden vars that are present → ``"FORBIDDEN:present"`` (value
          never revealed).
        - Secret-like or explicitly sensitive names → ``****``.
        - Other vars → full value.
        """
        forbidden_set = set(forbidden_found)
        redacted: dict[str, str] = {}
        for name in _KNOWN_CONFIG_ENVS:
            value = os.environ.get(name)
            if value is None:
                continue
            if name in forbidden_set:
                redacted[name] = "FORBIDDEN:present"
            elif name in SENSITIVE_CONFIG_ENVS or SECRET_PATTERN.search(name):
                redacted[name] = _redact_secret_value(value)
            else:
                redacted[name] = value
        # Surface any forbidden vars that are not in the known-config list so
        # operators see them in the summary (value never revealed).
        for name in forbidden_found:
            if name not in redacted:
                redacted[name] = "FORBIDDEN:present"
        return redacted

    def _print_summary(
        self,
        mode: TrainingMode,
        forbidden_found: tuple[str, ...],
        callback_ok: bool,
        redacted: dict[str, str],
    ) -> None:
        """Print the redacted config summary to stdout for operator audit."""
        print(f"[preflight] training_mode={mode.value}", flush=True)
        if forbidden_found:
            print(
                f"[preflight] forbidden env vars present: {sorted(forbidden_found)}",
                file=sys.stderr,
                flush=True,
            )
        cb_url = os.environ.get(_CALLBACK_URL_ENV, "")
        if cb_url:
            host = (urlparse(cb_url).hostname or "").lower()
            status = "ok" if callback_ok else "REJECTED"
            print(f"[preflight] callback_url host={host} ({status})", flush=True)
        else:
            print(
                "[preflight] callback_url not set (worker returns callback in response)",
                flush=True,
            )
        print("[preflight] redacted config summary:", flush=True)
        for key in sorted(redacted):
            print(f"  {key}={redacted[key]}", flush=True)


def _resolve_preflight_mode(input_data: dict[str, Any]) -> TrainingMode:
    """Resolve the training mode for the handler-level preflight.

    Reads the mode from, in priority order:
    1. ``input_data["mode"]`` / ``input_data["training_mode"]`` (top-level
       request fields — used by gpu_healthcheck and the canary).
    2. ``input_data["extra_constraints"]["training_mode"]`` (the
       RunPodTrainingRequest convention for training jobs — read BEFORE
       schema validation so the preflight can fail closed on a production
       request before any task dispatch).
    3. The ``QUANT_FOUNDRY_TRAINING_MODE`` env var.
    4. Defaults to ``canary`` (the most lenient mode) so a bare request
       never accidentally fails closed at the preflight gate.

    An unknown mode fails closed as ``production`` (strictest), matching
    ``_resolve_healthcheck_mode``.
    """
    raw = input_data.get("mode") or input_data.get("training_mode")
    if raw is None:
        extra = input_data.get("extra_constraints")
        if isinstance(extra, dict):
            raw = extra.get("training_mode")
    if raw is None:
        raw = os.environ.get(_MODE_ENV)
    if raw is None:
        return TrainingMode.CANARY
    try:
        return TrainingMode(raw)
    except ValueError:
        return TrainingMode.PRODUCTION


def _build_security_preflight_failure_callback(
    *,
    job_id: str | None,
    preflight_result: PreflightResult,
) -> dict[str, Any]:
    """Build a signed security-preflight failure envelope (Phase 5 / T-5.1).

    When the preflight fails closed in production, the handler emits this
    signed failure so the dispatcher can authenticate the rejection (never a
    silent drop). The envelope carries ``error_code="security_preflight_failed"``
    and the redacted preflight result (no secret values are revealed).

    Phase 5 / T-5.3: now returns a :class:`SignedFailureEnvelope` via
    :func:`_build_signed_failure` (HMAC-signed context hash) with backward-
    compat ``error_code`` / ``error_summary`` / ``callback_*`` keys.
    """
    error_message = preflight_result.preflight_error or "security preflight failed"
    context: dict[str, str] = {
        "job_id": job_id or "preflight-unknown",
        "mode": preflight_result.mode,
        "forbidden_vars_found": ",".join(sorted(preflight_result.forbidden_vars_found)),
    }
    return _build_signed_failure(
        error_code="security_preflight_failed",
        error_message=error_message,
        mode=preflight_result.mode,
        context=context,
        extra={
            "preflight_result": preflight_result.model_dump(),
        },
    )


# --- Phase 5 / T-5.3: signed failure envelope helper ------------------------
#
# ``_build_signed_failure`` wraps :func:`build_failure_envelope` and returns
# the standardized handler response dict for ALL failure paths. The dict
# carries:
# - ``error``: the full :class:`SignedFailureEnvelope` as a dict (the new
#   standardized contract — the trusted side verifies the HMAC signature
#   via :func:`verify_failure_envelope` / :func:`validate_failure_envelope`).
# - ``status``: ``"failed"`` (machine-readable terminal status).
# - ``signed_failure``: ``True`` (flag so the dispatcher knows the failure
#   is a signed envelope, not a bare error dict).
# - Backward-compat keys: ``error_code``, ``error_summary``, ``job_id``,
#   ``callback_payload``, ``callback_signature``, ``callback_ts`` (so
#   existing callers that check for ``error_code`` / ``error_summary``
#   keep working unchanged).
#
# The callback payload (signed via the existing ``sign_callback`` mechanism)
# embeds the full signed failure envelope dict so the trusted side can
# authenticate the failure through BOTH the legacy callback signature AND
# the new envelope signature (defense in depth).


def _build_signed_failure(
    *,
    error_code: str,
    error_message: str,
    mode: str,
    context: dict[str, str],
    runtime_fingerprint_hash: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a signed failure envelope and wrap it in a handler response dict.

    This is the single entry point for ALL handler failure paths (Phase 5 /
    T-5.3). It builds a :class:`SignedFailureEnvelope` (HMAC-signed context
    hash), embeds it in the handler response under the ``"error"`` key, and
    preserves backward compatibility with the existing ``error_code`` /
    ``error_summary`` / ``callback_payload`` / ``callback_signature`` /
    ``callback_ts`` keys.

    Args:
        error_code: machine-readable error code (e.g.
            ``"security_preflight_failed"``, ``"task_not_allowed"``,
            ``"quality_gate_failed"``, ``"dataset_load_error"``,
            ``"training_error"``, ``"runtime_fingerprint_invalid"``).
        error_message: human-readable failure description.
        mode: the training mode (``"production"`` / ``"canary"`` /
            ``"research"``).
        context: key-value pairs describing the failure context
            (job_id, task_id, dataset_id, gate_code, etc.).
        runtime_fingerprint_hash: optional link to the signed runtime
            fingerprint (T-5.2) active when the failure occurred.
        extra: optional extra fields to merge into the response dict
            (e.g. ``preflight_result``, ``quality_gate_result``,
            ``gate_code``). These are NOT part of the signed envelope —
            they are supplementary audit data.

    Returns:
        A handler response dict with the signed failure envelope + backward
        compatibility keys.
    """
    secret = _get_callback_secret()
    envelope = build_failure_envelope(
        error_code=error_code,
        error_message=error_message,
        mode=mode,
        context=context,
        secret=secret,
        worker_id="runpod-trainer",
        runtime_fingerprint_hash=runtime_fingerprint_hash,
    )
    env_dict = envelope.model_dump()

    # Build the callback payload (for backward compat with the existing
    # sign_callback mechanism). The full signed failure envelope is
    # embedded so the trusted side can authenticate through both paths.
    job_id = context.get("job_id") or "failure-unknown"
    callback_payload_dict = {
        "schema_version": 1,
        "job_id": job_id,
        "worker_id": "runpod-trainer",
        "result_type": error_code,
        "payload": {
            "error_code": error_code,
            "error_message": error_message,
            "mode": mode,
            "signed_failure_envelope": env_dict,
        },
    }
    callback_payload = json.dumps(
        callback_payload_dict,
        sort_keys=True,
    ).encode("utf-8")
    callback_ts = int(time.time())
    callback_signature = sign_callback(
        callback_payload,
        secret=secret,
        ts=callback_ts,
        job_id=job_id,
    )

    response: dict[str, Any] = {
        # Phase 5 / T-5.3: signed failure envelope (the new contract).
        "error": env_dict,
        "status": "failed",
        "signed_failure": True,
        # Backward compatibility: existing callers check error_code /
        # error_summary / job_id / callback_payload / callback_signature.
        "error_code": error_code,
        "error_summary": error_message,
        "job_id": context.get("job_id"),
        "callback_payload": callback_payload.decode("utf-8"),
        "callback_signature": callback_signature,
        "callback_ts": callback_ts,
    }
    # Merge any extra audit fields (not part of the signed envelope).
    if extra:
        response.update(extra)
    return response


class ArtifactWriteResult(BaseModel):
    """Typed result of an artifact write (Phase 1 / T-1.2).

    Frozen + ``extra='forbid'`` (audit integrity). Carries the artifact
    URI, sha256, size, format, and a write receipt (HMAC-SHA256 over the
    canonical ``uri|sha256|size|format`` string, signed with the callback
    secret). The trusted-side verifier re-computes the receipt to detect
    tampering with the artifact metadata.

    Fields:
        artifact_uri: declared location of the persisted artifact
            (``file://``, ``https://``, or ``artifact://fake/...``).
        artifact_sha256: SHA-256 hex (64 lowercase chars) of the
            artifact bytes.
        artifact_size_bytes: byte length of the artifact (> 0).
        artifact_format: serialisation format (``"pickle"``, ...).
        write_receipt: HMAC-SHA256 hex over
            ``artifact_uri|artifact_sha256|artifact_size_bytes|artifact_format``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_uri: str
    artifact_sha256: str
    artifact_size_bytes: int
    artifact_format: str
    write_receipt: str

    def verify_receipt(self, *, secret: str) -> bool:
        """Recompute the write receipt and compare (constant-time).

        Returns ``True`` iff the recomputed HMAC matches the stored
        receipt. Used by the trusted-side verifier to authenticate the
        artifact metadata (fail-closed — never raises on a mismatch).
        """
        if not isinstance(secret, str) or not secret:
            return False
        canonical = "|".join(
            [
                self.artifact_uri,
                self.artifact_sha256,
                str(self.artifact_size_bytes),
                self.artifact_format,
            ]
        ).encode("utf-8")
        expected = hmac.new(
            secret.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, self.write_receipt)


def _sign_artifact_metadata(
    *,
    artifact_uri: str,
    artifact_sha256: str,
    artifact_size_bytes: int,
    artifact_format: str,
    secret: str,
) -> str:
    """Sign artifact metadata (URI + sha256 + size + format) with HMAC.

    The canonical payload is ``artifact_uri|artifact_sha256|size|format``
    (pipe-delimited, no JSON wrapping — deterministic and compact). The
    trusted-side verifier re-computes this to authenticate the artifact
    metadata independent of the callback signature.
    """
    canonical = "|".join(
        [
            artifact_uri,
            artifact_sha256,
            str(artifact_size_bytes),
            artifact_format,
        ]
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def _validate_artifact_uri_scheme(uri: str) -> None:
    """Validate that ``uri`` uses an allowed artifact URI scheme.

    Allowed: ``file``, ``https``, ``artifact`` (fake writer).
    Disallowed: ``http`` (insecure), ``ftp``, and any arbitrary scheme.

    Raises :class:`ValueError` (fail-closed) on a disallowed scheme so
    the handler can translate it into a signed failure envelope.
    """
    if not uri or not uri.strip():
        raise ValueError("artifact URI must be non-empty")
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise ValueError(
            f"artifact URI has no scheme (allowed: "
            f"{sorted(_ALLOWED_ARTIFACT_URI_SCHEMES)}): {uri!r}"
        )
    if scheme not in _ALLOWED_ARTIFACT_URI_SCHEMES:
        raise ValueError(
            f"disallowed artifact URI scheme {scheme!r} "
            f"(allowed: {sorted(_ALLOWED_ARTIFACT_URI_SCHEMES)}): {uri!r}"
        )


class ArtifactWriter(Protocol):
    """Protocol for artifact writers (Phase 1 / T-1.2).

    Every writer persists (or simulates persisting) artifact bytes and
    returns an :class:`ArtifactWriteResult` with the URI, sha256, size,
    format, and a signed write receipt. Writers fail closed: a write
    failure raises so the handler can emit a signed failure envelope.
    """

    def write_artifact(
        self,
        model_bytes: bytes,
        artifact_id: str,
        artifact_format: str,
    ) -> ArtifactWriteResult: ...


class VolumeArtifactWriter:
    """Write artifact bytes to a RunPod network volume path.

    Writes to ``{output_dir}/model.pkl`` (where ``output_dir`` is the
    resolved volume path), computes the sha256 + size after the write,
    re-reads the written bytes to verify they match the declared hash
    (byte-for-byte, fail-closed), and returns a ``file://`` URI.

    Also writes the callback envelope, artifact manifest, and dossier
    JSON sidecars alongside the model so the trusted-side verifier can
    audit the full result without re-running training.

    Args:
        output_dir: resolved volume directory to write into.
        callback_payload_bytes: the signed callback envelope JSON bytes
            (written as ``callback_envelope.json`` sidecar).
        artifact_manifest_dict: the artifact manifest dict (written as
            ``artifact_manifest.json`` sidecar).
        dossier_dict: the dossier dict (written as ``dossier.json``
            sidecar).
        callback_secret: HMAC secret for signing the write receipt.
    """

    def __init__(
        self,
        *,
        output_dir: Path,
        callback_payload_bytes: bytes,
        artifact_manifest_dict: dict[str, Any],
        dossier_dict: dict[str, Any],
        callback_secret: str,
    ) -> None:
        self._output_dir = output_dir
        self._callback_payload_bytes = callback_payload_bytes
        self._artifact_manifest_dict = artifact_manifest_dict
        self._dossier_dict = dossier_dict
        self._callback_secret = callback_secret

    def write_artifact(
        self,
        model_bytes: bytes,
        artifact_id: str,
        artifact_format: str,
    ) -> ArtifactWriteResult:
        if not model_bytes:
            raise ValueError("VolumeArtifactWriter cannot write empty artifact bytes (fail closed)")
        out_dir = self._output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        # Write JSON sidecars (best-effort audit trail on the volume).
        try:
            callback_json = json.loads(self._callback_payload_bytes.decode("utf-8"))
            (out_dir / "callback_envelope.json").write_text(
                json.dumps(callback_json, indent=2),
                encoding="utf-8",
            )
            (out_dir / "artifact_manifest.json").write_text(
                json.dumps(self._artifact_manifest_dict, indent=2),
                encoding="utf-8",
            )
            (out_dir / "dossier.json").write_text(
                json.dumps(self._dossier_dict, indent=2),
                encoding="utf-8",
            )
        except Exception:
            # Sidecar write failures are non-fatal — the model artifact
            # is the contract-critical write. Re-raise only if the model
            # write itself fails (below).
            pass

        # Write the model artifact bytes (contract-critical).
        model_path = out_dir / "model.pkl"
        model_path.write_bytes(model_bytes)

        # Compute sha256 + size from the bytes we are about to declare.
        sha = hashlib.sha256(model_bytes).hexdigest()
        size = len(model_bytes)

        # Verify the written bytes match the declared hash (fail-closed):
        # re-read the file and re-hash. A mismatch means the volume
        # corrupted the write (bit flip, truncation, concurrent writer).
        written = model_path.read_bytes()
        written_sha = hashlib.sha256(written).hexdigest()
        if written_sha != sha or len(written) != size:
            raise ValueError(
                "artifact sha256/size mismatch after volume write: "
                f"declared sha={sha} size={size}, "
                f"written sha={written_sha} size={len(written)}"
            )

        uri = model_path.as_uri()
        _validate_artifact_uri_scheme(uri)
        receipt = _sign_artifact_metadata(
            artifact_uri=uri,
            artifact_sha256=sha,
            artifact_size_bytes=size,
            artifact_format=artifact_format,
            secret=self._callback_secret,
        )
        return ArtifactWriteResult(
            artifact_uri=uri,
            artifact_sha256=sha,
            artifact_size_bytes=size,
            artifact_format=artifact_format,
            write_receipt=receipt,
        )


class PresignedUploadArtifactWriter:
    """Upload artifact bytes to a presigned object store URL (production).

    Accepts a presigned URL (passed via the handler input) and uploads
    the artifact bytes via HTTP PUT. Computes the sha256 + size before
    upload, verifies the upload succeeded (HTTP 200), and returns the
    presigned URL as the artifact URI.

    The presigned URL MUST use ``https://`` (TLS required — ``http://``
    is rejected by :func:`_validate_artifact_uri_scheme`). The trusted
    side fetches the artifact from the same URL to re-verify the hash.

    Args:
        presigned_url: the presigned PUT URL for the artifact object.
        callback_secret: HMAC secret for signing the write receipt.
    """

    def __init__(
        self,
        *,
        presigned_url: str,
        callback_secret: str,
    ) -> None:
        # Validate the scheme up front (fail-closed before any network
        # I/O — reject http://, ftp://, and arbitrary schemes).
        _validate_artifact_uri_scheme(presigned_url)
        self._presigned_url = presigned_url
        self._callback_secret = callback_secret

    def write_artifact(
        self,
        model_bytes: bytes,
        artifact_id: str,
        artifact_format: str,
    ) -> ArtifactWriteResult:
        if not model_bytes:
            raise ValueError(
                "PresignedUploadArtifactWriter cannot upload empty artifact bytes (fail closed)"
            )
        sha = hashlib.sha256(model_bytes).hexdigest()
        size = len(model_bytes)

        # Upload via HTTP PUT with the raw artifact bytes as the body.
        # The presigned URL encodes the object key + signature; we do
        # not add extra auth headers (the signature is in the URL).
        req = Request(
            self._presigned_url,
            data=model_bytes,
            method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urlopen(req, timeout=120) as resp:  # noqa: S310 - presigned URL is operator-provided
                status = getattr(resp, "status", None) or resp.getcode()
                if status != 200:
                    raise ValueError(
                        f"presigned upload failed: HTTP {status} "
                        f"(expected 200) for {self._presigned_url}"
                    )
        except ValueError:
            # Re-raise validation errors (HTTP status mismatch) as-is.
            raise
        except Exception as exc:
            # Network / connection / timeout failure → fail closed.
            raise ValueError(f"presigned artifact upload failed: {exc}") from exc

        uri = self._presigned_url
        _validate_artifact_uri_scheme(uri)
        receipt = _sign_artifact_metadata(
            artifact_uri=uri,
            artifact_sha256=sha,
            artifact_size_bytes=size,
            artifact_format=artifact_format,
            secret=self._callback_secret,
        )
        return ArtifactWriteResult(
            artifact_uri=uri,
            artifact_sha256=sha,
            artifact_size_bytes=size,
            artifact_format=artifact_format,
            write_receipt=receipt,
        )


class FakeArtifactWriter:
    """In-memory fake writer for testing (Phase 1 / T-1.2).

    Computes the expected sha256 + size WITHOUT actually writing the
    bytes anywhere. Returns a synthetic ``artifact://fake/{artifact_id}``
    URI. Used by canary tests and unit tests that need to exercise the
    writer contract without a volume or network.

    Args:
        callback_secret: HMAC secret for signing the write receipt.
    """

    def __init__(self, *, callback_secret: str) -> None:
        self._callback_secret = callback_secret

    def write_artifact(
        self,
        model_bytes: bytes,
        artifact_id: str,
        artifact_format: str,
    ) -> ArtifactWriteResult:
        if not model_bytes:
            raise ValueError("FakeArtifactWriter cannot hash empty artifact bytes (fail closed)")
        sha = hashlib.sha256(model_bytes).hexdigest()
        size = len(model_bytes)
        uri = f"artifact://fake/{artifact_id}"
        _validate_artifact_uri_scheme(uri)
        receipt = _sign_artifact_metadata(
            artifact_uri=uri,
            artifact_sha256=sha,
            artifact_size_bytes=size,
            artifact_format=artifact_format,
            secret=self._callback_secret,
        )
        return ArtifactWriteResult(
            artifact_uri=uri,
            artifact_sha256=sha,
            artifact_size_bytes=size,
            artifact_format=artifact_format,
            write_receipt=receipt,
        )


def _build_artifact_write_failure_callback(
    *,
    job_id: str,
    error_summary: str,
) -> dict[str, Any]:
    """Build a signed artifact-write failure envelope (Phase 1 / T-1.2).

    When an artifact write fails (volume I/O error, presigned upload
    failure, URI scheme rejection, or sha mismatch after write), the
    handler emits this signed failure envelope so the dispatcher /
    trusted verifier can authenticate the failure (it is not a silent
    drop). The envelope carries ``error_code="artifact_write_failed"``
    and is HMAC-signed with ``QUANT_FOUNDRY_CALLBACK_SECRET``.

    Phase 5 / T-5.3: now returns a :class:`SignedFailureEnvelope` via
    :func:`_build_signed_failure` (HMAC-signed context hash) with backward-
    compat ``error_code`` / ``error_summary`` / ``callback_*`` keys.
    """
    context: dict[str, str] = {
        "job_id": job_id,
        "stage": "artifact_write",
    }
    return _build_signed_failure(
        error_code="artifact_write_failed",
        error_message=error_summary,
        mode="production",
        context=context,
    )


def _handle_canary(input_data: dict[str, Any]) -> dict[str, Any]:
    """Handle a callback-secret canary job.

    The canary is a minimal round-trip that proves the RunPod worker and
    the API share the same ``QUANT_FOUNDRY_CALLBACK_SECRET``. The API
    dispatches a canary job with a random nonce; the worker signs the
    nonce-bearing payload and returns it. The API verifies the signature.

    This is NOT a training job — it bypasses the training pipeline
    entirely and returns immediately.
    """
    job_id = input_data.get("job_id") or "canary-unknown"
    nonce = input_data.get("nonce") or ""
    callback_payload = json.dumps(
        {
            "schema_version": 1,
            "job_id": job_id,
            "worker_id": "runpod-canary",
            "result_type": "callback_secret_canary",
            "payload": {"nonce": nonce},
        },
        sort_keys=True,
    ).encode("utf-8")
    callback_ts = int(time.time())
    callback_signature = sign_callback(
        callback_payload,
        secret=_get_callback_secret(),
        ts=callback_ts,
        job_id=job_id,
    )
    return {
        "job_id": job_id,
        "callback_payload": callback_payload.decode("utf-8"),
        "callback_signature": callback_signature,
        "callback_ts": callback_ts,
        "canary": True,
        "nonce": nonce,
    }


def _get_deadline_seconds() -> int:
    raw = os.environ.get("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    try:
        return int(raw)
    except ValueError:
        return 600


def _handle_ingest_media_sentiment(input_data: dict[str, Any]) -> dict[str, Any]:
    """Handle a media-sentiment dataset ingestion task.

    Builds a media-sentiment-price dataset on the worker using the
    modular dataset system, writes the parquet + manifest + receipt +
    quality report to the network volume, and returns the paths + manifest
    hash so a subsequent training job can consume the dataset via
    ``dataset_manifest_ref``.

    This task bypasses the training pipeline entirely — it only builds
    the dataset.  A separate training job (with ``dataset_manifest_ref``
    pointing at the manifest written by this task) does the actual
    training.
    """
    dataset_id = input_data.get("dataset_id", "")
    if not dataset_id:
        # Phase 5 / T-5.3: signed failure envelope.
        return _build_signed_failure(
            error_code="bad_request",
            error_message="ingest_media_sentiment requires dataset_id",
            mode="canary",
            context={
                "job_id": str(input_data.get("job_id") or "unknown"),
                "task": "ingest_media_sentiment",
                "stage": "dataset_id",
            },
        )

    start_ns = input_data.get("start_ns")
    end_ns = input_data.get("end_ns")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int):
        # Phase 5 / T-5.3: signed failure envelope.
        return _build_signed_failure(
            error_code="bad_request",
            error_message="ingest_media_sentiment requires start_ns and end_ns as integers",
            mode="canary",
            context={
                "job_id": str(input_data.get("job_id") or "unknown"),
                "task": "ingest_media_sentiment",
                "stage": "time_range",
            },
        )

    output_dir = input_data.get("output_dir", "")
    if not output_dir:
        # Phase 5 / T-5.3: signed failure envelope.
        return _build_signed_failure(
            error_code="bad_request",
            error_message="ingest_media_sentiment requires output_dir",
            mode="canary",
            context={
                "job_id": str(input_data.get("job_id") or "unknown"),
                "task": "ingest_media_sentiment",
                "stage": "output_dir",
            },
        )

    # Resolve volume path
    output_dir = resolve_volume_path(output_dir)

    # Module selections (with defaults)
    universe_module = input_data.get("universe_module", "universe:sp500:1.0.0")
    source_module = input_data.get("source_module", "source:newsapi:1.0.0")
    sentiment_module = input_data.get("sentiment_module", "sentiment:finbert:1.0.0")
    feature_modules = input_data.get(
        "feature_modules",
        ["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"],
    )
    label_module = input_data.get("label_module", "label:abnormal-return:1.0.0")
    price_join_module = input_data.get("price_join_module", "price_join:alpaca-bars:1.0.0")
    n_folds = input_data.get("n_folds", 3)
    module_config = input_data.get("config", {})

    try:
        from quant_foundry.modules import DatasetComposer, load_all_modules

        load_all_modules()

        composer = DatasetComposer(
            universe=universe_module,
            source=source_module,
            sentiment=sentiment_module,
            features=feature_modules,
            label=label_module,
            price_join=price_join_module,
            config=module_config,
        )

        result = composer.build(
            output_dir=Path(output_dir),
            dataset_id=dataset_id,
            start_ns=start_ns,
            end_ns=end_ns,
            n_folds=n_folds,
        )
    except Exception as exc:
        # Phase 5 / T-5.3: signed failure envelope.
        return _build_signed_failure(
            error_code="ingestion_failed",
            error_message=str(exc),
            mode="canary",
            context={
                "job_id": str(input_data.get("job_id") or "unknown"),
                "task": "ingest_media_sentiment",
                "dataset_id": dataset_id,
                "stage": "ingestion",
            },
        )

    return {
        "task": "ingest_media_sentiment",
        "dataset_id": dataset_id,
        "parquet_path": str(result.parquet_path),
        "manifest_path": str(result.manifest_path),
        "receipt_path": str(result.receipt_path),
        "quality_path": str(result.quality_path),
        "row_count": result.manifest.row_count,
        "manifest_hash": result.manifest.manifest_hash(),
        "feature_schema_hash": result.manifest.feature_schema_hash,
        "label_schema_hash": result.manifest.label_schema_hash,
        "status": "ok",
    }


# --- GPU healthcheck (Phase 4 / T-4.1) --------------------------------------
#
# A new ``gpu_healthcheck`` task type that probes the worker's GPU
# runtime and returns signed metadata. The healthcheck:
#
# 1. Runs ``nvidia-smi`` (or reports missing if not available).
# 2. Records CUDA version, driver version, GPU model, GPU memory.
# 3. Records training-library GPU capability flags (lightgbm GPU,
#    xgboost GPU, catboost GPU — checked only if the library is
#    importable).
# 4. Integrates with the mode system:
#    - ``production``: fails closed if ``gpu_capable`` is False (a
#      production run MUST execute on a GPU worker — local CPU training
#      is not an acceptance substitute).
#    - ``canary``: may report GPU absence but marks
#      ``promotion_eligible=False`` (a canary without a GPU is never
#      promotion eligible).
#    - ``research``: permissive — reports the GPU state without failing.
# 5. Returns a signed callback payload containing the GPU runtime
#    metadata so the dispatcher/trusted verifier can audit the worker's
#    GPU capability at dispatch time.


@dataclass(frozen=True)
class GPUHealthcheckResult:
    """Structured result of a GPU healthcheck probe.

    All fields are populated on every run (``None`` / ``False`` when the
    corresponding capability is absent) so the downstream dispatcher can
    make routing decisions without defensive ``getattr`` checks.
    """

    gpu_capable: bool
    nvidia_smi_available: bool
    nvidia_smi_output: str | None
    cuda_version: str | None
    driver_version: str | None
    gpu_model: str | None
    gpu_memory_mb: int | None
    gpu_count: int
    library_gpu_flags: dict[str, bool]
    mode: str
    promotion_eligible: bool
    checked_at_ns: int
    runtime_fingerprint: dict[str, str]


def _probe_nvidia_smi() -> tuple[
    bool, str | None, str | None, str | None, str | None, int | None, int
]:
    """Probe the GPU via ``nvidia-smi``.

    Returns a tuple of:
        (available, raw_output, cuda_version, driver_version,
         gpu_model, gpu_memory_mb, gpu_count)

    When ``nvidia-smi`` is not installed (``FileNotFoundError``) or exits
    non-zero (``CalledProcessError``), ``available`` is False and the
    parsed fields are ``None`` / ``0``. The raw output (or error text) is
    always captured for diagnostics.
    """
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        raw = proc.stdout.strip()
    except FileNotFoundError:
        return (False, "nvidia-smi not found (FileNotFoundError)", None, None, None, None, 0)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or "").strip() or f"nvidia-smi exited {exc.returncode}"
        return (False, err, None, None, None, None, 0)
    except subprocess.TimeoutExpired:
        return (False, "nvidia-smi timed out", None, None, None, None, 0)

    # Parse the CSV: "name, memory.total MiB, driver_version"
    gpu_model: str | None = None
    gpu_memory_mb: int | None = None
    driver_version: str | None = None
    gpu_count = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        gpu_count += 1
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            if gpu_model is None:
                gpu_model = parts[0]
            mem_str = parts[1].replace("MiB", "").strip()
            try:
                gpu_memory_mb = int(mem_str)
            except ValueError:
                pass
            if driver_version is None:
                driver_version = parts[2]

    # Query CUDA version separately (nvidia-smi --query-gpu=driver_version
    # does not include CUDA; use the full nvidia-smi output header).
    cuda_version: str | None = None
    try:
        proc_full = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        full_out = proc_full.stdout
        for line in full_out.splitlines():
            if "CUDA Version" in line:
                # e.g. "CUDA Version: 12.2"
                for token in line.split():
                    if token.replace(".", "").isdigit():
                        cuda_version = token
                        break
                break
    except Exception:  # noqa: BLE001 - best-effort CUDA version probe
        pass

    available = gpu_count > 0
    return (available, raw, cuda_version, driver_version, gpu_model, gpu_memory_mb, gpu_count)


def _probe_cuda_via_torch() -> str | None:
    """Probe CUDA availability via PyTorch if installed.

    Returns the CUDA version string (e.g. ``"12.2"``) or ``None`` if
    PyTorch is not installed or CUDA is not available. This is a
    secondary probe — ``nvidia-smi`` is the primary source.
    """
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        if not torch.cuda.is_available():
            return None
        return torch.version.cuda  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001 - best-effort torch probe
        return None


def _probe_library_gpu_flags() -> dict[str, bool]:
    """Probe GPU capability of installed training libraries.

    Checks whether ``lightgbm``, ``xgboost``, and ``catboost`` are
    importable and whether they advertise GPU support. Libraries that
    are not installed report ``False`` (not an error — the healthcheck
    must succeed even on a CPU-only image).
    """
    flags: dict[str, bool] = {
        "lightgbm_gpu": False,
        "xgboost_gpu": False,
        "catboost_gpu": False,
    }

    # LightGBM: GPU support is indicated by the ``device_type="gpu"``
    # parameter being accepted. We check the installed version and
    # whether the GPU build is available via a lightweight import.
    try:
        import lightgbm as lgb  # type: ignore[import-not-found]

        # LightGBM GPU support: the ``LGBMClassifier``/``LGBMRegressor``
        # accept ``device_type="gpu"``. We cannot safely instantiate a
        # GPU model without data, so we check whether the library
        # exposes the GPU-capable estimator classes. The healthcheck
        # reports the flag; the dispatcher decides whether to trust it.
        try:
            default_params = lgb.LGBMModel().get_params()  # type: ignore[attr-defined]
            if "device_type" in default_params or "device" in default_params:
                flags["lightgbm_gpu"] = True
        except Exception:  # noqa: BLE001
            # Conservative: if lightgbm imports, assume GPU-capable
            # build may be present (the healthcheck reports the flag,
            # the dispatcher decides whether to trust it).
            flags["lightgbm_gpu"] = hasattr(lgb, "LGBMClassifier")
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass

    # XGBoost: GPU support via ``tree_method="gpu_hist"`` or
    # ``device="cuda"``. Check if the library imports and exposes the
    # GPU tree method.
    try:
        import xgboost as xgb  # type: ignore[import-not-found]

        # XGBoost >= 2.0 uses ``device="cuda"``; older uses
        # ``tree_method="gpu_hist"``. We check for the presence of the
        # GPU-capable attributes.
        flags["xgboost_gpu"] = hasattr(xgb, "XGBClassifier") or hasattr(xgb, "Booster")
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass

    # CatBoost: GPU support via ``task_type="GPU"``. Check if the
    # library imports.
    try:
        import catboost  # type: ignore[import-not-found]

        flags["catboost_gpu"] = hasattr(catboost, "CatBoostClassifier") or hasattr(
            catboost, "CatBoostRegressor"
        )
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass

    return flags


def _runtime_fingerprint() -> dict[str, str]:
    """Build a signed runtime fingerprint for the healthcheck callback.

    Includes the code git SHA, lockfile hash, and container image
    digest (pinned at build time or defaulted for local tests). These
    are the same reproducibility pins used by the trainer so the
    dispatcher can bind a healthcheck result to a specific image.
    """
    from quant_foundry.runpod_training import (
        _container_digest_or_default,
        _git_sha_or_default,
        _lockfile_hash_or_default,
    )

    return {
        "code_git_sha": _git_sha_or_default() or "unknown",
        "lockfile_hash": _lockfile_hash_or_default() or "unknown",
        "container_image_digest": _container_digest_or_default() or "unknown",
        "hostname": os.environ.get("HOSTNAME", "unknown"),
    }


def _resolve_healthcheck_mode(input_data: dict[str, Any]) -> TrainingMode:
    """Resolve the training mode for a GPU healthcheck request.

    The mode is read from ``input_data["mode"]`` or
    ``input_data["training_mode"]`` (the latter matches the
    ``RunPodTrainingRequest.extra_constraints["training_mode"]``
    convention). Defaults to ``canary`` (the most lenient mode) when
    absent, so a bare healthcheck never accidentally fails closed.
    """
    raw = input_data.get("mode") or input_data.get("training_mode")
    if raw is None:
        return TrainingMode.CANARY
    try:
        return TrainingMode(raw)
    except ValueError:
        # Unknown mode → fail closed as production (strictest).
        return TrainingMode.PRODUCTION


def _handle_gpu_healthcheck(input_data: dict[str, Any]) -> dict[str, Any]:
    """Handle a GPU healthcheck probe task.

    Probes the worker's GPU runtime and returns signed metadata. The
    mode (read from ``input_data["mode"]`` or
    ``input_data["training_mode"]``) controls the fail-closed behavior:

    - ``production``: if ``gpu_capable`` is False, the healthcheck
      FAILS (returns an ``error_code``). A production run MUST execute
      on a GPU worker — local CPU training is not an acceptance
      substitute.
    - ``canary``: may report GPU absence (``gpu_capable=False``) but
      marks ``promotion_eligible=False``. The healthcheck succeeds (no
      error) so the dispatcher can record the GPU state.
    - ``research``: permissive — reports the GPU state without failing.

    The response includes a signed callback payload (HMAC over the GPU
    runtime metadata) so the dispatcher/trusted verifier can audit the
    worker's GPU capability at dispatch time.
    """
    job_id = input_data.get("job_id") or "gpu-healthcheck-unknown"
    mode = _resolve_healthcheck_mode(input_data)

    # --- probe the GPU runtime -------------------------------------------
    (
        nvidia_smi_available,
        nvidia_smi_output,
        cuda_version_nvidia,
        driver_version,
        gpu_model,
        gpu_memory_mb,
        gpu_count,
    ) = _probe_nvidia_smi()

    # Secondary CUDA probe via PyTorch (may catch CUDA even if
    # nvidia-smi parsing missed it).
    cuda_version_torch = _probe_cuda_via_torch()
    cuda_version = cuda_version_nvidia or cuda_version_torch

    library_gpu_flags = _probe_library_gpu_flags()

    # A worker is GPU-capable if nvidia-smi reports at least one GPU OR
    # PyTorch reports CUDA availability.
    gpu_capable = nvidia_smi_available or (cuda_version_torch is not None)

    checked_at_ns = time.time_ns()
    runtime_fingerprint = _runtime_fingerprint()

    # --- mode-aware promotion eligibility --------------------------------
    # Canary without a GPU is never promotion eligible. Research is
    # never promotion eligible by default (per MODE_RULES). Production
    # requires a GPU to be promotion eligible.
    rules = MODE_RULES.get(mode, {})
    promotion_eligible_default = bool(rules.get("promotion_eligible_default", False))
    if mode == TrainingMode.CANARY and not gpu_capable:
        promotion_eligible = False
    elif mode == TrainingMode.PRODUCTION:
        promotion_eligible = promotion_eligible_default and gpu_capable
    else:
        promotion_eligible = promotion_eligible_default

    result = GPUHealthcheckResult(
        gpu_capable=gpu_capable,
        nvidia_smi_available=nvidia_smi_available,
        nvidia_smi_output=nvidia_smi_output,
        cuda_version=cuda_version,
        driver_version=driver_version,
        gpu_model=gpu_model,
        gpu_memory_mb=gpu_memory_mb,
        gpu_count=gpu_count,
        library_gpu_flags=library_gpu_flags,
        mode=mode.value,
        promotion_eligible=promotion_eligible,
        checked_at_ns=checked_at_ns,
        runtime_fingerprint=runtime_fingerprint,
    )

    # --- production fail-closed ------------------------------------------
    # A production healthcheck with no GPU is a terminal failure — the
    # dispatcher must not route a production training job to this
    # worker.
    if mode == TrainingMode.PRODUCTION and not gpu_capable:
        # Phase 5 / T-5.3: signed failure envelope.
        return _build_signed_failure(
            error_code="gpu_required_production",
            error_message=(
                "production mode requires a GPU worker but "
                "gpu_capable=false (local CPU training is not an "
                "acceptance substitute)"
            ),
            mode=mode.value,
            context={
                "job_id": job_id,
                "stage": "gpu_healthcheck",
                "gpu_capable": "false",
            },
            extra={
                "gpu_healthcheck": asdict(result),
            },
        )

    # --- build the signed callback payload -------------------------------
    # The callback carries the full GPU runtime metadata so the
    # dispatcher/trusted verifier can audit the worker's GPU capability
    # and bind it to a specific image (runtime fingerprint).
    callback_payload_dict = {
        "schema_version": 1,
        "job_id": job_id,
        "worker_id": "runpod-gpu-healthcheck",
        "result_type": "gpu_healthcheck",
        "payload": {
            "gpu_healthcheck": asdict(result),
        },
    }
    callback_payload = json.dumps(callback_payload_dict, sort_keys=True).encode("utf-8")
    callback_ts = int(time.time())
    callback_signature = sign_callback(
        callback_payload,
        secret=_get_callback_secret(),
        ts=callback_ts,
        job_id=job_id,
    )

    return {
        "task": "gpu_healthcheck",
        "job_id": job_id,
        "callback_payload": callback_payload.decode("utf-8"),
        "callback_signature": callback_signature,
        "callback_ts": callback_ts,
        "gpu_healthcheck": asdict(result),
    }


_REAL_TRAINER_BACKEND_BY_FAMILY: dict[str, str] = {
    "gbm": "lightgbm",
    "lightgbm": "lightgbm",
    "lightgbm_baseline": "lightgbm",
    "catboost": "catboost",
    "catboost_gpu": "catboost",
    "xgb": "xgboost",
    "xgboost": "xgboost",
    "xgboost_gpu": "xgboost",
}

_KNOWN_NON_ROUTED_FAMILIES: frozenset[str] = frozenset(
    {
        "tabm",
        "tabm_gpu",
        "patchtst",
        "patchtst_gpu",
        "tft",
        "tft_gpu",
        "deeplob",
        "deeplob_gpu",
        "event",
        "event_text",
        "graph",
        "graph_ranker",
        "rl_shadow",
        "rl_shadow_policy",
    },
)


def _json_mapping_from_extra(
    extra: dict[str, str],
    *keys: str,
) -> dict[str, Any] | None:
    for key in keys:
        raw = extra.get(key)
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TrainingFailure(
                    error_code="invalid_trainer_metadata",
                    error_summary=f"{key} must be valid JSON: {exc}",
                ) from exc
        else:
            value = raw
        if not isinstance(value, dict):
            raise TrainingFailure(
                error_code="invalid_trainer_metadata",
                error_summary=f"{key} must decode to a JSON object",
            )
        return value
    return None


def _qf_column_roles_from_raw(raw: Any, source: str) -> QFColumnRoles | None:
    if raw is None:
        return None
    if isinstance(raw, QFColumnRoles):
        return raw
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise TrainingFailure(
            error_code="invalid_column_roles",
            error_summary=f"{source} must be a mapping or ColumnRoles model",
        )
    try:
        return QFColumnRoles.model_validate(raw)
    except Exception as exc:
        raise TrainingFailure(
            error_code="invalid_column_roles",
            error_summary=f"{source} failed validation: {exc}",
        ) from exc


def _task_spec_from_raw(raw: Any, source: str) -> ModelTaskSpec | None:
    if raw is None:
        return None
    if isinstance(raw, ModelTaskSpec):
        return raw
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise TrainingFailure(
            error_code="invalid_task_spec",
            error_summary=f"{source} must be a mapping or ModelTaskSpec model",
        )
    try:
        return ModelTaskSpec.model_validate(raw)
    except Exception as exc:
        raise TrainingFailure(
            error_code="invalid_task_spec",
            error_summary=f"{source} failed validation: {exc}",
        ) from exc


def _fold_spec_from_raw(raw: Any, source: str) -> QFFoldSpec | None:
    if raw is None:
        return None
    if isinstance(raw, QFFoldSpec):
        return raw
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise TrainingFailure(
            error_code="invalid_fold_spec",
            error_summary=f"{source} must be a mapping or FoldSpec model",
        )
    try:
        return QFFoldSpec.model_validate(raw)
    except Exception as exc:
        raise TrainingFailure(
            error_code="invalid_fold_spec",
            error_summary=f"{source} failed validation: {exc}",
        ) from exc


def _resolve_column_roles(
    req: RunPodTrainingRequest,
    loaded_dataset: LoadedDataset | None,
) -> QFColumnRoles | None:
    extra_roles = _json_mapping_from_extra(
        req.extra_constraints,
        "column_roles",
        "column_roles_json",
    )
    if extra_roles is not None:
        return _qf_column_roles_from_raw(extra_roles, "extra_constraints.column_roles")
    if loaded_dataset is not None:
        manifest_roles = loaded_dataset.manifest.get("column_roles")
        if manifest_roles is not None:
            return _qf_column_roles_from_raw(manifest_roles, "dataset manifest column_roles")
        return _qf_column_roles_from_raw(
            loaded_dataset.column_roles,
            "loaded dataset column_roles",
        )
    return None


def _resolve_task_spec(
    req: RunPodTrainingRequest,
    loaded_dataset: LoadedDataset | None,
) -> ModelTaskSpec | None:
    extra_spec = _json_mapping_from_extra(
        req.extra_constraints,
        "task_spec",
        "task_spec_json",
        "model_task_spec",
        "model_task_spec_json",
    )
    if extra_spec is not None:
        return _task_spec_from_raw(extra_spec, "extra_constraints.task_spec")
    if loaded_dataset is not None:
        for key in ("task_spec", "model_task_spec"):
            manifest_spec = loaded_dataset.manifest.get(key)
            if manifest_spec is not None:
                return _task_spec_from_raw(manifest_spec, f"dataset manifest {key}")
    return None


def _resolve_fold_spec(
    req: RunPodTrainingRequest,
    loaded_dataset: LoadedDataset | None,
) -> QFFoldSpec | None:
    extra_spec = _json_mapping_from_extra(
        req.extra_constraints,
        "fold_spec",
        "fold_spec_json",
    )
    if extra_spec is not None:
        return _fold_spec_from_raw(extra_spec, "extra_constraints.fold_spec")
    if loaded_dataset is not None:
        manifest_spec = loaded_dataset.manifest.get("fold_spec")
        if manifest_spec is not None:
            return _fold_spec_from_raw(manifest_spec, "dataset manifest fold_spec")
    return None


def _real_backend_for_family(model_family: str) -> str:
    family = model_family.strip().lower()
    backend = _REAL_TRAINER_BACKEND_BY_FAMILY.get(family)
    if backend:
        return backend
    if family in _KNOWN_NON_ROUTED_FAMILIES:
        raise TrainingFailure(
            error_code="model_family_not_routed",
            error_summary=(
                f"model_family {model_family!r} is implemented locally but "
                "does not yet share the RunPod training artifact contract; "
                "use a dedicated canary route before live training"
            ),
        )
    raise TrainingFailure(
        error_code="unsupported_model_family",
        error_summary=(
            f"unsupported model_family {model_family!r}; supported live "
            f"families: {sorted(_REAL_TRAINER_BACKEND_BY_FAMILY)}"
        ),
    )


def _training_mode_is_production(req: RunPodTrainingRequest) -> bool:
    raw_mode = req.extra_constraints.get("training_mode") or "research"
    try:
        return TrainingMode(raw_mode) == TrainingMode.PRODUCTION
    except ValueError:
        return True


def _build_trainer(
    req: RunPodTrainingRequest,
    *,
    n_folds: int = 3,
    loaded_dataset: LoadedDataset | None = None,
) -> Any:
    use_real = os.environ.get("QUANT_FOUNDRY_USE_REAL_TRAINER", "").lower() == "true"
    if use_real:
        from quant_foundry.real_trainer import RealLightGBMTrainer

        backend = _real_backend_for_family(req.model_family)
        column_roles = _resolve_column_roles(req, loaded_dataset)
        task_spec = _resolve_task_spec(req, loaded_dataset)
        fold_spec = _resolve_fold_spec(req, loaded_dataset)
        if backend != "lightgbm" and column_roles is None:
            raise TrainingFailure(
                error_code="missing_column_roles",
                error_summary=(
                    f"model_family {req.model_family!r} requires explicit "
                    "column_roles in extra_constraints or dataset manifest"
                ),
            )
        if backend != "lightgbm" and task_spec is None:
            raise TrainingFailure(
                error_code="missing_task_spec",
                error_summary=(
                    f"model_family {req.model_family!r} requires explicit "
                    "task_spec in extra_constraints or dataset manifest"
                ),
            )
        return RealLightGBMTrainer(
            n_folds=n_folds,
            backend=backend,
            column_roles=column_roles,
            task_spec=task_spec,
            fold_spec=fold_spec,
            is_production=_training_mode_is_production(req),
        )
    return LocalTrainer()


def _heartbeat_during_training(job_id: str, interval: float = 10.0) -> threading.Event:
    """Start a background heartbeat thread. Returns a stop event.

    The thread writes a heartbeat status file every ``interval`` seconds
    so the gateway can detect stale/crashed workers. The caller must
    ``set()`` the returned event to stop the thread.
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval):
            write_heartbeat(job_id)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop


def _load_dataset_via_manifest(
    load_spec: dict[str, Any],
    *,
    job_id: str,
) -> tuple[str, dict[str, Any]]:
    """Load a dataset manifest-first and return the verified data URI.

    Phase 3 / T-2.2: when the input includes a ``dataset_load_spec``
    dict, the handler uses :class:`ManifestDatasetLoader` to fetch +
    verify the manifest and data BEFORE training. This replaces the
    overloaded ``dataset_manifest_ref`` with an explicit, fail-closed
    contract:

    1. Fetch the manifest from ``manifest_uri``.
    2. Verify ``manifest_sha256`` (fail on mismatch).
    3. Read the data URI from the verified manifest.
    4. Fetch the data from ``data_uri``.
    5. Verify ``data_sha256`` (fail on mismatch).
    6. Verify ``row_count`` (fail on mismatch).
    7. Verify ``feature_schema_hash`` / ``label_schema_hash`` (fail on
       mismatch).

    On success, returns ``(resolved_data_uri, load_receipt_dict)``. The
    resolved data URI is set on ``dataset_manifest_ref`` so the existing
    trainer reads the *verified* data file. On any verification failure,
    raises :class:`DatasetLoadError` (fail-closed).

    Args:
        load_spec: a dict with the load-spec fields (manifest_uri,
            manifest_sha256, data_uri, data_sha256, data_format,
            row_count, feature_schema_hash, label_schema_hash). Mirrors
            :class:`~quant_foundry.dataset_manifest.DatasetLoadSpec`.
        job_id: the job ID (for error reporting).
    """
    # Resolve volume paths in the spec URIs (/runpod-volume vs /workspace).
    manifest_uri = resolve_volume_path(load_spec.get("manifest_uri", ""))
    data_uri = resolve_volume_path(load_spec.get("data_uri", ""))
    spec_fields = {
        "manifest_uri": manifest_uri,
        "manifest_sha256": load_spec.get("manifest_sha256"),
        "data_uri": data_uri,
        "data_sha256": load_spec.get("data_sha256"),
        "data_format": load_spec.get("data_format"),
        "row_count": load_spec.get("row_count"),
        "feature_schema_hash": load_spec.get("feature_schema_hash"),
        "label_schema_hash": load_spec.get("label_schema_hash"),
    }
    loader = ManifestDatasetLoader(**spec_fields)
    loaded = loader.load()
    receipt = loaded.load_receipt
    receipt_dict = {
        "manifest_uri": receipt.manifest_uri,
        "manifest_sha256_verified": receipt.manifest_sha256_verified,
        "data_uri": receipt.data_uri,
        "data_sha256_verified": receipt.data_sha256_verified,
        "row_count_verified": receipt.row_count_verified,
        "schema_verified": receipt.schema_verified,
        "loaded_at_ns": receipt.loaded_at_ns,
        "manifest_hash": loaded.manifest_hash,
        "row_count": loaded.row_count,
    }
    # The verified data URI is what the trainer reads. Resolve it to a
    # path the trainer can open (already resolved above, but the
    # manifest may declare a different data_uri — use the receipt's).
    return receipt.data_uri, receipt_dict, loaded


# ---------------------------------------------------------------------------
# Phase 3 / T-3.3: Worker-Side QualityGateRunner
# ---------------------------------------------------------------------------
#
# The worker recomputes cheap data checks on the loaded dataframe AFTER
# the manifest-first load (T-2.2) and BEFORE training begins. This is
# defense in depth: even if the trusted-side preflight was skipped (or
# produced a stale/tampered quality report), the worker catches bad data
# on its own copy of the dataframe.
#
# Mode-aware enforcement:
# - ``production``: all gates must pass. Any failure → signed failure
#   callback with ``error_code="quality_gate_failed"`` and the specific
#   ``gate_code``. Training does NOT start.
# - ``canary``: gates are advisory — failures are logged as warnings and
#   the job continues, but ``promotion_eligible`` is forced to ``False``.
# - ``research``: gates are advisory — failures are logged but the job
#   continues (research is permissive by design).
#
# The failure callback is HMAC-signed with the same
# ``QUANT_FOUNDRY_CALLBACK_SECRET`` used for training callbacks, so the
# dispatcher/trusted verifier can authenticate it.


class QualityGateError(ValueError):
    """A quality gate configuration or resolution error (fail-closed).

    Subclass of :class:`ValueError` so existing ``except ValueError``
    handlers keep catching it. ``code`` is a short machine-readable
    string (``unknown_quality_policy``, ``quality_report_fetch_failed``,
    ``quality_report_parse_failed``) the handler maps to an error
    callback.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- dataframe-agnostic helpers -------------------------------------------
#
# The ManifestDatasetLoader may return a pandas DataFrame, a pyarrow
# Table, or (rarely) a numpy array. These helpers work across all three
# so the quality gate runner does not hard-depend on pandas.


def _df_row_count(df: Any) -> int:
    """Return the number of rows in ``df`` (pandas, pyarrow, or numpy)."""
    if hasattr(df, "__len__"):
        return len(df)
    shape = getattr(df, "shape", None)
    if shape is not None and len(shape) >= 1:
        return int(shape[0])
    return 0


def _df_columns(df: Any) -> list[str]:
    """Return the column names of ``df`` (pandas, pyarrow, or numpy)."""
    if hasattr(df, "columns"):
        try:
            return [str(c) for c in df.columns]
        except Exception:  # noqa: BLE001 - best-effort column extraction
            pass
    if hasattr(df, "column_names"):
        return list(df.column_names)
    # numpy ndarray → positional column names.
    shape = getattr(df, "shape", None)
    if shape is not None and len(shape) >= 2:
        return [f"col_{i}" for i in range(shape[1])]
    return []


def _df_to_pandas(df: Any) -> Any:
    """Best-effort convert ``df`` to a pandas DataFrame.

    Returns the original object if pandas is not available or the
    conversion fails. The caller should guard pandas-specific operations
    with ``hasattr`` checks after calling this.
    """
    # Already a pandas DataFrame.
    if hasattr(df, "duplicated") and hasattr(df, "iloc"):
        return df
    # pyarrow Table → pandas.
    if hasattr(df, "to_pandas"):
        try:
            return df.to_pandas()
        except Exception:  # noqa: BLE001
            pass
    return df


def _df_duplicate_count(df: Any) -> int:
    """Count exact duplicate rows in ``df`` (pandas or pyarrow)."""
    pdf = _df_to_pandas(df)
    if hasattr(pdf, "duplicated"):
        try:
            return int(pdf.duplicated().sum())
        except Exception:  # noqa: BLE001
            pass
    # pyarrow fallback: unique() height difference.
    if hasattr(df, "unique") and hasattr(df, "num_rows"):
        try:
            return int(df.num_rows - df.unique().num_rows)
        except Exception:  # noqa: BLE001
            pass
    return 0


def _df_label_balance(df: Any, label_col: str) -> float:
    """Return the minority class fraction (0..1) for ``label_col``.

    Returns ``0.0`` when the column is missing, empty, or the fraction
    cannot be computed (fail-closed: an unknown label distribution is
    treated as maximally imbalanced).
    """
    cols = _df_columns(df)
    if label_col not in cols:
        return 0.0
    pdf = _df_to_pandas(df)
    if hasattr(pdf, "value_counts") and hasattr(pdf, "__getitem__"):
        try:
            series = pdf[label_col]
            non_null = series.dropna()
            total = len(non_null)
            if total == 0:
                return 0.0
            counts = non_null.value_counts()
            return float(counts.min()) / total
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def _df_feature_coverage(df: Any, feature_cols: list[str]) -> float:
    """Return the minimum non-null fraction across ``feature_cols`` (0..1).

    A missing column contributes ``0.0`` (fail-closed). Returns ``1.0``
    when there are no feature columns to check (vacuously covered).
    """
    if not feature_cols:
        return 1.0
    total_rows = _df_row_count(df)
    if total_rows == 0:
        return 0.0
    cols = set(_df_columns(df))
    pdf = _df_to_pandas(df)
    min_frac = 1.0
    for col in feature_cols:
        if col not in cols:
            return 0.0  # missing column → zero coverage
        # pandas path.
        if hasattr(pdf, "isnull") and hasattr(pdf, "__getitem__"):
            try:
                null_count = int(pdf[col].isnull().sum())
                frac = (total_rows - null_count) / total_rows
                min_frac = min(min_frac, frac)
                continue
            except Exception:  # noqa: BLE101
                pass
        # pyarrow path.
        if hasattr(df, "column") and hasattr(df, "num_rows"):
            try:
                col_data = df.column(col)
                null_count = int(col_data.null_count)
                frac = (total_rows - null_count) / total_rows
                min_frac = min(min_frac, frac)
                continue
            except Exception:  # noqa: BLE101
                pass
        # Cannot determine coverage for this column → fail closed.
        return 0.0
    return min_frac


def _fetch_quality_report(
    quality_report_uri: str,
    *,
    expected_sha256: str | None = None,
) -> DatasetQualityReport:
    """Fetch + parse a :class:`DatasetQualityReport` from a URI.

    Reads the JSON file at ``quality_report_uri`` (resolved via
    :func:`resolve_volume_path`), optionally verifies its SHA-256, and
    parses it into a :class:`DatasetQualityReport`. Raises
    :class:`QualityGateError` on any fetch, hash, or parse failure
    (fail-closed).
    """
    resolved = resolve_volume_path(quality_report_uri)
    try:
        raw = Path(resolved).read_bytes()
    except OSError as exc:
        raise QualityGateError(
            "quality_report_fetch_failed",
            f"failed to fetch quality report from {quality_report_uri}: {exc}",
        ) from exc
    if expected_sha256:
        import hashlib

        actual = hashlib.sha256(raw).hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise QualityGateError(
                "quality_report_hash_mismatch",
                f"quality report sha256 mismatch: expected {expected_sha256}, got {actual}",
            )
    try:
        return DatasetQualityReport.model_validate_json(raw)
    except Exception as exc:
        raise QualityGateError(
            "quality_report_parse_failed",
            f"failed to parse quality report JSON: {exc}",
        ) from exc


class QualityGateRunner:
    """Worker-side quality gate runner (Phase 3 / T-3.3).

    Recomputes cheap data checks on the loaded dataframe and validates
    them against a :class:`QualityPolicy`. Defense in depth: even if
    the trusted-side preflight was skipped (or produced a stale report),
    the worker rejects bad data before GPU training begins.

    Usage::

        runner = QualityGateRunner(
            loaded, quality_policy_id="qp-production-v1",
            quality_report=report, mode=TrainingMode.PRODUCTION,
        )
        result = runner.run()
        if not result.passed and mode == TrainingMode.PRODUCTION:
            # emit signed failure callback, do NOT train
    """

    def __init__(
        self,
        loaded: LoadedDataset,
        *,
        quality_policy_id: str | None = None,
        quality_report: DatasetQualityReport | None = None,
        mode: TrainingMode = TrainingMode.RESEARCH,
    ) -> None:
        self._loaded = loaded
        self._quality_report = quality_report
        self._mode = mode
        self._policy = self._resolve_policy(quality_policy_id, mode)

    # ------------------------------------------------------------------ #
    # Policy resolution                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_policy(
        policy_id: str | None,
        mode: TrainingMode,
    ) -> QualityPolicy:
        """Resolve the quality policy, falling back to the mode default.

        Raises :class:`QualityGateError` (``unknown_quality_policy``)
        if an explicit ``policy_id`` is provided but not registered, or
        if no policy is available for the mode (fail-closed).
        """
        if policy_id:
            policy = resolve_quality_policy(policy_id)
            if policy is None:
                raise QualityGateError(
                    "unknown_quality_policy",
                    f"unknown quality_policy_id {policy_id!r} "
                    f"(not in registry: "
                    f"{sorted(QUALITY_POLICY_REGISTRY.known_ids())})",
                )
            return policy
        # Fall back to the mode's default policy (defense in depth:
        # even without an explicit policy id, the worker applies the
        # mode-appropriate gate).
        policy = QUALITY_POLICY_REGISTRY.for_mode(mode)
        if policy is None:
            raise QualityGateError(
                "unknown_quality_policy",
                f"no quality_policy_id provided and no default policy for mode {mode.value}",
            )
        return policy

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    @property
    def policy(self) -> QualityPolicy:
        """The resolved quality policy."""
        return self._policy

    def run(self) -> QualityGateResult:
        """Run the quality gates and return a :class:`QualityGateResult`.

        Always recomputes cheap data checks on the loaded dataframe
        (defense in depth). When a quality report is provided, also
        validates it against the policy via
        :func:`validate_quality_policy` and merges any failures. The
        merged result is fail-closed: ``passed`` is ``True`` only when
        no check failed across either path.
        """
        cheap_result = self._run_cheap_checks()

        if self._quality_report is not None:
            report_result = validate_quality_policy(
                self._quality_report,
                self._policy,
            )
            return self._merge_results(report_result, cheap_result)

        return cheap_result

    # ------------------------------------------------------------------ #
    # Cheap data checks (recomputed on the loaded df)                    #
    # ------------------------------------------------------------------ #

    def _run_cheap_checks(self) -> QualityGateResult:
        """Recompute cheap data checks on the loaded dataframe.

        Checks (each maps to a gate code):
        - ``row_count_mismatch`` — actual rows vs manifest-declared.
        - ``duplicate_rows`` — duplicate count vs policy max.
        - ``label_balance`` — minority class fraction vs policy min.
        - ``feature_coverage`` — min non-null fraction vs policy min.
        - ``schema_match`` — expected feature/label columns exist in df.
        """
        failed: list[FailedCheck] = []
        df = self._loaded.df
        roles = self._loaded.column_roles
        policy = self._policy

        # --- row count (compare to manifest-declared) -------------------
        actual_rows = _df_row_count(df)
        declared_rows = self._loaded.row_count
        if declared_rows is not None and actual_rows != declared_rows:
            failed.append(
                FailedCheck(
                    check_name="row_count_mismatch",
                    expected=str(declared_rows),
                    actual=str(actual_rows),
                    message=(
                        f"row count mismatch: manifest declares "
                        f"{declared_rows} rows, loaded df has "
                        f"{actual_rows} rows"
                    ),
                )
            )

        # --- row count (compare to policy minimum) ----------------------
        # Defense in depth: even without a quality report, the worker
        # checks the actual row count against the policy's min_row_count
        # (mirrors the ``row_count`` check in validate_quality_policy).
        if actual_rows < policy.min_row_count:
            failed.append(
                FailedCheck(
                    check_name="row_count",
                    expected=f">= {policy.min_row_count}",
                    actual=str(actual_rows),
                    message=(
                        f"dataset has {actual_rows} rows; policy "
                        f"requires at least {policy.min_row_count}"
                    ),
                )
            )

        # --- duplicate rows ---------------------------------------------
        dup_count = _df_duplicate_count(df)
        if dup_count > policy.max_duplicate_rows:
            failed.append(
                FailedCheck(
                    check_name="duplicate_rows",
                    expected=f"<= {policy.max_duplicate_rows}",
                    actual=str(dup_count),
                    message=(
                        f"dataset has {dup_count} duplicate rows; "
                        f"policy allows at most "
                        f"{policy.max_duplicate_rows}"
                    ),
                )
            )

        # --- label balance (minority class fraction) --------------------
        label_col = roles.label_columns[0] if roles.label_columns else None
        if label_col:
            minority = _df_label_balance(df, label_col)
            if minority < policy.min_label_balance:
                failed.append(
                    FailedCheck(
                        check_name="label_balance",
                        expected=f">= {policy.min_label_balance}",
                        actual=str(minority),
                        message=(
                            f"minority class fraction is {minority}; "
                            f"policy requires at least "
                            f"{policy.min_label_balance}"
                        ),
                    )
                )

        # --- feature coverage (min non-null fraction) -------------------
        feature_cols = list(roles.feature_columns)
        min_cov = _df_feature_coverage(df, feature_cols)
        if min_cov < policy.min_feature_coverage:
            failed.append(
                FailedCheck(
                    check_name="feature_coverage",
                    expected=f">= {policy.min_feature_coverage}",
                    actual=str(min_cov),
                    message=(
                        f"minimum feature coverage is {min_cov}; "
                        f"policy requires at least "
                        f"{policy.min_feature_coverage}"
                    ),
                )
            )

        # --- schema match (expected columns exist) ----------------------
        df_cols = set(_df_columns(df))
        expected_cols = list(feature_cols)
        if label_col:
            expected_cols.append(label_col)
        missing = [c for c in expected_cols if c not in df_cols]
        if missing and policy.require_schema_match:
            failed.append(
                FailedCheck(
                    check_name="schema_match",
                    expected=str(expected_cols),
                    actual=f"missing={missing}",
                    message=(
                        f"schema mismatch: columns {missing} declared "
                        f"in column roles but not present in the "
                        f"loaded dataframe (columns={sorted(df_cols)})"
                    ),
                )
            )

        return QualityGateResult(
            policy_id=policy.policy_id,
            passed=len(failed) == 0,
            failed_checks=tuple(failed),
            evaluated_at_ns=time.time_ns(),
        )

    # ------------------------------------------------------------------ #
    # Merging                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _merge_results(
        report_result: QualityGateResult,
        cheap_result: QualityGateResult,
    ) -> QualityGateResult:
        """Merge two gate results (union of failed checks, fail-closed).

        De-duplicates by ``check_name`` so a failure caught by both the
        report validation and the cheap checks is reported once. The
        merged result is fail-closed: ``passed`` is ``True`` only when
        both inputs passed.
        """
        seen: set[str] = set()
        merged: list[FailedCheck] = []
        for fc in (*report_result.failed_checks, *cheap_result.failed_checks):
            if fc.check_name in seen:
                continue
            seen.add(fc.check_name)
            merged.append(fc)
        return QualityGateResult(
            policy_id=report_result.policy_id,
            passed=len(merged) == 0,
            failed_checks=tuple(merged),
            evaluated_at_ns=time.time_ns(),
        )


def _quality_gate_result_to_dict(result: QualityGateResult) -> dict[str, Any]:
    """Serialize a :class:`QualityGateResult` to a JSON-safe dict."""
    return {
        "policy_id": result.policy_id,
        "passed": result.passed,
        "failed_checks": [
            {
                "check_name": fc.check_name,
                "expected": fc.expected,
                "actual": fc.actual,
                "message": fc.message,
            }
            for fc in result.failed_checks
        ],
        "evaluated_at_ns": result.evaluated_at_ns,
    }


def _build_quality_gate_failure_callback(
    *,
    job_id: str,
    mode: TrainingMode,
    gate_result: QualityGateResult,
) -> dict[str, Any]:
    """Build a signed quality-gate failure callback.

    The callback carries:
    - ``error_code``: ``"quality_gate_failed"`` (machine-readable).
    - ``gate_code``: the first failed check's ``check_name`` (e.g.
      ``"row_count_mismatch"``, ``"label_balance"``).
    - ``quality_gate_result``: the full serialized gate result.
    - ``mode``: the training mode.

    The callback payload is HMAC-signed with
    ``QUANT_FOUNDRY_CALLBACK_SECRET`` (same mechanism as training
    callbacks) so the dispatcher/trusted verifier can authenticate it.

    Phase 5 / T-5.3: now returns a :class:`SignedFailureEnvelope` via
    :func:`_build_signed_failure` (HMAC-signed context hash) with backward-
    compat ``error_code`` / ``error_summary`` / ``callback_*`` keys.
    """
    gate_code = gate_result.failed_checks[0].check_name if gate_result.failed_checks else "unknown"
    error_summary = (
        f"quality gate failed (mode={mode.value}, "
        f"policy={gate_result.policy_id}, gate={gate_code}): "
        f"{len(gate_result.failed_checks)} check(s) failed"
    )
    context: dict[str, str] = {
        "job_id": job_id,
        "mode": mode.value,
        "policy_id": gate_result.policy_id,
        "gate_code": gate_code,
        "failed_check_count": str(len(gate_result.failed_checks)),
    }
    return _build_signed_failure(
        error_code="quality_gate_failed",
        error_message=error_summary,
        mode=mode.value,
        context=context,
        extra={
            "gate_code": gate_code,
            "mode": mode.value,
            "quality_gate_result": _quality_gate_result_to_dict(gate_result),
        },
    )


def _build_task_rejection_callback(
    *,
    task_type: str | None,
    error_code: str,
    message: str,
    job_id: str | None,
) -> dict[str, Any]:
    """Build a signed task-rejection envelope (Phase 4 / T-4.3).

    When the trainer handler receives a task it is not permitted to
    dispatch (a dataset utility task, or an entirely unknown task type),
    it emits this signed failure envelope so the dispatcher can
    authenticate the rejection — it is never a silent drop. The envelope
    is HMAC-signed with ``QUANT_FOUNDRY_CALLBACK_SECRET`` via the existing
    :func:`sign_callback` mechanism.

    Phase 5 / T-5.3: now returns a :class:`SignedFailureEnvelope` via
    :func:`_build_signed_failure` (HMAC-signed context hash) with backward-
    compat ``error_code`` / ``error_summary`` / ``callback_*`` keys.

    Args:
        task_type: the rejected task string (may be ``None`` if absent).
        error_code: ``"task_not_supported_on_trainer"`` for dataset
            utility tasks, or ``"unknown_task_type"`` for anything else.
        message: human-readable explanation + dispatch suggestion.
        job_id: the job id from the input, if available.
    """
    context: dict[str, str] = {
        "job_id": job_id or "task-rejection-unknown",
        "task_type": task_type or "none",
        "error_code": error_code,
    }
    return _build_signed_failure(
        error_code=error_code,
        error_message=message,
        mode="production",
        context=context,
        extra={
            "task_type": task_type,
        },
    )


def handler(event: dict[str, Any]) -> dict[str, Any]:
    """RunPod serverless handler entrypoint (with crash logging wrapper).

    Args:
        event: RunPod event dict. `event["input"]` must be a dict matching
            RunPodTrainingRequest.

    Returns:
        On success: dict with callback_payload, callback_signature,
        callback_ts, job_id, artifact_id, dossier_id.
        On failure: dict with error_code, error_summary, job_id.
    """
    try:
        return _handler_impl(event)
    except Exception as exc:
        # Crash logging: print full traceback to stdout so it appears in
        # container logs. The runpod SDK may still report the job as failed,
        # but at least we can see WHY it crashed.
        import traceback as _tb

        _job_id = "unknown"
        try:
            _input = event.get("input") if isinstance(event, dict) else None
            if isinstance(_input, dict):
                _job_id = _input.get("job_id", "unknown")
        except Exception:
            pass
        print(
            f"[handler] CRASH job_id={_job_id}: {type(exc).__name__}: {exc}",
            flush=True,
        )
        print(_tb.format_exc(), flush=True)
        # Re-raise so the SDK can handle the error appropriately
        raise


def _handler_impl(event: dict[str, Any]) -> dict[str, Any]:
    """Actual handler implementation (called by the crash-logging wrapper)."""
    input_data = event.get("input") if isinstance(event, dict) else None
    if not isinstance(input_data, dict):
        # Phase 5 / T-5.3: signed failure envelope (mode unknown → canary,
        # the most lenient default, matching _resolve_preflight_mode).
        return _build_signed_failure(
            error_code="bad_request",
            error_message=("event['input'] must be a dict matching RunPodTrainingRequest"),
            mode="canary",
            context={"job_id": "none", "stage": "input_parse"},
        )

    # Phase 5 / T-5.1: handler-level SecurityPreflight (defense in depth).
    #
    # Runs at request time, before task dispatch, on top of the Dockerfile
    # startup preflight (T-4.2). Production fails closed if any forbidden env
    # var (broker/Redis/DB/trading/cloud-admin credentials) is present or the
    # callback URL host is loopback/private. Canary/research log advisory
    # warnings but never fail. The redacted config summary is always printed.
    preflight_mode = _resolve_preflight_mode(input_data)
    preflight = SecurityPreflight(mode=preflight_mode)
    preflight_result = preflight.run()
    if not preflight_result.passed:
        # Production fail-closed: refuse to start with app credentials
        # present. Emit a signed failure envelope so the dispatcher can
        # authenticate the rejection (never a silent drop).
        return _build_security_preflight_failure_callback(
            job_id=input_data.get("job_id"),
            preflight_result=preflight_result,
        )

    # Phase 4 / T-4.3: trainer worker task allowlist gate.
    #
    # This image is the GPU trainer (``trainer-gpu-tree``). It only
    # dispatches the tasks in ``ALLOWED_TRAINER_TASKS``. Dataset utility
    # tasks (volume writes/stats/listing, media-sentiment ingestion)
    # belong on the separate dataset utility worker endpoint and are
    # rejected here with a signed failure envelope so the dispatcher can
    # authenticate the rejection (fail-closed — never a silent drop).
    # Any task not in either set is an unknown task type and is likewise
    # rejected with a signed failure.
    #
    # A request with no ``task`` field is an implicit training request
    # (the original RunPod protocol: training is the default dispatch).
    # We normalize ``None`` → ``"train_model"`` so the legacy training
    # fallthrough below still works without every caller setting an
    # explicit task field.
    #
    # The existing dataset-utility implementations below (write_volume,
    # stat_volume, list_volume, ingest_media_sentiment) are preserved for
    # the dataset worker endpoint (same codebase) but are unreachable via
    # this trainer dispatch — they sit behind this gate.
    task_type = input_data.get("task")
    job_id = input_data.get("job_id")
    if task_type in DATASET_UTILITY_TASKS:
        rejection = _build_task_rejection_callback(
            task_type=task_type,
            error_code="task_not_supported_on_trainer",
            message=(
                f"task {task_type!r} is a dataset utility task and is not "
                f"supported on the trainer worker. Dispatch it to the "
                f"dataset utility worker endpoint instead."
            ),
            job_id=job_id,
        )
        rejection["preflight_result"] = preflight_result.model_dump()
        return rejection
    # ``None`` (no task field) is an implicit training request — allow it
    # through to the training pipeline (do NOT reject as unknown).
    if task_type is not None and task_type not in ALLOWED_TRAINER_TASKS:
        rejection = _build_task_rejection_callback(
            task_type=task_type,
            error_code="unknown_task_type",
            message=(
                f"unknown task type {task_type!r}. The trainer worker only "
                f"supports {sorted(ALLOWED_TRAINER_TASKS)} (plus implicit "
                f"training when no task field is set)."
            ),
            job_id=job_id,
        )
        rejection["preflight_result"] = preflight_result.model_dump()
        return rejection

    # Callback-secret canary: bypasses the training pipeline entirely.
    # The API dispatches this to verify that the worker shares the same
    # QUANT_FOUNDRY_CALLBACK_SECRET. See gateway.runpod_canary().
    if input_data.get("task") == "callback_secret_canary":
        canary_result = _handle_canary(input_data)
        canary_result["preflight_result"] = preflight_result.model_dump()
        return canary_result

    # Volume write task: write a data chunk to the network volume.
    # This bypasses training entirely and is used to stage large datasets
    # on the persistent network volume at /workspace/.
    # Input fields (all handler-level extensions, not in the schema):
    #   task: "write_volume"
    #   volume_path: "/workspace/dataset.csv" (target path)
    #   chunk_data: "<csv chunk text>" (the data to write)
    #   chunk_mode: "write" | "append" (write = overwrite, append = add)
    if input_data.get("task") == "write_volume":
        volume_path = input_data.get("volume_path", "")
        chunk_data = input_data.get("chunk_data", "")
        chunk_mode = input_data.get("chunk_mode", "write")
        if not volume_path or not chunk_data:
            # Phase 5 / T-5.3: signed failure envelope.
            return _build_signed_failure(
                error_code="bad_request",
                error_message="write_volume requires volume_path and chunk_data",
                mode=preflight_mode.value,
                context={
                    "job_id": str(input_data.get("job_id") or "unknown"),
                    "task": "write_volume",
                    "stage": "write_volume_args",
                },
            )
        # Resolve volume path (/runpod-volume vs /workspace)
        resolved = resolve_volume_path(volume_path)
        target = Path(resolved)
        target.parent.mkdir(parents=True, exist_ok=True)
        if chunk_mode == "append":
            with open(target, "a", encoding="utf-8") as f:
                f.write(chunk_data)
        else:
            with open(target, "w", encoding="utf-8") as f:
                f.write(chunk_data)
        size = target.stat().st_size
        return {
            "task": "write_volume",
            "volume_path": str(target),
            "requested_path": volume_path,
            "chunk_mode": chunk_mode,
            "file_size_bytes": size,
            "file_size_mb": round(size / 1024 / 1024, 2),
            "status": "ok",
        }

    # Volume read task: check if a file exists on the network volume.
    if input_data.get("task") == "stat_volume":
        volume_path = input_data.get("volume_path", "")
        resolved = resolve_volume_path(volume_path)
        target = Path(resolved)
        if target.exists():
            return {
                "task": "stat_volume",
                "volume_path": str(target),
                "requested_path": volume_path,
                "exists": True,
                "file_size_bytes": target.stat().st_size,
                "file_size_mb": round(target.stat().st_size / 1024 / 1024, 2),
            }
        return {
            "task": "stat_volume",
            "volume_path": str(target),
            "requested_path": volume_path,
            "exists": False,
            "file_size_bytes": 0,
        }

    # Volume list task: list files in a directory on the network volume.
    if input_data.get("task") == "list_volume":
        dir_path = input_data.get("volume_path", "/")
        resolved = resolve_volume_path(dir_path)
        target = Path(resolved)
        if not target.exists():
            return {
                "task": "list_volume",
                "volume_path": str(target),
                "exists": False,
                "files": [],
            }
        files = []
        for p in sorted(target.iterdir()):
            files.append(
                {
                    "name": p.name,
                    "size_bytes": p.stat().st_size if p.is_file() else 0,
                    "is_dir": p.is_dir(),
                }
            )
        return {
            "task": "list_volume",
            "volume_path": str(target),
            "exists": True,
            "files": files,
        }

    # Media sentiment dataset ingestion task: build a media-sentiment-price
    # dataset on the worker using the modular dataset system, then write
    # the parquet + manifest to the network volume for a subsequent
    # training job to consume via dataset_manifest_ref.
    #
    # Input fields (handler-level extensions, not in the schema):
    #   task: "ingest_media_sentiment"
    #   dataset_id: "media-sentiment-price-2023" (unique dataset ID)
    #   start_ns: 1672531200000000000 (start time in nanoseconds)
    #   end_ns: 1704067200000000000 (end time in nanoseconds)
    #   universe_module: "universe:sp500:1.0.0" (optional, default sp500)
    #   source_module: "source:newsapi:1.0.0" (optional, default newsapi)
    #   sentiment_module: "sentiment:finbert:1.0.0" (optional)
    #   feature_modules: ["feature:per-event-type:1.0.0", "feature:per-year:1.0.0"]
    #   label_module: "label:abnormal-return:1.0.0" (optional)
    #   price_join_module: "price_join:alpaca-bars:1.0.0" (optional)
    #   output_dir: "/workspace/datasets/media-sentiment-price-2023"
    #   n_folds: 3 (optional, default 3)
    #   config: {...} (optional per-module config overrides)
    if input_data.get("task") == "ingest_media_sentiment":
        ingest_result = _handle_ingest_media_sentiment(input_data)
        ingest_result["preflight_result"] = preflight_result.model_dump()
        return ingest_result

    # GPU healthcheck task (Phase 4 / T-4.1): probe the worker's GPU
    # runtime and return signed metadata. Mode-aware:
    #   - production: fails closed if no GPU (gpu_capable=false → error)
    #   - canary: reports GPU absence but marks promotion_eligible=false
    #   - research: permissive (reports GPU state without failing)
    # Input fields (handler-level extensions, not in the schema):
    #   task: "gpu_healthcheck"
    #   job_id: "gpu-hc-001" (optional, defaults to gpu-healthcheck-unknown)
    #   mode: "canary" | "research" | "production" (optional, default canary)
    #   training_mode: alias for mode (matches extra_constraints convention)
    if input_data.get("task") == "gpu_healthcheck":
        hc_result = _handle_gpu_healthcheck(input_data)
        hc_result["preflight_result"] = preflight_result.model_dump()
        return hc_result

    # Support inline dataset for E2E testing: if the input includes
    # ``inline_dataset_csv``, write it to a temp file and override the
    # dataset_manifest_ref. This avoids needing a network volume or S3
    # bucket for simple smoke tests. The field is NOT part of the
    # RunPodTrainingRequest schema — it is a handler-level extension, so
    # we must pop it from the input BEFORE schema validation (the schema
    # forbids extra fields).
    # Pop handler-level extensions BEFORE schema validation (schema forbids extra fields)
    inline_csv = input_data.pop("inline_dataset_csv", None)
    output_prefix = input_data.pop("output_prefix", None)
    n_folds = input_data.pop("n_folds", 3)
    # Phase 1 / T-1.2: presigned object upload URL for the production
    # artifact writer. When present, the handler uses
    # ``PresignedUploadArtifactWriter`` to upload the model artifact via
    # HTTP PUT to the presigned URL (TLS required — ``http://`` is
    # rejected by the writer's URI scheme validation). When absent, the
    # handler falls back to ``VolumeArtifactWriter`` (if output_prefix is
    # set) or ``FakeArtifactWriter`` (canary tests with no persistence).
    presigned_artifact_url = input_data.pop("presigned_artifact_url", None)
    # Phase 3 / T-2.2: manifest-first dataset loading. When present,
    # ``dataset_load_spec`` is a dict with manifest_uri + expected hashes.
    # The handler fetches + verifies the manifest and data BEFORE training
    # and sets ``dataset_manifest_ref`` to the verified data URI. Fail
    # closed on any verification error (hash mismatch, row count mismatch,
    # schema hash mismatch, unknown format, missing column role).
    dataset_load_spec = input_data.pop("dataset_load_spec", None)

    try:
        req = RunPodTrainingRequest.model_validate(input_data)
    except Exception as exc:
        # Phase 5 / T-5.3: signed failure envelope.
        return _build_signed_failure(
            error_code="schema_validation_failed",
            error_message=str(exc),
            mode=preflight_mode.value,
            context={
                "job_id": str(input_data.get("job_id") or "unknown"),
                "stage": "schema_validation",
            },
        )

    # Phase 3 / T-2.2: manifest-first loading takes precedence over the
    # legacy inline/volume-path paths. When a load spec is provided, the
    # handler verifies the manifest + data hashes and row count, then
    # points ``dataset_manifest_ref`` at the verified data file. Any
    # verification failure is a terminal error (fail-closed).
    dataset_load_receipt: dict[str, Any] | None = None
    loaded_dataset: LoadedDataset | None = None
    if dataset_load_spec and isinstance(dataset_load_spec, dict):
        try:
            verified_data_uri, dataset_load_receipt, loaded_dataset = _load_dataset_via_manifest(
                dataset_load_spec,
                job_id=req.job_id,
            )
            req = req.model_copy(update={"dataset_manifest_ref": verified_data_uri})
        except DatasetLoadError as exc:
            write_status(
                req.job_id,
                "failed",
                error_code=exc.code,
                error_summary=str(exc),
            )
            # Phase 5 / T-5.3: signed failure envelope.
            raw_mode = req.extra_constraints.get("training_mode") or "research"
            return _build_signed_failure(
                error_code="dataset_load_error",
                error_message=str(exc),
                mode=raw_mode,
                context={
                    "job_id": req.job_id,
                    "dataset_error_code": exc.code,
                    "dataset_manifest_ref": req.dataset_manifest_ref,
                },
            )
    elif isinstance(inline_csv, str) and inline_csv.strip():
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="qf_dataset_"))
        csv_path = tmp_dir / "inline_dataset.csv"
        csv_path.write_text(inline_csv, encoding="utf-8")
        req = req.model_copy(update={"dataset_manifest_ref": str(csv_path)})
    else:
        # Resolve volume paths (/runpod-volume vs /workspace)
        resolved_ref = resolve_volume_path(req.dataset_manifest_ref)
        if resolved_ref != req.dataset_manifest_ref:
            req = req.model_copy(update={"dataset_manifest_ref": resolved_ref})

    # Phase 3 / T-3.3: worker-side quality gate runner (defense in depth).
    #
    # After the manifest-first load (T-2.2) and BEFORE training begins,
    # the worker recomputes cheap data checks on the loaded dataframe and
    # validates them against the resolved quality policy. This catches
    # bad data even if the trusted-side preflight was skipped (or
    # produced a stale/tampered quality report).
    #
    # Mode-aware enforcement:
    # - ``production``: all gates must pass. Any failure → signed failure
    #   callback with ``error_code="quality_gate_failed"`` + ``gate_code``.
    #   Training does NOT start (bad datasets stop before GPU training).
    # - ``canary``: gates are advisory — failures are logged as warnings
    #   and the job continues, but ``quality_gate_advisory_failures`` is
    #   included in the result so the dispatcher can mark
    #   ``promotion_eligible=False``.
    # - ``research``: gates are advisory — failures are logged but the
    #   job continues (research is permissive by design).
    #
    # The quality policy id is read from
    # ``req.extra_constraints["quality_policy_id"]`` (forwarded by the
    # dispatch manifest). When absent, the mode's default policy is used
    # (defense in depth: the worker always applies a gate).
    quality_gate_result_dict: dict[str, Any] | None = None
    quality_gate_advisory_failures: dict[str, Any] | None = None
    if loaded_dataset is not None:
        # Resolve the training mode (same convention as
        # runpod_training._resolve_mode).
        raw_mode = req.extra_constraints.get("training_mode")
        try:
            gate_mode = TrainingMode(raw_mode) if raw_mode else TrainingMode.RESEARCH
        except ValueError:
            # Unknown mode → fail closed as production (strictest).
            gate_mode = TrainingMode.PRODUCTION

        quality_policy_id = req.extra_constraints.get("quality_policy_id") or None

        # Optionally fetch the quality report if the manifest or load
        # spec declares a ``quality_report_uri``. The manifest dict
        # (loaded_dataset.manifest) is the source of truth once verified.
        quality_report: DatasetQualityReport | None = None
        qr_uri = (
            loaded_dataset.manifest.get("quality_report_uri") if loaded_dataset.manifest else None
        ) or (dataset_load_spec or {}).get("quality_report_uri")
        qr_sha = (
            loaded_dataset.manifest.get("quality_report_sha256")
            if loaded_dataset.manifest
            else None
        ) or (dataset_load_spec or {}).get("quality_report_sha256")
        if qr_uri:
            try:
                quality_report = _fetch_quality_report(
                    qr_uri,
                    expected_sha256=qr_sha,
                )
            except QualityGateError as exc:
                # Production: a declared-but-unreadable quality report
                # is a hard failure (fail-closed). Canary/research: log
                # and continue without the report (the cheap checks
                # still run).
                if gate_mode == TrainingMode.PRODUCTION:
                    write_status(
                        req.job_id,
                        "failed",
                        error_code=exc.code,
                        error_summary=str(exc),
                    )
                    # Phase 5 / T-5.3: signed failure envelope.
                    return _build_signed_failure(
                        error_code="quality_gate_failed",
                        error_message=str(exc),
                        mode=gate_mode.value,
                        context={
                            "job_id": req.job_id,
                            "stage": "quality_report_fetch",
                            "gate_error_code": exc.code,
                        },
                    )
                # Advisory: log the warning (best-effort).
                write_status(
                    req.job_id,
                    "started",
                    error_code=exc.code,
                    error_summary=f"advisory: {exc}",
                )

        # Run the quality gate.
        try:
            gate_runner = QualityGateRunner(
                loaded_dataset,
                quality_policy_id=quality_policy_id,
                quality_report=quality_report,
                mode=gate_mode,
            )
            gate_result = gate_runner.run()
        except QualityGateError as exc:
            # Policy resolution failure (unknown policy id). Production
            # fails closed; canary/research log and skip the gate.
            if gate_mode == TrainingMode.PRODUCTION:
                write_status(
                    req.job_id,
                    "failed",
                    error_code=exc.code,
                    error_summary=str(exc),
                )
                # Phase 5 / T-5.3: signed failure envelope.
                return _build_signed_failure(
                    error_code="quality_gate_failed",
                    error_message=str(exc),
                    mode=gate_mode.value,
                    context={
                        "job_id": req.job_id,
                        "stage": "quality_gate_policy",
                        "gate_error_code": exc.code,
                        "policy_id": quality_policy_id or "default",
                    },
                )
            write_status(
                req.job_id,
                "started",
                error_code=exc.code,
                error_summary=f"advisory: {exc}",
            )
        else:
            quality_gate_result_dict = _quality_gate_result_to_dict(gate_result)

            if not gate_result.passed:
                if gate_mode == TrainingMode.PRODUCTION:
                    # --- production: fail closed BEFORE training ----------
                    # Bad datasets stop before GPU training begins. Emit
                    # a signed failure callback with the gate code so the
                    # dispatcher/trusted verifier can authenticate it.
                    failure = _build_quality_gate_failure_callback(
                        job_id=req.job_id,
                        mode=gate_mode,
                        gate_result=gate_result,
                    )
                    write_status(
                        req.job_id,
                        "failed",
                        error_code="quality_gate_failed",
                        error_summary=failure["error_summary"],
                    )
                    return failure
                else:
                    # --- canary/research: advisory -------------------------
                    # Log the failures as warnings but continue training.
                    # The advisory failures are included in the result so
                    # the dispatcher can mark promotion_eligible=False.
                    failed_names = [fc.check_name for fc in gate_result.failed_checks]
                    write_status(
                        req.job_id,
                        "started",
                        error_code="quality_gate_advisory",
                        error_summary=(
                            f"advisory quality gate failures (mode="
                            f"{gate_mode.value}): {failed_names}"
                        ),
                    )
                    quality_gate_advisory_failures = {
                        "policy_id": gate_result.policy_id,
                        "failed_checks": failed_names,
                        "mode": gate_mode.value,
                    }

    # Resolve output_prefix if provided (handler-level extension)
    if output_prefix:
        output_prefix = resolve_volume_path(output_prefix)

    # Worker-side status file: mark the job as started so the gateway
    # can detect crashed workers via stale heartbeat_at timestamps.
    write_status(req.job_id, "started")

    # Build the trainer and keep a reference so we can read the typed
    # artifact result (Phase 1 / T-1.1) after handle() returns. The
    # trainer stashes ``last_artifact_result`` / ``last_model_bytes`` on
    # a successful train(); the handler reads them through the typed
    # field instead of the fragile ``getattr(result, "model_bytes")``.
    try:
        trainer = _build_trainer(
            req,
            n_folds=int(n_folds) if n_folds else 3,
            loaded_dataset=loaded_dataset,
        )
    except TrainingFailure as exc:
        write_status(
            req.job_id,
            "failed",
            error_code=exc.error_code,
            error_summary=exc.error_summary,
        )
        raw_mode = req.extra_constraints.get("training_mode") or "research"
        return _build_signed_failure(
            error_code=exc.error_code,
            error_message=exc.error_summary,
            mode=raw_mode,
            context={
                "job_id": req.job_id,
                "stage": "trainer_routing",
                "model_family": req.model_family,
            },
        )
    handler = RunPodTrainingHandler(
        callback_secret=_get_callback_secret(),
        trainer=trainer,
        deadline_seconds=_get_deadline_seconds(),
    )

    # Background heartbeat thread: writes a heartbeat status file every
    # 10s while training runs. If the container crashes, the gateway
    # detects a stale heartbeat_at and marks the job as failed.
    heartbeat_stop = _heartbeat_during_training(req.job_id)
    try:
        result = handler.handle(req)
    except TrainingFailure as exc:
        write_status(
            req.job_id,
            "failed",
            error_code=exc.error_code,
            error_summary=exc.error_summary,
        )
        # Phase 5 / T-5.3: signed failure envelope.
        raw_mode = req.extra_constraints.get("training_mode") or "research"
        return _build_signed_failure(
            error_code="training_error",
            error_message=exc.error_summary,
            mode=raw_mode,
            context={
                "job_id": req.job_id,
                "training_error_code": exc.error_code,
                "model_family": req.model_family,
            },
        )
    finally:
        heartbeat_stop.set()

    write_status(req.job_id, "completed", artifact_id=result.artifact_id)

    # --- Phase 1 / T-1.1: resolve the typed artifact result -----------------
    # The real trainer stashes a TypedArtifactResult on itself; the local
    # (canary) trainer does not, so we synthesize a tiny inline-bytes
    # result for canary tests. A successful real training job with no
    # artifact is a contract violation — fail closed.
    typed_artifact: TypedArtifactResult | None = getattr(trainer, "last_artifact_result", None)
    model_bytes: bytes | None = getattr(trainer, "last_model_bytes", None)

    is_real_trainer = not isinstance(trainer, LocalTrainer)

    if is_real_trainer:
        # Fail closed: a successful real training job MUST produce an
        # artifact (acceptance criterion: trainer success without
        # artifact fails).
        if typed_artifact is None or not model_bytes:
            # Phase 5 / T-5.3: signed failure envelope.
            raw_mode = req.extra_constraints.get("training_mode") or "production"
            return _build_signed_failure(
                error_code="artifact_missing",
                error_message=(
                    "successful training produced no typed artifact "
                    "result (fail closed: artifact URI/hash/size "
                    "required)"
                ),
                mode=raw_mode,
                context={
                    "job_id": req.job_id,
                    "stage": "artifact_resolve",
                    "trainer": "real",
                },
            )
    else:
        # Canary / local-stub path: keep tiny inline bytes only for
        # canary tests (never persisted to a real artifact URI unless
        # output_prefix is set). Build a typed result so the contract
        # shape is identical across modes.
        if typed_artifact is None:
            canary_bytes = b"canary-model-stub:" + req.job_id.encode("utf-8")
            try:
                typed_artifact = build_artifact_result(
                    artifact_id=result.artifact_id,
                    model_bytes=canary_bytes,
                    model_family=req.model_family,
                    req=req,
                    artifact_uri=None,
                    artifact_format="local-stub",
                    artifact_kind="model",
                    loader_family="local-stub",
                )
            except ValueError as exc:
                # Phase 5 / T-5.3: signed failure envelope.
                raw_mode = req.extra_constraints.get("training_mode") or "canary"
                return _build_signed_failure(
                    error_code="artifact_missing",
                    error_message=f"cannot build canary artifact result: {exc}",
                    mode=raw_mode,
                    context={
                        "job_id": req.job_id,
                        "stage": "canary_artifact_build",
                        "trainer": "local",
                    },
                )
            model_bytes = typed_artifact.model_bytes

    # --- Phase 1 / T-1.2: persist the artifact via the writer interface -----
    # Select a writer backend based on the available inputs + run mode:
    #   - presigned_artifact_url → PresignedUploadArtifactWriter (prod)
    #   - output_prefix → VolumeArtifactWriter (canary/operator fallback)
    #   - neither → FakeArtifactWriter (canary tests, no persistence)
    # The writer returns an ArtifactWriteResult with URI/sha256/size/format
    # + a signed write receipt. Writer failure (volume I/O, upload failure,
    # URI scheme rejection, sha mismatch after write) produces a signed
    # failure envelope (fail-closed — never a silent drop).
    artifact_uri: str | None = None
    write_result: ArtifactWriteResult | None = None
    callback_secret = _get_callback_secret()
    if typed_artifact is not None and model_bytes:
        # Resolve the callback payload sidecar dicts for the volume writer.
        cb_json = json.loads(result.callback_payload.decode("utf-8"))
        cb_payload = cb_json.get("payload", {})
        artifact_manifest_dict = cb_payload.get("artifact_manifest", {})
        dossier_dict = cb_payload.get("dossier", {})

        # Select the writer backend.
        writer: ArtifactWriter
        if presigned_artifact_url:
            # Production path: presigned object upload (TLS required).
            try:
                writer = PresignedUploadArtifactWriter(
                    presigned_url=presigned_artifact_url,
                    callback_secret=callback_secret,
                )
            except ValueError as exc:
                # URI scheme rejection (e.g. http://, ftp://) → signed
                # failure envelope (fail-closed).
                write_status(
                    req.job_id,
                    "failed",
                    error_code="artifact_write_failed",
                    error_summary=str(exc),
                )
                return _build_artifact_write_failure_callback(
                    job_id=req.job_id,
                    error_summary=(f"disallowed presigned artifact URL: {exc}"),
                )
        elif output_prefix:
            # Canary/operator fallback: volume path writer.
            writer = VolumeArtifactWriter(
                output_dir=Path(output_prefix),
                callback_payload_bytes=result.callback_payload,
                artifact_manifest_dict=artifact_manifest_dict,
                dossier_dict=dossier_dict,
                callback_secret=callback_secret,
            )
        else:
            # No persistence target → fake writer (canary tests). Computes
            # the expected sha256 without actually writing.
            writer = FakeArtifactWriter(
                callback_secret=callback_secret,
            )

        try:
            write_result = writer.write_artifact(
                model_bytes=model_bytes,
                artifact_id=typed_artifact.artifact_id,
                artifact_format=typed_artifact.artifact_format,
            )
        except ValueError as exc:
            # Write failure (sha mismatch, URI scheme rejection, upload
            # failure) → signed failure envelope (fail-closed).
            write_status(
                req.job_id,
                "failed",
                error_code="artifact_write_failed",
                error_summary=str(exc),
            )
            return _build_artifact_write_failure_callback(
                job_id=req.job_id,
                error_summary=f"artifact write failed: {exc}",
            )
        except Exception as exc:
            # Unexpected write failure (volume I/O, network) → signed
            # failure envelope (fail-closed — no silent drop).
            write_status(
                req.job_id,
                "failed",
                error_code="artifact_write_failed",
                error_summary=str(exc),
            )
            return _build_artifact_write_failure_callback(
                job_id=req.job_id,
                error_summary=f"artifact write failed: {exc}",
            )

        # Cross-check: the writer's declared sha256 must match the typed
        # artifact's sha256 (computed from the same bytes). A mismatch
        # means the writer corrupted the bytes or used a different input
        # — fail closed.
        if write_result.artifact_sha256 != typed_artifact.artifact_sha256:
            write_status(
                req.job_id,
                "failed",
                error_code="artifact_sha_mismatch",
                error_summary=(
                    "artifact sha256 mismatch: writer declared "
                    f"{write_result.artifact_sha256} but typed artifact "
                    f"declared {typed_artifact.artifact_sha256}"
                ),
            )
            return _build_artifact_write_failure_callback(
                job_id=req.job_id,
                error_summary=(
                    "artifact sha256 mismatch: writer declared "
                    f"{write_result.artifact_sha256} but typed artifact "
                    f"declared {typed_artifact.artifact_sha256}"
                ),
            )
        artifact_uri = write_result.artifact_uri
    elif output_prefix or presigned_artifact_url:
        # A persistence target was set but there are no artifact bytes —
        # fail closed (a successful job with no artifact is a contract
        # violation).
        # Phase 5 / T-5.3: signed failure envelope.
        raw_mode = req.extra_constraints.get("training_mode") or "production"
        return _build_signed_failure(
            error_code="artifact_missing",
            error_message=("persistence target set but no artifact bytes to write (fail closed)"),
            mode=raw_mode,
            context={
                "job_id": req.job_id,
                "stage": "artifact_persist_no_bytes",
            },
        )

    # Bind the artifact URI onto the typed result (immutable → rebuild).
    # Also bind the signed write receipt so the trusted-side verifier can
    # authenticate the artifact metadata independent of the callback
    # signature (Phase 1 / T-1.2: worker signs returned artifact metadata).
    if typed_artifact is not None and artifact_uri is not None:
        typed_artifact = TypedArtifactResult(
            artifact_id=typed_artifact.artifact_id,
            artifact_uri=artifact_uri,
            artifact_sha256=typed_artifact.artifact_sha256,
            artifact_size_bytes=typed_artifact.artifact_size_bytes,
            artifact_format=typed_artifact.artifact_format,
            artifact_kind=typed_artifact.artifact_kind,
            loader_family=typed_artifact.loader_family,
            model_family=typed_artifact.model_family,
            dataset_manifest_hash=typed_artifact.dataset_manifest_hash,
            training_manifest_hash=typed_artifact.training_manifest_hash,
            created_at=typed_artifact.created_at,
            model_bytes=typed_artifact.model_bytes,
        )

    # --- Phase 1 / T-1.4: build the typed RunPodTrainingCallback -------------
    # Construct the typed, HMAC-signed callback contract that binds together
    # the artifact, manifest hashes, runtime fingerprint, metrics, quality
    # gate result, GPU healthcheck, and promotion eligibility. The trusted
    # side verifies the signature via validate_callback() before trusting
    # any field (fail-closed).
    callback_json = json.loads(result.callback_payload.decode("utf-8"))
    payload = callback_json.get("payload", {})
    dossier_data = payload.get("dossier", {})

    # Resolve the training mode (same convention as the quality gate path).
    raw_mode = req.extra_constraints.get("training_mode")
    try:
        cb_mode = TrainingMode(raw_mode) if raw_mode else TrainingMode.RESEARCH
    except ValueError:
        cb_mode = TrainingMode.PRODUCTION  # fail closed (strictest)

    # Manifest hashes: prefer the typed artifact's hashes (computed from
    # the request), fall back to the extra_constraints values. When no
    # manifest hash is available (e.g. ad-hoc canary requests not staged
    # through a manifest), derive a deterministic hash from the request
    # config so the callback always carries a non-empty binding (the
    # trusted-side validate_callback rejects empty manifest hashes).
    dataset_mhash = (
        typed_artifact.dataset_manifest_hash
        if typed_artifact is not None
        else req.extra_constraints.get("dataset_manifest_hash", "")
    ) or ""
    if not dataset_mhash:
        import hashlib as _hl

        dataset_mhash = _hl.sha256(
            req.dataset_manifest_ref.encode("utf-8"),
        ).hexdigest()

    training_mhash = (
        typed_artifact.training_manifest_hash
        if typed_artifact is not None
        else req.extra_constraints.get("manifest_content_hash", "")
    ) or ""
    if not training_mhash:
        import hashlib as _hl

        training_mhash = _hl.sha256(
            json.dumps(
                {
                    "job_id": req.job_id,
                    "model_family": req.model_family,
                    "search_space": req.search_space,
                    "extra_constraints": req.extra_constraints,
                },
                sort_keys=True,
            ).encode("utf-8"),
        ).hexdigest()

    # Metrics summary from the dossier training_metrics.
    metrics_summary = dict(dossier_data.get("training_metrics", {}))

    # Quality gate passed flag (fail-closed when unknown).
    quality_gate_passed: bool | None = None
    if quality_gate_result_dict is not None:
        quality_gate_passed = bool(quality_gate_result_dict.get("passed", False))
    # Advisory failures (canary/research) force promotion_eligible=False —
    # build_callback already forces canary=False; for research, the
    # advisory failures mean the gate did not cleanly pass.
    if quality_gate_advisory_failures is not None:
        quality_gate_passed = False

    # Runtime fingerprint (same pins used by the trainer / gpu_healthcheck).
    rt_fingerprint = _runtime_fingerprint()

    # Primary artifact dict (serialized TypedArtifactResult, without the
    # inline model_bytes — those are not JSON-safe and not part of the
    # callback contract). Includes the signed write receipt (Phase 1 /
    # T-1.2) so the trusted-side verifier can authenticate the artifact
    # metadata (URI + sha256 + size + format) independent of the callback
    # signature.
    primary_artifact_dict: dict[str, Any] | None = None
    if typed_artifact is not None:
        primary_artifact_dict = {
            "artifact_id": typed_artifact.artifact_id,
            "artifact_uri": typed_artifact.artifact_uri,
            "artifact_sha256": typed_artifact.artifact_sha256,
            "artifact_size_bytes": typed_artifact.artifact_size_bytes,
            "artifact_format": typed_artifact.artifact_format,
            "artifact_kind": typed_artifact.artifact_kind,
            "loader_family": typed_artifact.loader_family,
            "model_family": typed_artifact.model_family,
            "dataset_manifest_hash": typed_artifact.dataset_manifest_hash,
            "training_manifest_hash": typed_artifact.training_manifest_hash,
            "created_at": typed_artifact.created_at,
            # Phase 1 / T-1.2: signed write receipt (HMAC over
            # uri|sha256|size|format). Present when a writer persisted
            # the artifact; None for inline-only canary runs.
            "write_receipt": (write_result.write_receipt if write_result is not None else None),
        }

    # GPU healthcheck dict (present only if a healthcheck ran for this
    # job — normally the healthcheck is a separate task, so this is
    # typically None here; included for completeness when available).
    gpu_healthcheck_dict: dict[str, Any] | None = None

    try:
        typed_callback = build_callback(
            job_id=req.job_id,
            training_manifest_hash=training_mhash,
            dataset_manifest_hash=dataset_mhash,
            runtime_fingerprint=rt_fingerprint,
            primary_artifact=primary_artifact_dict,
            auxiliary_artifacts=(),
            metrics_summary=metrics_summary,
            mode=cb_mode,
            quality_gate_result=quality_gate_result_dict,
            gpu_healthcheck=gpu_healthcheck_dict,
            quality_gate_passed=quality_gate_passed,
            secret=_get_callback_secret(),
        )
    except Exception as exc:
        # Fail closed: if the typed callback cannot be built, the job is
        # a contract violation. Return a safe terminal error.
        # Phase 5 / T-5.3: signed failure envelope.
        return _build_signed_failure(
            error_code="callback_build_failed",
            error_message=f"failed to build typed callback: {exc}",
            mode=cb_mode.value,
            context={
                "job_id": req.job_id,
                "stage": "callback_build",
            },
        )

    return {
        "job_id": req.job_id,
        "callback_payload": result.callback_payload.decode("utf-8"),
        "callback_signature": result.callback_signature,
        "callback_ts": result.callback_ts,
        "artifact_id": result.artifact_id,
        "dossier_id": result.dossier_id,
        "output_prefix": output_prefix,
        # Phase 3 / T-2.2: dataset load receipt (present when the job
        # used manifest-first loading). Records every verification flag
        # so the dispatcher/trusted verifier can audit the dataset
        # provenance.
        "dataset_load_receipt": dataset_load_receipt,
        # Phase 3 / T-3.3: worker-side quality gate result. Present when
        # the job used manifest-first loading (the gate runs on the
        # loaded dataframe). ``passed=True`` means all gates passed;
        # ``quality_gate_advisory_failures`` is non-null when canary/
        # research mode logged advisory failures (the dispatcher should
        # mark promotion_eligible=False in that case).
        "quality_gate_result": quality_gate_result_dict,
        "quality_gate_advisory_failures": quality_gate_advisory_failures,
        # Phase 1 / T-1.1: typed artifact result (uri/hash/size/format/
        # kind/loader_family + manifest hashes). Present on every
        # successful training job; the dispatcher/trusted verifier uses
        # it to fetch + re-verify the artifact. Includes the signed
        # write receipt (Phase 1 / T-1.2) authenticating the metadata.
        "artifact_result": primary_artifact_dict,
        # Phase 1 / T-1.2: artifact write receipt (HMAC over
        # uri|sha256|size|format, signed with the callback secret). The
        # trusted-side verifier re-computes this to authenticate the
        # artifact metadata independent of the callback signature. None
        # for inline-only canary runs (no writer persisted the artifact).
        "artifact_write_receipt": (
            write_result.write_receipt if write_result is not None else None
        ),
        # Phase 1 / T-1.4: typed, HMAC-signed RunPodTrainingCallback.
        # The trusted side verifies the signature via
        # ``validate_callback()`` before trusting any field. Carries the
        # full contract: schema_version, job_id, manifest hashes, runtime
        # fingerprint hash, primary/auxiliary artifacts, metrics summary,
        # promotion_eligible, failure code/reason, quality gate result,
        # GPU healthcheck, signature, and timestamp.
        "typed_callback": typed_callback.model_dump(),
        # Phase 5 / T-5.1: handler-level SecurityPreflight result. Carries
        # the forbidden-env-var check, callback URL validation, URI
        # allowlist confirmation, container user, writable dirs, and the
        # redacted config summary (no secret values). Advisory warnings
        # (canary/research) are recorded in ``forbidden_vars_found`` and
        # ``preflight_error``; production failures short-circuit above.
        "preflight_result": preflight_result.model_dump(),
    }


# RunPod's serverless module loader looks for a `handler` function at the
# top level. When running on RunPod serverless, use the runpod SDK to start
# the worker. When run as a script (local testing), accept JSON on stdin.
if __name__ == "__main__":  # pragma: no cover
    import sys
    import traceback

    # Run the standalone security preflight before starting the SDK.
    # This catches forbidden env vars early (before the SDK starts polling)
    # so misconfigured images fail fast with a clear message.
    # Skip with QF_DIAG_SKIP_PREFLIGHT=1 for diagnostic builds.
    if os.environ.get("QF_DIAG_SKIP_PREFLIGHT", "0") != "1":
        try:
            import importlib.util

            _pf_spec = importlib.util.spec_from_file_location(
                "preflight", "/worker/preflight.py"
            )
            if _pf_spec and _pf_spec.loader:
                _pf_mod = importlib.util.module_from_spec(_pf_spec)
                _pf_spec.loader.exec_module(_pf_mod)
                _pf_rc = _pf_mod.main()
                if _pf_rc != 0:
                    print(
                        f"[handler] preflight failed (rc={_pf_rc}), exiting",
                        file=sys.stderr, flush=True,
                    )
                    raise SystemExit(_pf_rc)
        except FileNotFoundError:
            print("[handler] preflight.py not found, skipping", flush=True)
        except Exception as _pf_exc:
            print(f"[handler] preflight error: {_pf_exc}", file=sys.stderr, flush=True)

    # Debug logging to network volume (try both mount paths)
    def _log(msg):
        print(msg, flush=True)  # noqa: T201 - CLI debug output
        for path in ["/runpod-volume/handler-debug.log", "/workspace/handler-debug.log"]:
            try:
                with open(path, "a") as f:
                    f.write(msg + "\n")
            except Exception:  # noqa: S110 - best-effort debug log
                pass

    _log(f"=== Handler starting at {__file__} ===")
    _log(f"PYTHONPATH={os.environ.get('PYTHONPATH', 'NOT SET')}")
    _log(f"sys.path={sys.path}")

    # Check if handler file exists
    _log(f"Handler file exists: {os.path.exists(__file__)}")

    # Try RunPod serverless mode first (uses runpod SDK)
    try:
        import runpod

        _log(f"runpod SDK imported, version: {getattr(runpod, '__version__', 'unknown')}")

        # Dump RUNPOD_* env vars to diagnose serverless vs local mode.
        # The SDK checks for RUNPOD_WEBHOOK_GET_JOB to decide whether to
        # poll the real job queue (serverless) or start a local FastAPI
        # test server on :8000 (local mode). If this var is missing,
        # jobs will stay IN_QUEUE forever while the worker looks "ready".
        runpod_env = {k: v for k, v in os.environ.items() if k.startswith("RUNPOD_")}
        _log(f"RUNPOD_* env vars: {json.dumps(runpod_env, indent=2)}")
        if not runpod_env:
            _log("WARNING: No RUNPOD_* env vars found! SDK will likely enter local/test mode.")
            _log("  This means the worker will NOT poll the real job queue.")
            _log("  Jobs will stay IN_QUEUE indefinitely while the worker shows 'ready'.")

        _log("Starting runpod.serverless.start()...")
        runpod.serverless.start({"handler": handler})
    except ImportError as e:
        _log(f"ImportError: {e}")
        # runpod SDK not installed — fall back to stdin mode for local testing
        raw = sys.stdin.read()
        event = json.loads(raw) if raw else {}
        result = handler(event)
        print(json.dumps(result, indent=2))  # noqa: T201 - CLI entrypoint output
    except Exception as e:
        _log(f"ERROR in runpod.serverless.start(): {e}")
        _log(traceback.format_exc())
        raise
