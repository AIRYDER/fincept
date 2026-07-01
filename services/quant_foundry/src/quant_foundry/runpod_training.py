"""
quant_foundry.runpod_training — RunPod training worker handler (TASK-0501).

This is the first RunPod worker. It runs in a container on RunPod's GPU
infrastructure (or locally for testing). It receives a RunPodTrainingRequest,
reads a dataset manifest ref, trains a tiny baseline model, writes an
ArtifactManifest + ModelDossier, builds a signed RunPodCallbackEnvelope,
and returns the callback payload + signature.

Critical invariants (enforced + tested):
- NO broker credentials, NO Redis, NO stream write capability. The handler
  is a pure function over its inputs. It has no `redis`, `broker`, `bus`,
  `producer`, `stream`, `sig_predict_writer`, `order_writer`, or trading
  attributes. This is a hard security boundary.
- Same contract as the mock dispatcher (TASK-0305): the RunPodCallbackEnvelope
  shape and signature are identical. Flipping from mock to RunPod is a
  dispatcher-only change.
- Deterministic: identical inputs (seed + request) produce identical
  artifact_id / sha256 / dossier metrics. The artifact is hash-verifiable.
- Shadow-only: the dossier always carries authority=SHADOW_ONLY.
- Time/budget enforced: a deadline breach raises TrainingFailure (safe
  terminal status, not a crash).
- Training failure returns a safe terminal status (TrainingFailure), not
  a raw exception.

Training modes (Phase 0):
    The training system supports three modes — ``canary``, ``research``,
    and ``production`` — defined in
    :class:`quant_foundry.training_manifest.TrainingMode`. The
    authoritative rules table is
    :data:`quant_foundry.training_manifest.MODE_RULES`; builders and
    operators consult that single table when deciding what rules apply.

    - ``canary``: small registered dataset, tiny artifacts, never
      promotion eligible. CPU fallback allowed.
    - ``research``: real RunPod training, experimental families allowed,
      promotion disabled unless escalated. CPU fallback allowed.
    - ``production``: registered L3/L4 dataset, GPU required, artifact
      verification required, quality gates required, no CPU fallback.

    **Local training is not an acceptance substitute.** A ``canary`` or
    ``research`` run may execute on the local CPU trainer for contract
    proofs, but a ``production`` run MUST execute on real RunPod GPU
    infrastructure. Use :func:`validate_mode` to enforce the mode rules
    at the dispatch boundary before handing a request to a trainer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
    RunPodTrainingRequest,
)
from quant_foundry.signatures import sign_callback
from quant_foundry.training_manifest import (
    MODE_RULES,
    TrainingMode,
)

# --- errors ----------------------------------------------------------------


class TrainingFailure(Exception):
    """Safe terminal failure from the training handler.

    Carries an error_code (machine-readable) and error_summary (human-readable)
    so the dispatcher/gateway can record them in the outbox FAILED transition.
    """

    def __init__(self, error_code: str, error_summary: str) -> None:
        super().__init__(error_summary)
        self.error_code = error_code
        self.error_summary = error_summary


# --- mode validation (Phase 0) ---------------------------------------------


class ModeValidationError(ValueError):
    """Raised when a training request violates its mode's rules.

    This is a :class:`ValueError` subclass so it surfaces as a
    schema-level rejection (fail closed). The dispatch path should
    catch it and translate it into a ``TrainingFailure`` (or a gateway
    bad-request response) depending on where validation runs.
    """


def _resolve_mode(req: RunPodTrainingRequest) -> TrainingMode:
    """Resolve the training mode from a ``RunPodTrainingRequest``.

    The cross-boundary ``RunPodTrainingRequest`` schema does not carry a
    dedicated ``mode`` field (it must stay minimal for the worker). The
    mode is forwarded through ``extra_constraints["training_mode"]`` by
    :meth:`TrainingManifest.to_dispatch_request`. If absent, default to
    ``research`` (the historical, permissive behaviour).
    """
    raw = req.extra_constraints.get("training_mode")
    if raw is None:
        return TrainingMode.RESEARCH
    try:
        return TrainingMode(raw)
    except ValueError:
        raise ModeValidationError(
            f"unknown training_mode {raw!r}; expected one of "
            f"{[m.value for m in TrainingMode]}"
        ) from None


def validate_mode(req: RunPodTrainingRequest) -> TrainingMode:
    """Validate that ``req`` satisfies its training mode's rules.

    The mode is read from ``req.extra_constraints["training_mode"]``
    (forwarded by :meth:`TrainingManifest.to_dispatch_request`). The
    rules are sourced from the single
    :data:`quant_foundry.training_manifest.MODE_RULES` table.

    Production mode fails closed. The per-field semantics at the
    dispatch boundary are:

    - ``gpu_required``: must be ``"1"`` (missing or ``"0"`` → fail).
    - ``allow_cpu_fallback``: must NOT be ``"1"`` (missing is permissive,
      ``"0"`` is OK, ``"1"`` → fail).
    - ``quality_policy_id``: must be present and non-empty (missing or
      ``""`` → fail).
    - ``artifact_verification_required``: must be ``"1"`` (missing or
      ``"0"`` → fail).
    - ``dataset_manifest_ref``: must not be a raw CSV/parquet path.

    **Local training is not an acceptance substitute** — a production
    request that would run on the CPU trainer must be rejected here,
    before any work begins.

    Canary and research modes are permissive and never raise.

    Returns the resolved :class:`TrainingMode` so the caller can route
    the request (e.g. skip GPU scheduling for canary/research).

    Raises:
        ModeValidationError: if the request violates its mode's rules.
    """
    mode = _resolve_mode(req)
    errors: list[str] = []

    if mode == TrainingMode.PRODUCTION:
        ec = req.extra_constraints
        if ec.get("gpu_required") != "1":
            errors.append(
                "production mode requires gpu_required=1 "
                "(local CPU training is not an acceptance substitute)"
            )
        # allow_cpu_fallback: only an explicit "1" is a violation.
        # Missing is permissive (treated as "not enabled").
        if ec.get("allow_cpu_fallback") == "1":
            errors.append(
                "production mode requires allow_cpu_fallback=0 "
                "(no CPU fallback for production runs)"
            )
        if not ec.get("quality_policy_id"):
            errors.append(
                "production mode requires a quality_policy_id "
                "(quality gates are mandatory)"
            )
        if ec.get("artifact_verification_required") != "1":
            errors.append(
                "production mode requires artifact_verification_required=1 "
                "(artifact hash verification is mandatory)"
            )
        ref = req.dataset_manifest_ref or ""
        low = ref.lower()
        if (
            low.endswith((".csv", ".parquet", ".csv.gz", ".parquet.gz"))
            or low.startswith(("file://", "inline://"))
        ):
            errors.append(
                "production mode requires a registered dataset "
                f"reference, not a raw CSV/path: {ref!r}"
            )

    if errors:
        raise ModeValidationError(
            "production mode validation failed: " + "; ".join(errors)
        )
    return mode


# --- result ----------------------------------------------------------------


@dataclass(frozen=True)
class TrainingResult:
    """Result of a successful training run."""

    callback_payload: bytes  # JSON-encoded RunPodCallbackEnvelope
    callback_signature: str  # HMAC signature over callback_payload
    callback_ts: int  # unix seconds used in the signature
    artifact_id: str
    dossier_id: str


# --- typed callback contract (Phase 1 / T-1.4) -----------------------------
#
# A typed, tamper-evident callback result contract. This replaces the
# previous opaque ``RunPodCallbackEnvelope`` payload with an explicit,
# schema-versioned, HMAC-signed contract that binds together:
#
# - the primary + auxiliary artifacts (T-1.1 TypedArtifactResult dicts),
# - the dataset + training manifest hashes,
# - the runtime fingerprint hash (T-4.1 gpu_healthcheck),
# - the training metrics summary,
# - the quality gate result (T-3.3),
# - the GPU healthcheck result (T-4.1),
# - the promotion eligibility flag (mode-aware),
# - the failure code/reason (for failure callbacks),
# - the callback signature + timestamp.
#
# Design rules (matching the codebase Pydantic-v2 conventions):
# - ``frozen=True`` + ``extra='forbid'`` so the contract is immutable and
#   rejects unknown fields (audit integrity / fail-closed).
# - The HMAC covers the canonical JSON of every field EXCEPT
#   ``callback_signature`` (the signature cannot sign itself).
# - ``callback_timestamp_ns`` is a nanosecond epoch timestamp captured at
#   signing time and included in the signed payload (replay protection).
# - Required fields are non-optional; optional fields default to ``None``
#   or empty containers. ``validate_callback`` fails closed when a
#   required field is missing.


class CallbackValidationError(ValueError):
    """Raised when a callback fails trusted-side validation (fail-closed).

    Subclass of :class:`ValueError` so existing ``except ValueError``
    handlers keep catching it. Carries a machine-readable ``code`` and a
    human-readable ``message`` plus the list of missing/invalid fields
    so the trusted-side verifier can record a precise rejection reason.

    Attributes:
        code: short machine-readable error code
            (``missing_required_fields``, ``signature_mismatch``,
            ``schema_validation_failed``).
        fields: the specific fields that failed (when applicable).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


# Required fields for a RunPodTrainingCallback (per acceptance criterion 3:
# a callback with missing required fields is rejected by the trusted side).
# Optional fields (failure_code, failure_reason, quality_gate_result,
# gpu_healthcheck, primary_artifact, auxiliary_artifacts) are NOT in this
# set — they may be absent on a success-only or minimal callback.
_CALLBACK_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "job_id",
    "training_manifest_hash",
    "dataset_manifest_hash",
    "runtime_fingerprint_hash",
    "metrics_summary",
    "promotion_eligible",
    "callback_signature",
    "callback_timestamp_ns",
)


class RunPodTrainingCallback(BaseModel):
    """Typed, HMAC-signed callback result contract (Phase 1 / T-1.4).

    Frozen + ``extra='forbid'`` (audit integrity). The
    ``callback_signature`` field carries the HMAC-SHA256 over the
    canonical JSON of every other field (see :func:`build_callback`).
    The trusted side verifies the signature via
    :func:`verify_callback` / :func:`validate_callback` before trusting
    any field (fail-closed).

    Fields:
        schema_version: callback schema version (e.g. ``"1.0"``).
        job_id: the training job id (binds the callback to one job).
        training_manifest_hash: SHA-256 of the training manifest content.
        dataset_manifest_hash: SHA-256 of the dataset manifest reference.
        runtime_fingerprint_hash: SHA-256 of the runtime fingerprint
            (git sha + lockfile hash + container digest) — binds the
            callback to a specific worker image.
        primary_artifact: the main model artifact (a
            :class:`~quant_foundry.real_trainer.TypedArtifactResult`
            serialized to a dict). ``None`` only on failure callbacks.
        auxiliary_artifacts: additional artifacts (calibration, feature
            importance, etc.) as a tuple of TypedArtifactResult dicts.
        metrics_summary: training metrics (accuracy, logloss, etc.).
        promotion_eligible: whether the result is eligible for promotion
            (mode-aware: canary=False, production=True only if all
            gates pass).
        failure_code: machine-readable failure code (failure callbacks).
        failure_reason: human-readable failure reason (failure callbacks).
        quality_gate_result: serialized :class:`QualityGateResult`
            (from T-3.3 QualityGateRunner). ``None`` when no gate ran.
        gpu_healthcheck: serialized :class:`GPUHealthcheckResult`
            (from T-4.1). ``None`` when no healthcheck ran.
        callback_signature: HMAC-SHA256 hex over the canonical JSON of
            all fields except this one.
        callback_timestamp_ns: nanosecond epoch timestamp at signing
            time (included in the signed payload for replay protection).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0"
    job_id: str
    training_manifest_hash: str
    dataset_manifest_hash: str
    runtime_fingerprint_hash: str
    primary_artifact: dict[str, Any] | None = None
    auxiliary_artifacts: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    metrics_summary: dict[str, Any] = Field(default_factory=dict)
    promotion_eligible: bool = False
    failure_code: str | None = None
    failure_reason: str | None = None
    quality_gate_result: dict[str, Any] | None = None
    gpu_healthcheck: dict[str, Any] | None = None
    callback_signature: str = ""
    callback_timestamp_ns: int = 0

    @field_validator("job_id")
    @classmethod
    def _job_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("job_id must be non-empty")
        return v

    @field_validator("schema_version")
    @classmethod
    def _schema_version_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("schema_version must be non-empty")
        return v


def _canonical_callback_payload(callback: RunPodTrainingCallback) -> bytes:
    """Return the canonical JSON bytes of a callback EXCLUDING the signature.

    The HMAC is computed over this canonical form so the signature cannot
    sign itself. Keys are sorted for determinism and ``None`` values are
    preserved (they are part of the contract). The
    ``callback_signature`` field is always excluded.
    """
    data = callback.model_dump()
    data.pop("callback_signature", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _runtime_fingerprint_hash(runtime_fingerprint: dict[str, str]) -> str:
    """Compute a stable SHA-256 over a runtime fingerprint dict.

    The runtime fingerprint (from T-4.1) carries the git sha, lockfile
    hash, container digest, and hostname. We hash the canonical JSON so
    a single ``runtime_fingerprint_hash`` field can bind the callback to
    a specific worker image without embedding the full fingerprint in
    the signed payload (the full fingerprint travels in
    ``gpu_healthcheck``).
    """
    canonical = json.dumps(
        runtime_fingerprint, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_callback(
    *,
    job_id: str,
    training_manifest_hash: str,
    dataset_manifest_hash: str,
    runtime_fingerprint: dict[str, str],
    primary_artifact: dict[str, Any] | None,
    auxiliary_artifacts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    metrics_summary: dict[str, Any] | None = None,
    mode: TrainingMode = TrainingMode.RESEARCH,
    quality_gate_result: dict[str, Any] | None = None,
    gpu_healthcheck: dict[str, Any] | None = None,
    quality_gate_passed: bool | None = None,
    failure_code: str | None = None,
    failure_reason: str | None = None,
    secret: str,
    schema_version: str = "1.0",
) -> RunPodTrainingCallback:
    """Build a signed :class:`RunPodTrainingCallback`.

    Constructs the callback model with all fields, computes the HMAC over
    the canonical JSON of every field except ``callback_signature``, and
    returns the signed (frozen) callback.

    Promotion eligibility is mode-aware (per MODE_RULES):
    - ``canary``: always ``False`` (canary is never promotion eligible).
    - ``research``: ``False`` by default (research is permissive but not
      promotion-eligible unless escalated).
    - ``production``: ``True`` only if all gates pass
      (``quality_gate_passed is True`` and no ``failure_code``).

    Args:
        job_id: the training job id.
        training_manifest_hash: SHA-256 of the training manifest content.
        dataset_manifest_hash: SHA-256 of the dataset manifest reference.
        runtime_fingerprint: dict with git sha / lockfile hash / container
            digest (from T-4.1). Hashed into ``runtime_fingerprint_hash``.
        primary_artifact: the main model artifact dict
            (serialized TypedArtifactResult). ``None`` for failure callbacks.
        auxiliary_artifacts: additional artifact dicts.
        metrics_summary: training metrics dict.
        mode: the training mode (controls promotion_eligible).
        quality_gate_result: serialized QualityGateResult (T-3.3).
        gpu_healthcheck: serialized GPUHealthcheckResult (T-4.1).
        quality_gate_passed: whether the quality gate passed. When
            ``None``, inferred from ``quality_gate_result["passed"]`` if
            present, else treated as not-passed (fail-closed).
        failure_code: machine-readable failure code (failure callbacks).
        failure_reason: human-readable failure reason.
        secret: HMAC secret for signing the callback.
        schema_version: callback schema version (default ``"1.0"``).

    Returns:
        A frozen, signed :class:`RunPodTrainingCallback`.
    """
    aux: tuple[dict[str, Any], ...] = (
        tuple(auxiliary_artifacts) if auxiliary_artifacts else ()
    )
    metrics: dict[str, Any] = metrics_summary or {}

    # Resolve quality gate passed flag (fail-closed when unknown).
    if quality_gate_passed is None and quality_gate_result is not None:
        quality_gate_passed = bool(quality_gate_result.get("passed", False))
    gates_ok = bool(quality_gate_passed) if quality_gate_passed is not None else False

    # Mode-aware promotion eligibility.
    rules = MODE_RULES.get(mode, {})
    promotion_default = bool(rules.get("promotion_eligible_default", False))
    if failure_code is not None:
        # A failure callback is never promotion eligible.
        promotion_eligible = False
    elif mode == TrainingMode.CANARY:
        # Canary is never promotion eligible (even if gates pass).
        promotion_eligible = False
    elif mode == TrainingMode.PRODUCTION:
        # Production is promotion eligible ONLY if all gates pass.
        # ``promotion_eligible_default`` is False (the pre-gate default);
        # gates passing is what flips it to True.
        promotion_eligible = gates_ok
    else:
        # Research: permissive but not promotion eligible by default
        # (escalation is a separate, explicit operator action).
        promotion_eligible = promotion_default

    rt_hash = _runtime_fingerprint_hash(runtime_fingerprint)
    ts_ns = time.time_ns()

    # Build the callback WITHOUT the signature first so we can compute
    # the HMAC over the canonical payload. We use model_construct to
    # bypass validation (the signature is empty at this point) then
    # re-validate after signing.
    unsigned = RunPodTrainingCallback.model_construct(
        schema_version=schema_version,
        job_id=job_id,
        training_manifest_hash=training_manifest_hash,
        dataset_manifest_hash=dataset_manifest_hash,
        runtime_fingerprint_hash=rt_hash,
        primary_artifact=primary_artifact,
        auxiliary_artifacts=aux,
        metrics_summary=metrics,
        promotion_eligible=promotion_eligible,
        failure_code=failure_code,
        failure_reason=failure_reason,
        quality_gate_result=quality_gate_result,
        gpu_healthcheck=gpu_healthcheck,
        callback_signature="",
        callback_timestamp_ns=ts_ns,
    )
    canonical = _canonical_callback_payload(unsigned)
    signature = hmac.new(
        secret.encode("utf-8"), canonical, hashlib.sha256,
    ).hexdigest()
    return RunPodTrainingCallback(
        schema_version=schema_version,
        job_id=job_id,
        training_manifest_hash=training_manifest_hash,
        dataset_manifest_hash=dataset_manifest_hash,
        runtime_fingerprint_hash=rt_hash,
        primary_artifact=primary_artifact,
        auxiliary_artifacts=aux,
        metrics_summary=metrics,
        promotion_eligible=promotion_eligible,
        failure_code=failure_code,
        failure_reason=failure_reason,
        quality_gate_result=quality_gate_result,
        gpu_healthcheck=gpu_healthcheck,
        callback_signature=signature,
        callback_timestamp_ns=ts_ns,
    )


def verify_callback(
    callback: RunPodTrainingCallback | dict[str, Any],
    *,
    secret: str,
) -> bool:
    """Verify the HMAC signature of a :class:`RunPodTrainingCallback`.

    Recomputes the HMAC over the canonical JSON of every field except
    ``callback_signature`` and compares it to the stored signature using
    :func:`hmac.compare_digest` (constant-time). Returns ``True`` if the
    signature matches, ``False`` otherwise (fail-closed — never raises
    on a signature mismatch).

    Args:
        callback: a :class:`RunPodTrainingCallback` or a dict that can be
            parsed into one.
        secret: the HMAC secret used at signing time.

    Returns:
        ``True`` if the signature is valid, ``False`` otherwise.
    """
    if isinstance(callback, dict):
        try:
            callback = RunPodTrainingCallback.model_validate(callback)
        except Exception:
            return False
    elif not isinstance(callback, RunPodTrainingCallback):
        return False
    if not isinstance(secret, str) or not secret:
        return False
    if not callback.callback_signature:
        return False
    canonical = _canonical_callback_payload(callback)
    expected = hmac.new(
        secret.encode("utf-8"), canonical, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, callback.callback_signature)


def validate_callback(
    callback: RunPodTrainingCallback | dict[str, Any],
    *,
    secret: str,
) -> RunPodTrainingCallback:
    """Validate a callback (required fields + HMAC) — fail-closed.

    1. Parses the callback into a :class:`RunPodTrainingCallback` (rejects
       unknown fields via ``extra='forbid'``).
    2. Checks all required fields are present (per
       :data:`_CALLBACK_REQUIRED_FIELDS`).
    3. Verifies the HMAC signature via :func:`verify_callback`.

    Returns the validated callback on success. Raises
    :class:`CallbackValidationError` on any failure (fail-closed).

    Args:
        callback: a :class:`RunPodTrainingCallback` or a dict.
        secret: the HMAC secret used at signing time.

    Raises:
        CallbackValidationError: if schema validation fails, required
            fields are missing, or the signature does not verify.
    """
    # 1. Schema validation (extra='forbid' rejects unknown fields).
    if isinstance(callback, RunPodTrainingCallback):
        model = callback
    elif isinstance(callback, dict):
        try:
            model = RunPodTrainingCallback.model_validate(callback)
        except Exception as exc:
            raise CallbackValidationError(
                "schema_validation_failed",
                f"callback failed schema validation: {exc}",
            ) from exc
    else:
        raise CallbackValidationError(
            "schema_validation_failed",
            f"callback must be a dict or RunPodTrainingCallback, got "
            f"{type(callback).__name__}",
        )

    # 2. Required fields check (fail-closed).
    missing: list[str] = []
    for fname in _CALLBACK_REQUIRED_FIELDS:
        val = getattr(model, fname, None)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(fname)
    if missing:
        raise CallbackValidationError(
            "missing_required_fields",
            f"callback missing required fields: {missing}",
            fields=tuple(missing),
        )

    # 3. HMAC signature verification.
    if not verify_callback(model, secret=secret):
        raise CallbackValidationError(
            "signature_mismatch",
            "callback signature verification failed (HMAC mismatch)",
            fields=("callback_signature",),
        )

    return model


# --- local trainer ---------------------------------------------------------


@dataclass
class LocalTrainer:
    """CPU-only deterministic trainer. No GPU, no sklearn dependency.

    Produces a deterministic "model" (a stub) whose artifact hash is derived
    from the request inputs (seed + model_family + dataset_manifest_ref +
    search_space). This proves the contract end-to-end without ML deps.

    Args:
        should_fail: if True, raise TrainingFailure on every train() call
            (used to test the failure path).
    """

    should_fail: bool = False

    def train(
        self,
        req: RunPodTrainingRequest,
        *,
        deadline_ns: int,
    ) -> tuple[ArtifactManifest, ModelDossier]:
        """Train a tiny baseline. Returns (artifact_manifest, dossier).

        Raises TrainingFailure on deadline breach or if should_fail is set.
        """
        if self.should_fail:
            raise TrainingFailure(
                error_code="training_error",
                error_summary="local trainer injected failure (should_fail=True)",
            )
        # Deadline check (enforced before any work).
        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached before work started",
            )

        # Deterministic artifact hash from the request inputs. We sort keys
        # explicitly for canonical bytes (frozen pydantic models don't sort
        # by default in model_dump_json).
        canonical = json.dumps(
            {
                "schema_version": req.schema_version,
                "job_id": req.job_id,
                "dataset_manifest_ref": req.dataset_manifest_ref,
                "model_family": req.model_family,
                "search_space": req.search_space,
                "random_seed": req.random_seed,
                "hardware_class": req.hardware_class,
                "extra_constraints": req.extra_constraints,
            },
            sort_keys=True,
        ).encode("utf-8")
        payload_hash = hashlib.sha256(canonical).hexdigest()

        now_ns = time.time_ns()
        seed = req.random_seed if req.random_seed is not None else 0
        seed_hex = (seed * 2654435761) & 0xFFFFFFFF
        artifact_id = f"artifact:{payload_hash[:16]}"
        artifact = ArtifactManifest(
            artifact_id=artifact_id,
            sha256=payload_hash,
            size_bytes=2048 + (seed_hex % 8192),
            uri=None,
            model_family=req.model_family,
            created_at_ns=now_ns,
            feature_schema_hash=payload_hash[:16],
            label_schema_hash=payload_hash[16:32],
            code_git_sha=_git_sha_or_default(),
            lockfile_hash=_lockfile_hash_or_default(),
            container_image_digest=_container_digest_or_default(),
        )
        pbo = (seed_hex % 100) / 100.0
        deflated_sharpe = ((seed_hex >> 8) % 300) / 100.0 - 1.0
        dossier = ModelDossier(
            model_id=f"model:{req.job_id}",
            artifact_manifest_id=artifact.artifact_id,
            dataset_manifest_id=req.dataset_manifest_ref,
            code_git_sha=artifact.code_git_sha or "unknown",
            lockfile_hash=artifact.lockfile_hash or "unknown",
            container_image_digest=artifact.container_image_digest or "unknown",
            random_seed=req.random_seed,
            hardware_class=req.hardware_class,
            training_metrics={
                "accuracy": 0.5 + (pbo / 2.0),
                "logloss": 0.7 - (pbo / 4.0),
            },
            pbo=pbo,
            deflated_sharpe=deflated_sharpe,
            authority=Authority.SHADOW_ONLY,
            metadata={"model_family": req.model_family},
        )
        return artifact, dossier


# --- handler ---------------------------------------------------------------


class TrainerProtocol(Protocol):
    """Protocol for trainers injectable into ``RunPodTrainingHandler``.

    Both ``LocalTrainer`` and ``RealLightGBMTrainer`` satisfy this protocol.
    """

    def train(
        self,
        req: RunPodTrainingRequest,
        *,
        deadline_ns: int,
    ) -> tuple[ArtifactManifest, ModelDossier]: ...


_DEFAULT_DEADLINE_SECONDS = 600  # 10 min default


@dataclass
class RunPodTrainingHandler:
    """RunPod training worker handler. Same contract as the mock dispatcher.

    This handler runs inside the RunPod container (or locally for tests).
    It has NO broker/Redis/stream access — it is a pure function over its
    inputs that produces a signed callback envelope.

    Args:
        callback_secret: HMAC secret for signing the callback.
        trainer: the LocalTrainer (or injected fake). Defaults to LocalTrainer().
        deadline_seconds: max wall-clock seconds for the training run.
            0 means "immediate timeout" (used to test the deadline path).
        worker_id: identifier for this worker instance.
    """

    callback_secret: str
    trainer: TrainerProtocol = field(default_factory=LocalTrainer)
    deadline_seconds: int = _DEFAULT_DEADLINE_SECONDS
    worker_id: str = "runpod-worker-1"

    def handle(self, req: RunPodTrainingRequest) -> TrainingResult:
        """Train a model and return a signed callback.

        Raises TrainingFailure on deadline breach or training error.
        """
        start_ns = time.time_ns()
        deadline_ns = start_ns + (self.deadline_seconds * 1_000_000_000)

        # Deadline check (enforced before any work). Use >= so a 0-second
        # deadline fails immediately (deadline_ns == start_ns).
        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary=(
                    f"training deadline breached (deadline_seconds={self.deadline_seconds})"
                ),
            )

        # Train (deterministic, CPU-only).
        artifact, dossier = self.trainer.train(req, deadline_ns=deadline_ns)

        # Build the signed callback envelope (same shape as mock dispatcher).
        now_ns = time.time_ns()
        envelope = RunPodCallbackEnvelope(
            job_id=req.job_id,
            worker_id=self.worker_id,
            result_type="training_complete",
            payload={
                "model_family": req.model_family,
                "dossier": dossier.model_dump(),
                "artifact_manifest": artifact.model_dump(),
            },
            received_at_ns=now_ns,
        )
        envelope_bytes = envelope.model_dump_json().encode("utf-8")

        # Sign the callback (real HMAC path, same as mock dispatcher).
        ts = int(time.time())
        signature = sign_callback(
            envelope_bytes,
            secret=self.callback_secret,
            ts=ts,
            job_id=req.job_id,
        )

        return TrainingResult(
            callback_payload=envelope_bytes,
            callback_signature=signature,
            callback_ts=ts,
            artifact_id=artifact.artifact_id,
            dossier_id=dossier.model_id,
        )


# --- reproducibility pin helpers -------------------------------------------


def _git_sha_or_default() -> str | None:
    """Return the current git SHA, or None if not in a git repo.

    In the RunPod container, the code git SHA is pinned at build time.
    For local tests, we return a deterministic default.
    """
    # We don't shell out here (no subprocess in the handler — keeps it pure).
    # The container build pins the real SHA; tests use the default.
    return "local-git-sha"


def _lockfile_hash_or_default() -> str | None:
    """Return the lockfile hash, or None. Pinned at container build time."""
    return "local-lockfile-hash"


def _container_digest_or_default() -> str | None:
    """Return the container image digest, or None. Set at build time."""
    return "local-container-digest"
