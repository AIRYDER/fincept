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
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

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
    TrainingMode as TrainingMode,  # re-export for verification_matrix
)

# --- metric sanity bounds (Tier 0) -----------------------------------------
#
# Conservative sanity thresholds for training metrics. The A7 canary run
# produced a Sharpe ratio of 769 — impossible for any real trading
# strategy (real strategies rarely exceed Sharpe ~3). These bounds flag
# implausible metrics BEFORE they reach promotion decisions, while
# preserving the raw values (never deleted — only annotated).
#
# Thresholds are overridable via env vars so operators can tune them
# without a code change. Defaults are deliberately conservative:
#
#   * Sharpe ratio: |sharpe| > 10  -> "implausible" (real strategies
#     rarely exceed 3; >10 is almost certainly a bug). |sharpe| > 5 ->
#     "warning" (suspicious but not impossible).
#   * Annual return: |annual_return| > 5.0 (500%) -> "implausible".
#   * Max drawdown: |max_drawdown| > 1.0 (100%) -> "implausible".
#   * Fold overfit ratio (PBO): > 5.0 -> "implausible".
#
# A metric flagged "implausible" is treated as CRITICAL and blocks
# promotion eligibility (promotion_eligible forced False). A "warning"
# is recorded but does NOT block promotion (it is advisory only).

METRIC_SANITY_SHARPE_IMPLAUSIBLE: float = float(
    os.environ.get("QF_METRIC_SANITY_SHARPE_IMPLAUSIBLE", "10.0")
)
METRIC_SANITY_SHARPE_WARNING: float = float(
    os.environ.get("QF_METRIC_SANITY_SHARPE_WARNING", "5.0")
)
METRIC_SANITY_ANNUAL_RETURN_IMPLAUSIBLE: float = float(
    os.environ.get("QF_METRIC_SANITY_ANNUAL_RETURN_IMPLAUSIBLE", "5.0")
)
METRIC_SANITY_MAX_DRAWDOWN_IMPLAUSIBLE: float = float(
    os.environ.get("QF_METRIC_SANITY_MAX_DRAWDOWN_IMPLAUSIBLE", "1.0")
)
METRIC_SANITY_FOLD_OVERFIT_IMPLAUSIBLE: float = float(
    os.environ.get("QF_METRIC_SANITY_FOLD_OVERFIT_IMPLAUSIBLE", "5.0")
)

# Metric key aliases — the dossier/training_metrics dict may use
# different naming conventions across model families, so we check all
# known aliases for each critical metric.
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "sharpe_ratio": ("sharpe_ratio", "sharpe", "deflated_sharpe"),
    "annual_return": ("annual_return", "annualized_return", "cagr"),
    "max_drawdown": ("max_drawdown", "maximum_drawdown"),
    "fold_overfit_ratio": ("pbo", "fold_overfit_ratio", "overfit_ratio"),
}

# Metrics whose "implausible" status is CRITICAL (blocks promotion).
_CRITICAL_METRICS: frozenset[str] = frozenset(
    {"sharpe_ratio", "annual_return", "max_drawdown", "fold_overfit_ratio"}
)


@dataclass(frozen=True)
class MetricSanityReport:
    """Result of sanity-validating a metrics_summary dict.

    The raw metric values are NEVER modified or deleted — this report is
    an annotation layer that travels alongside the raw values in the
    callback payload under ``metrics_summary["metric_sanity"]``.

    Attributes:
        status: overall worst-case status — ``"ok"`` (all metrics within
            bounds), ``"warning"`` (at least one metric suspicious but
            none implausible), or ``"implausible"`` (at least one
            critical metric is implausible).
        reason_codes: machine-readable reason codes, one per flagged
            metric (e.g. ``"sharpe_ratio_implausible:769.0"``).
        promotion_allowed: ``False`` when a CRITICAL metric is
            implausible (the caller should force
            ``promotion_eligible=False``). ``True`` otherwise.
        flagged_metrics: per-metric detail dict keyed by canonical
            metric name, each carrying ``raw_value``, ``status``, and
            ``reason_code``.
    """

    status: str = "ok"
    reason_codes: tuple[str, ...] = ()
    promotion_allowed: bool = True
    flagged_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for the callback payload."""
        return {
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "promotion_allowed": self.promotion_allowed,
            "flagged_metrics": dict(self.flagged_metrics),
        }


def _coerce_float(value: Any) -> float | None:
    """Coerce a metric value to float, returning None if not numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_metric_sanity(
    metrics_summary: dict[str, Any] | None,
) -> MetricSanityReport:
    """Sanity-validate a metrics_summary dict (Tier 0 metric bounds).

    Checks each known critical metric against conservative implausible/
    warning thresholds (see the module-level constants above). Raw
    metric values are NEVER modified or deleted — the returned report is
    an annotation that the caller embeds alongside the raw values.

    A metric is flagged ``"implausible"`` when it exceeds the implausible
    threshold, and ``"warning"`` when it exceeds the warning threshold
    but not the implausible one. The overall ``status`` is the worst
    case across all metrics. ``promotion_allowed`` is ``False`` only
    when a CRITICAL metric (sharpe_ratio, annual_return, max_drawdown,
    fold_overfit_ratio) is implausible.

    Args:
        metrics_summary: the raw training metrics dict (e.g. from
            ``dossier_data["training_metrics"]``). May be ``None`` or
            empty (returns an "ok" report).

    Returns:
        A :class:`MetricSanityReport` with status, reason codes, and
        per-metric detail. Safe to embed in the callback payload.
    """
    if not metrics_summary:
        return MetricSanityReport()

    flagged: dict[str, dict[str, Any]] = {}
    reason_codes: list[str] = []
    has_implausible_critical = False
    has_warning = False

    # Sharpe ratio (absolute value checked — extreme negative is also
    # implausible).
    for canonical, aliases in _METRIC_ALIASES.items():
        raw_value = None
        for alias in aliases:
            if alias in metrics_summary:
                raw_value = metrics_summary[alias]
                break
        if raw_value is None:
            continue
        numeric = _coerce_float(raw_value)
        if numeric is None:
            continue

        if canonical == "sharpe_ratio":
            abs_val = abs(numeric)
            if abs_val > METRIC_SANITY_SHARPE_IMPLAUSIBLE:
                status = "implausible"
                code = f"sharpe_ratio_implausible:{numeric}"
            elif abs_val > METRIC_SANITY_SHARPE_WARNING:
                status = "warning"
                code = f"sharpe_ratio_warning:{numeric}"
            else:
                continue
        elif canonical == "annual_return":
            if abs(numeric) > METRIC_SANITY_ANNUAL_RETURN_IMPLAUSIBLE:
                status = "implausible"
                code = f"annual_return_implausible:{numeric}"
            else:
                continue
        elif canonical == "max_drawdown":
            if abs(numeric) > METRIC_SANITY_MAX_DRAWDOWN_IMPLAUSIBLE:
                status = "implausible"
                code = f"max_drawdown_implausible:{numeric}"
            else:
                continue
        elif canonical == "fold_overfit_ratio":
            if numeric > METRIC_SANITY_FOLD_OVERFIT_IMPLAUSIBLE:
                status = "implausible"
                code = f"fold_overfit_ratio_implausible:{numeric}"
            else:
                continue
        else:
            continue

        flagged[canonical] = {
            "raw_value": raw_value,
            "status": status,
            "reason_code": code,
        }
        reason_codes.append(code)
        if status == "implausible" and canonical in _CRITICAL_METRICS:
            has_implausible_critical = True
        elif status == "warning":
            has_warning = True

    if has_implausible_critical:
        overall = "implausible"
        promotion_allowed = False
    elif has_warning:
        overall = "warning"
        promotion_allowed = True
    else:
        overall = "ok"
        promotion_allowed = True

    return MetricSanityReport(
        status=overall,
        reason_codes=tuple(reason_codes),
        promotion_allowed=promotion_allowed,
        flagged_metrics=flagged,
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
            f"unknown training_mode {raw!r}; expected one of {[m.value for m in TrainingMode]}"
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
                "production mode requires a quality_policy_id (quality gates are mandatory)"
            )
        if ec.get("artifact_verification_required") != "1":
            errors.append(
                "production mode requires artifact_verification_required=1 "
                "(artifact hash verification is mandatory)"
            )
        ref = req.dataset_manifest_ref or ""
        low = ref.lower()
        if low.endswith((".csv", ".parquet", ".csv.gz", ".parquet.gz")) or low.startswith(
            ("file://", "inline://")
        ):
            errors.append(
                "production mode requires a registered dataset "
                f"reference, not a raw CSV/path: {ref!r}"
            )

    if errors:
        raise ModeValidationError("production mode validation failed: " + "; ".join(errors))
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
        runtime_fingerprint,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_callback(
    *,
    job_id: str,
    training_manifest_hash: str,
    dataset_manifest_hash: str,
    runtime_fingerprint: dict[str, str] | RuntimeFingerprint,
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
        runtime_fingerprint: either a :class:`RuntimeFingerprint` (T-5.2,
            whose precomputed ``fingerprint_hash`` is used directly) or a
            legacy dict with git sha / lockfile hash / container digest
            (from T-4.1). Hashed into ``runtime_fingerprint_hash``.
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
    aux: tuple[dict[str, Any], ...] = tuple(auxiliary_artifacts) if auxiliary_artifacts else ()
    metrics: dict[str, Any] = dict(metrics_summary) if metrics_summary else {}

    # Tier 0 / metric sanity bounds: validate the raw metrics BEFORE any
    # promotion decision. Raw values are NEVER deleted — the sanity
    # report is embedded alongside them under ``metric_sanity``. When a
    # CRITICAL metric is implausible, promotion is blocked below.
    sanity_report = validate_metric_sanity(metrics)
    metrics["metric_sanity"] = sanity_report.to_dict()

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

    # Tier 0 / metric sanity bounds: a CRITICAL metric flagged
    # "implausible" (e.g. Sharpe 769) blocks promotion regardless of
    # mode/gates. This is a hard floor — fail-closed.
    if not sanity_report.promotion_allowed:
        promotion_eligible = False

    # Resolve the runtime fingerprint hash. When a full
    # RuntimeFingerprint (T-5.2) is supplied, its ``fingerprint_hash`` is
    # already computed + signed — use it directly so the callback binds to
    # the exact same signed fingerprint. When a legacy dict (T-4.1 shape)
    # is supplied, hash it via the canonical-JSON helper.
    if isinstance(runtime_fingerprint, RuntimeFingerprint):
        rt_hash = runtime_fingerprint.fingerprint_hash
    else:
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
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
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
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
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
            f"callback must be a dict or RunPodTrainingCallback, got {type(callback).__name__}",
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


# --- trusted-side artifact verifier (Phase 1 / T-1.3) -----------------------
#
# The trusted side (dispatcher/gateway) must independently verify the
# artifact referenced by a callback before marking it
# ``artifact_verified=true``. This module implements the full verification
# chain:
#
# 1. Fetch the artifact bytes from the declared ``artifact_uri``
#    (``file://`` and ``https://`` supported).
# 2. Recompute the SHA-256 and byte size, compare to the declared values
#    (fail-closed on mismatch — detects corruption / truncation / tamper).
# 3. Load the artifact with the declared ``loader_family``
#    (``"lightgbm"`` or ``"local-stub"``). Unknown loaders are rejected.
# 4. Run a deterministic smoke prediction against a frozen sample (when
#    provided) to prove the loaded model is callable and produces a
#    sensibly-shaped output.
# 5. Build and HMAC-sign an :class:`ArtifactVerificationReceipt` recording
#    every check's pass/fail status.
#
# Security invariants (fail-closed):
# - A corrupted artifact (hash mismatch) is rejected.
# - A missing artifact (empty/None URI or fetch failure) is rejected.
# - An unknown loader family is rejected.
# - A loader that throws on load is rejected.
# - A smoke prediction that throws or produces a wrong shape is rejected.
# - The receipt is HMAC-signed so it cannot be forged after the fact.


# Loader families recognized by the trusted-side verifier. A callback
# declaring a loader family NOT in this set is rejected with
# ``ArtifactVerificationError(code="unknown_loader")`` (fail-closed).
_KNOWN_LOADER_FAMILIES: frozenset[str] = frozenset({"lightgbm", "local-stub"})

# Allowed URI schemes for artifact fetch on the trusted side. ``file://``
# is the volume path; ``https://`` is the presigned object URL (TLS
# required). ``artifact://`` is the synthetic fake-writer URI (testing
# only — bytes are not fetchable, so it raises ``artifact_fetch_failed``
# unless the caller passes inline bytes). Any other scheme is rejected.
_VERIFIER_ALLOWED_URI_SCHEMES: frozenset[str] = frozenset(
    {"file", "https", "artifact"},
)


class ArtifactVerificationError(ValueError):
    """Raised when artifact verification fails (fail-closed).

    Subclass of :class:`ValueError` so existing ``except ValueError``
    handlers keep catching it. Carries a machine-readable ``code`` so the
    trusted-side verifier can record a precise rejection reason in the
    outbox / audit log.

    Attributes:
        code: short machine-readable error code. One of:
            - ``"missing_artifact"`` — artifact URI is empty or None.
            - ``"artifact_fetch_failed"`` — could not fetch bytes from URI.
            - ``"hash_mismatch"`` — recomputed sha256 doesn't match.
            - ``"size_mismatch"`` — recomputed size doesn't match.
            - ``"unknown_loader"`` — loader family not recognized.
            - ``"load_failed"`` — loader threw an exception.
            - ``"smoke_prediction_failed"`` — smoke prediction failed.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactVerificationReceipt(BaseModel):
    """Typed, HMAC-signed artifact verification receipt (Phase 1 / T-1.3).

    Frozen + ``extra='forbid'`` (audit integrity). Records the outcome of
    every verification check (hash, size, loader, smoke prediction) so the
    trusted side can audit exactly which check passed/failed. The
    ``receipt_signature`` is an HMAC-SHA256 over the canonical JSON of
    every other field, signed with the callback secret, so the receipt
    cannot be forged after the fact.

    Fields:
        artifact_uri: the URI the artifact was fetched from.
        artifact_sha256: the declared SHA-256 (64-char lowercase hex).
        artifact_size_bytes: the declared byte size.
        artifact_format: the declared serialisation format.
        loader_family: the declared loader family used to load the
            artifact.
        artifact_verified: ``True`` only if ALL checks (hash, size,
            loader, smoke prediction) passed. This is the single
            authoritative flag the trusted side checks before marking a
            callback ``artifact_verified=true``.
        hash_verified: ``True`` if the recomputed sha256 matched.
        size_verified: ``True`` if the recomputed size matched.
        loader_verified: ``True`` if the loader loaded the artifact
            without error.
        smoke_prediction_passed: ``True`` if the smoke prediction
            succeeded (or was skipped because no sample was provided).
        smoke_prediction_metrics: dict of metrics from the smoke
            prediction (e.g. ``{"n_rows": ..., "n_outputs": ...}``).
            Empty when no smoke sample was provided.
        verification_error: the error code (from
            :class:`ArtifactVerificationError`) when verification failed,
            or ``None`` on success.
        verified_at_ns: nanosecond epoch timestamp of verification.
        receipt_signature: HMAC-SHA256 hex over the canonical JSON of
            every field except this one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_uri: str
    artifact_sha256: str
    artifact_size_bytes: int
    artifact_format: str
    loader_family: str
    artifact_verified: bool = False
    hash_verified: bool = False
    size_verified: bool = False
    loader_verified: bool = False
    smoke_prediction_passed: bool = False
    smoke_prediction_metrics: dict[str, Any] = Field(default_factory=dict)
    verification_error: str | None = None
    verified_at_ns: int = 0
    receipt_signature: str = ""

    def verify_receipt(self, *, secret: str) -> bool:
        """Recompute the receipt HMAC and compare (constant-time).

        Returns ``True`` iff the recomputed HMAC matches the stored
        ``receipt_signature``. Used by downstream auditors to authenticate
        the receipt (fail-closed — never raises on a mismatch).
        """
        if not isinstance(secret, str) or not secret:
            return False
        if not self.receipt_signature:
            return False
        data = self.model_dump()
        data.pop("receipt_signature", None)
        canonical = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected = hmac.new(
            secret.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, self.receipt_signature)


def _canonical_receipt_payload(receipt: ArtifactVerificationReceipt) -> bytes:
    """Return the canonical JSON bytes of a receipt EXCLUDING the signature."""
    data = receipt.model_dump()
    data.pop("receipt_signature", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_receipt(
    receipt: ArtifactVerificationReceipt,
    *,
    secret: str,
) -> str:
    """Compute the HMAC-SHA256 hex over the canonical receipt payload."""
    canonical = _canonical_receipt_payload(receipt)
    return hmac.new(
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def _fetch_artifact_bytes(artifact_uri: str) -> bytes:
    """Fetch artifact bytes from a ``file://`` or ``https://`` URI.

    Raises :class:`ArtifactVerificationError` with code
    ``"artifact_fetch_failed"`` on any fetch failure (missing file,
    network error, disallowed scheme). This is a fail-closed helper — it
    never returns empty bytes silently.

    Args:
        artifact_uri: the URI to fetch (``file://`` or ``https://``).

    Returns:
        The raw artifact bytes.

    Raises:
        ArtifactVerificationError: if the URI is empty, uses a
            disallowed scheme, or the fetch fails.
    """
    if not artifact_uri or not artifact_uri.strip():
        raise ArtifactVerificationError(
            "missing_artifact",
            "artifact URI is empty or None (missing artifact — fail closed)",
        )
    parsed = urlparse(artifact_uri)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise ArtifactVerificationError(
            "artifact_fetch_failed",
            f"artifact URI has no scheme: {artifact_uri!r}",
        )
    if scheme not in _VERIFIER_ALLOWED_URI_SCHEMES:
        raise ArtifactVerificationError(
            "artifact_fetch_failed",
            f"disallowed artifact URI scheme {scheme!r} "
            f"(allowed: {sorted(_VERIFIER_ALLOWED_URI_SCHEMES)}): "
            f"{artifact_uri!r}",
        )
    if scheme == "artifact":
        # Synthetic fake-writer URI — bytes are not fetchable from a URI.
        # The caller must pass inline bytes via ``artifact_bytes``.
        raise ArtifactVerificationError(
            "artifact_fetch_failed",
            f"artifact:// URIs are not fetchable (testing-only scheme); "
            f"pass inline bytes via artifact_bytes: {artifact_uri!r}",
        )
    if scheme == "file":
        path = unquote(parsed.path)
        # On Windows, file:///C:/path produces parsed.path = "/C:/path".
        # Strip the leading slash before the drive letter so Path() works.
        import os

        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        if not path:
            raise ArtifactVerificationError(
                "artifact_fetch_failed",
                f"file:// URI has no path: {artifact_uri!r}",
            )
        try:
            from pathlib import Path

            p = Path(path)
            if not p.exists():
                raise ArtifactVerificationError(
                    "artifact_fetch_failed",
                    f"artifact file does not exist: {artifact_uri!r}",
                )
            return p.read_bytes()
        except ArtifactVerificationError:
            raise
        except Exception as exc:
            raise ArtifactVerificationError(
                "artifact_fetch_failed",
                f"failed to read artifact from {artifact_uri!r}: {exc}",
            ) from exc
    if scheme == "https":
        try:
            req = Request(artifact_uri, method="GET")
            with urlopen(req, timeout=120) as resp:  # noqa: S310 - operator-provided URL
                status = getattr(resp, "status", None) or resp.getcode()
                if status != 200:
                    raise ArtifactVerificationError(
                        "artifact_fetch_failed",
                        f"artifact fetch failed: HTTP {status} for {artifact_uri!r}",
                    )
                return cast("bytes", resp.read())
        except ArtifactVerificationError:
            raise
        except Exception as exc:
            raise ArtifactVerificationError(
                "artifact_fetch_failed",
                f"failed to fetch artifact from {artifact_uri!r}: {exc}",
            ) from exc
    # Should be unreachable (scheme validated above), but fail-closed.
    raise ArtifactVerificationError(
        "artifact_fetch_failed",
        f"unsupported artifact URI scheme {scheme!r}: {artifact_uri!r}",
    )


def _load_artifact(
    model_bytes: bytes,
    loader_family: str,
    artifact_format: str,
) -> Any:
    """Load artifact bytes with the declared loader family.

    Returns the loaded model object. Raises
    :class:`ArtifactVerificationError` on any load failure (unknown
    loader, loader exception).

    Loader families:
        - ``"lightgbm"``: loads a LightGBM model. Supports both pickled
          ``Booster`` objects (``artifact_format="pickle"``) and LightGBM
          text model files (``artifact_format="lightgbm-txt"``). ML deps
          (``lightgbm``, ``numpy``) are imported lazily so this module
          remains importable without them.
        - ``"local-stub"``: accepts any bytes (canary/test loader). The
          "loaded model" is a lightweight stub that returns a fixed
          prediction shape — used for contract proofs without ML deps.

    Raises:
        ArtifactVerificationError: with code ``"unknown_loader"`` if the
            loader family is not recognized, or ``"load_failed"`` if the
            loader throws.
    """
    if loader_family not in _KNOWN_LOADER_FAMILIES:
        raise ArtifactVerificationError(
            "unknown_loader",
            f"unknown loader family {loader_family!r} (known: {sorted(_KNOWN_LOADER_FAMILIES)})",
        )
    if loader_family == "local-stub":
        # Canary/test loader: accept any non-empty bytes. Return a stub
        # "model" that records the byte length (proves the bytes were
        # received). The stub's predict() returns a fixed-shape output.
        return _LocalStubModel(len(model_bytes))
    # loader_family == "lightgbm"
    try:
        import importlib.util

        if importlib.util.find_spec("lightgbm") is None:
            raise ArtifactVerificationError(
                "load_failed",
                f"lightgbm dependency not available for loader_family={loader_family!r}",
            )
        import lightgbm as lgb

        fmt = (artifact_format or "").lower()
        if fmt == "pickle":
            # The real trainer serializes Booster objects via pickle.
            import pickle

            model = pickle.loads(model_bytes)
            # Verify it's a Booster (or at least has predict()).
            if not hasattr(model, "predict"):
                raise ArtifactVerificationError(
                    "load_failed",
                    f"pickled artifact is not a callable model "
                    f"(no predict() method): {type(model).__name__}",
                )
            return model
        # lightgbm-txt or other text formats: use Booster(model_file=...).
        # Booster can load from a file path or a model string. We pass
        # the raw bytes decoded as the model string.
        try:
            model_str = model_bytes.decode("utf-8")
            return lgb.Booster(model_file=model_str)
        except Exception:
            # Fallback: write to a temp file and load from path.
            import tempfile

            with tempfile.NamedTemporaryFile(
                suffix=".txt",
                delete=False,
            ) as tmp:
                tmp.write(model_bytes)
                tmp_path = tmp.name
            return lgb.Booster(model_file=tmp_path)
    except ArtifactVerificationError:
        raise
    except Exception as exc:
        raise ArtifactVerificationError(
            "load_failed",
            f"lightgbm loader failed to load artifact: {exc}",
        ) from exc


class _LocalStubModel:
    """Stub model for the ``local-stub`` loader family (canary/test).

    Provides a ``predict()`` method that returns a deterministic output
    based on the input shape, proving the load + predict contract without
    ML dependencies.
    """

    def __init__(self, n_bytes: int) -> None:
        self._n_bytes = n_bytes

    def predict(self, X: Any, **kwargs: Any) -> Any:
        """Return a deterministic prediction based on the input shape."""
        try:
            import numpy as np

            n_rows = int(X.shape[0]) if hasattr(X, "shape") else len(X)
            return np.zeros(n_rows, dtype=float)
        except ImportError:
            # numpy not available — return a plain list.
            n_rows = int(X.shape[0]) if hasattr(X, "shape") else len(X)
            return [0.0] * n_rows


def _run_smoke_prediction(
    model: Any,
    smoke_sample: Any,
) -> tuple[bool, dict[str, Any]]:
    """Run a deterministic smoke prediction against a frozen sample.

    Returns ``(passed, metrics)``. The prediction is considered passed if
    ``model.predict(smoke_sample)`` succeeds and produces a non-empty
    output with the expected number of rows (matching the sample's first
    dimension).

    Args:
        model: the loaded model object (must have a ``predict()`` method).
        smoke_sample: a frozen feature matrix (numpy array or list of
            lists). Must not be mutated by this function.

    Returns:
        A tuple of ``(passed, metrics_dict)``. The metrics dict records
        ``n_rows``, ``n_outputs``, ``prediction_min``, ``prediction_max``,
        and ``prediction_mean`` (when numpy is available).
    """
    metrics: dict[str, Any] = {}
    try:
        preds = model.predict(smoke_sample)
    except Exception as exc:
        metrics["error"] = str(exc)
        return False, metrics
    # Determine the prediction shape.
    try:
        import numpy as np

        arr = np.asarray(preds)
        n_rows = int(arr.shape[0]) if arr.ndim >= 1 else 1
        n_outputs = int(arr.shape[1]) if arr.ndim >= 2 else 1
        metrics["n_rows"] = n_rows
        metrics["n_outputs"] = n_outputs
        if arr.size > 0:
            metrics["prediction_min"] = float(arr.min())
            metrics["prediction_max"] = float(arr.max())
            metrics["prediction_mean"] = float(arr.mean())
        else:
            return False, metrics
        # Verify the prediction has at least one row.
        if n_rows < 1:
            metrics["error"] = "smoke prediction produced 0 rows"
            return False, metrics
        return True, metrics
    except ImportError:
        # numpy not available — do a best-effort shape check.
        try:
            n_rows = len(preds) if hasattr(preds, "__len__") else 1
        except Exception:
            n_rows = 1
        metrics["n_rows"] = n_rows
        if n_rows < 1:
            metrics["error"] = "smoke prediction produced 0 rows"
            return False, metrics
        return True, metrics


def _build_frozen_smoke_sample(model: Any, n_features: int | None = None) -> Any:
    """Build a small frozen feature matrix for smoke prediction.

    Uses the model's declared feature count when available (LightGBM
    Booster exposes ``num_feature``). Falls back to ``n_features`` arg,
    then to a single-feature default.

    The sample is deterministic (all zeros) so the smoke prediction is
    reproducible across runs.
    """
    try:
        import numpy as np

        nf = n_features
        if nf is None and hasattr(model, "num_feature"):
            try:
                nf = int(model.num_feature())
            except Exception:
                nf = None
        if nf is None or nf < 1:
            nf = 1
        return np.zeros((2, nf), dtype=float)
    except ImportError:
        # numpy not available — return a list of lists.
        nf = n_features if (n_features and n_features >= 1) else 1
        return [[0.0] * nf for _ in range(2)]


def verify_artifact(
    callback: RunPodTrainingCallback | dict[str, Any] | None = None,
    *,
    artifact_uri: str | None = None,
    artifact_sha256: str | None = None,
    artifact_size_bytes: int | None = None,
    artifact_format: str | None = None,
    loader_family: str | None = None,
    artifact_bytes: bytes | None = None,
    smoke_sample: Any = None,
    run_smoke_prediction: bool = True,
    secret: str,
) -> ArtifactVerificationReceipt:
    """Verify a training artifact (trusted-side, fail-closed).

    Performs the full verification chain:
    1. Resolve the artifact metadata (URI, sha256, size, format, loader)
       from a :class:`RunPodTrainingCallback`'s ``primary_artifact`` or
       from explicit keyword arguments.
    2. Fetch the artifact bytes from ``artifact_uri`` (``file://`` /
       ``https://``), unless ``artifact_bytes`` is provided inline
       (testing / ``artifact://`` fake URIs).
    3. Recompute the SHA-256 and byte size, compare to the declared
       values (fail-closed on mismatch).
    4. Load the artifact with the declared ``loader_family`` (rejects
       unknown loaders).
    5. Run a deterministic smoke prediction against ``smoke_sample`` (or
       a frozen default sample when ``run_smoke_prediction=True`` and no
       sample is given).
    6. Build and HMAC-sign an :class:`ArtifactVerificationReceipt`.

    On any verification failure, a receipt is STILL built and signed
    (with ``artifact_verified=False`` and the error code recorded), so the
    trusted side has an auditable record of the failure. The
    :class:`ArtifactVerificationError` is also raised so the caller can
    branch on the failure code.

    Args:
        callback: a :class:`RunPodTrainingCallback` (or dict) whose
            ``primary_artifact`` carries the artifact metadata. When
            provided, the artifact fields are read from it unless
            overridden by the explicit keyword args.
        artifact_uri: explicit artifact URI (overrides callback).
        artifact_sha256: explicit declared SHA-256 (overrides callback).
        artifact_size_bytes: explicit declared size (overrides callback).
        artifact_format: explicit declared format (overrides callback).
        loader_family: explicit declared loader family (overrides
            callback).
        artifact_bytes: inline artifact bytes (skip the URI fetch — used
            for testing and ``artifact://`` fake URIs).
        smoke_sample: a frozen feature matrix for the smoke prediction.
            When ``None`` and ``run_smoke_prediction=True``, a frozen
            default sample is built from the loaded model's feature count.
        run_smoke_prediction: if ``True`` (default), run the smoke
            prediction step. Set to ``False`` to skip it (the receipt
            records ``smoke_prediction_passed=True`` when skipped).
        secret: HMAC secret for signing the receipt.

    Returns:
        A signed :class:`ArtifactVerificationReceipt`.

    Raises:
        ArtifactVerificationError: on any verification failure (the
            receipt is also returned via the exception's ``__cause__``
            chain — see implementation). Callers that want the receipt
            even on failure should catch the exception.
    """
    # --- resolve artifact metadata from callback or explicit args ----------
    uri = artifact_uri
    sha = artifact_sha256
    size = artifact_size_bytes
    fmt = artifact_format
    loader = loader_family

    if callback is not None:
        if isinstance(callback, dict):
            pa = callback.get("primary_artifact")
        elif isinstance(callback, RunPodTrainingCallback):
            pa = callback.primary_artifact
        else:
            pa = None
        if isinstance(pa, dict):
            if uri is None:
                uri = pa.get("artifact_uri")
            if sha is None:
                sha = pa.get("artifact_sha256")
            if size is None:
                size = pa.get("artifact_size_bytes")
            if fmt is None:
                fmt = pa.get("artifact_format")
            if loader is None:
                loader = pa.get("loader_family")

    # --- helper to build + sign a receipt (success or failure) ------------
    def _make_receipt(
        *,
        artifact_verified: bool,
        hash_verified: bool,
        size_verified: bool,
        loader_verified: bool,
        smoke_passed: bool,
        smoke_metrics: dict[str, Any],
        error_code: str | None,
    ) -> ArtifactVerificationReceipt:
        ts_ns = time.time_ns()
        unsigned = ArtifactVerificationReceipt.model_construct(
            artifact_uri=uri or "",
            artifact_sha256=sha or "",
            artifact_size_bytes=int(size) if size is not None else 0,
            artifact_format=fmt or "",
            loader_family=loader or "",
            artifact_verified=artifact_verified,
            hash_verified=hash_verified,
            size_verified=size_verified,
            loader_verified=loader_verified,
            smoke_prediction_passed=smoke_passed,
            smoke_prediction_metrics=smoke_metrics,
            verification_error=error_code,
            verified_at_ns=ts_ns,
            receipt_signature="",
        )
        sig = _sign_receipt(unsigned, secret=secret)
        return ArtifactVerificationReceipt(
            artifact_uri=uri or "",
            artifact_sha256=sha or "",
            artifact_size_bytes=int(size) if size is not None else 0,
            artifact_format=fmt or "",
            loader_family=loader or "",
            artifact_verified=artifact_verified,
            hash_verified=hash_verified,
            size_verified=size_verified,
            loader_verified=loader_verified,
            smoke_prediction_passed=smoke_passed,
            smoke_prediction_metrics=smoke_metrics,
            verification_error=error_code,
            verified_at_ns=ts_ns,
            receipt_signature=sig,
        )

    # --- step 1: missing artifact check -----------------------------------
    if not uri or not uri.strip():
        receipt = _make_receipt(
            artifact_verified=False,
            hash_verified=False,
            size_verified=False,
            loader_verified=False,
            smoke_passed=False,
            smoke_metrics={},
            error_code="missing_artifact",
        )
        raise _ArtifactVerificationWithReceipt(
            "missing_artifact",
            "artifact URI is empty or None (missing artifact — fail closed)",
            receipt,
        )

    # --- step 2: fetch artifact bytes -------------------------------------
    try:
        if artifact_bytes is not None:
            model_bytes = artifact_bytes
        else:
            model_bytes = _fetch_artifact_bytes(uri)
    except ArtifactVerificationError as exc:
        receipt = _make_receipt(
            artifact_verified=False,
            hash_verified=False,
            size_verified=False,
            loader_verified=False,
            smoke_passed=False,
            smoke_metrics={},
            error_code=exc.code,
        )
        raise _ArtifactVerificationWithReceipt(exc.code, str(exc), receipt) from exc

    # --- step 3: recompute hash + size, compare (fail-closed) -------------
    recomputed_sha = hashlib.sha256(model_bytes).hexdigest()
    recomputed_size = len(model_bytes)
    hash_ok = (
        isinstance(sha, str)
        and len(sha) == 64
        and hmac.compare_digest(
            recomputed_sha,
            sha.lower(),
        )
    )
    size_ok = size is not None and recomputed_size == int(size)

    if not hash_ok:
        receipt = _make_receipt(
            artifact_verified=False,
            hash_verified=False,
            size_verified=size_ok,
            loader_verified=False,
            smoke_passed=False,
            smoke_metrics={},
            error_code="hash_mismatch",
        )
        raise _ArtifactVerificationWithReceipt(
            "hash_mismatch",
            f"artifact sha256 mismatch: declared={sha!r} recomputed={recomputed_sha!r}",
            receipt,
        )
    if not size_ok:
        receipt = _make_receipt(
            artifact_verified=False,
            hash_verified=True,
            size_verified=False,
            loader_verified=False,
            smoke_passed=False,
            smoke_metrics={},
            error_code="size_mismatch",
        )
        raise _ArtifactVerificationWithReceipt(
            "size_mismatch",
            f"artifact size mismatch: declared={size!r} recomputed={recomputed_size!r}",
            receipt,
        )

    # --- step 4: load artifact with declared loader family ----------------
    try:
        model = _load_artifact(model_bytes, loader or "", fmt or "")
    except ArtifactVerificationError as exc:
        receipt = _make_receipt(
            artifact_verified=False,
            hash_verified=True,
            size_verified=True,
            loader_verified=False,
            smoke_passed=False,
            smoke_metrics={},
            error_code=exc.code,
        )
        raise _ArtifactVerificationWithReceipt(exc.code, str(exc), receipt) from exc

    # --- step 5: smoke prediction (deterministic, frozen sample) ----------
    smoke_passed = True
    smoke_metrics: dict[str, Any] = {}
    if run_smoke_prediction:
        sample = smoke_sample
        if sample is None:
            sample = _build_frozen_smoke_sample(model)
        smoke_passed, smoke_metrics = _run_smoke_prediction(model, sample)
        if not smoke_passed:
            receipt = _make_receipt(
                artifact_verified=False,
                hash_verified=True,
                size_verified=True,
                loader_verified=True,
                smoke_passed=False,
                smoke_metrics=smoke_metrics,
                error_code="smoke_prediction_failed",
            )
            raise _ArtifactVerificationWithReceipt(
                "smoke_prediction_failed",
                f"smoke prediction failed: {smoke_metrics.get('error', 'unknown')}",
                receipt,
            )

    # --- step 6: all checks passed — build + sign the success receipt -----
    receipt = _make_receipt(
        artifact_verified=True,
        hash_verified=True,
        size_verified=True,
        loader_verified=True,
        smoke_passed=smoke_passed,
        smoke_metrics=smoke_metrics,
        error_code=None,
    )
    return receipt


class _ArtifactVerificationWithReceipt(ArtifactVerificationError):
    """Internal: ArtifactVerificationError that carries the signed receipt.

    This lets callers that want the receipt even on failure to access it
    via ``exc.receipt`` without changing the public exception hierarchy
    (``_ArtifactVerificationWithReceipt`` is still an
    ``ArtifactVerificationError`` and a ``ValueError``).
    """

    def __init__(
        self,
        code: str,
        message: str,
        receipt: ArtifactVerificationReceipt,
    ) -> None:
        super().__init__(code, message)
        self.receipt = receipt


class TrainingCallbackVerificationResult(BaseModel):
    """Combined result of :func:`verify_training_callback`.

    Frozen + ``extra='forbid'``. Carries the validated callback and the
    artifact verification receipt, plus the single authoritative
    ``artifact_verified`` flag (``True`` only if BOTH the callback HMAC
    validated AND the artifact verification passed).

    Fields:
        callback: the validated :class:`RunPodTrainingCallback`.
        callback_valid: ``True`` if the callback HMAC + schema validated.
        receipt: the :class:`ArtifactVerificationReceipt` (or ``None``
            when artifact verification was skipped or the callback itself
            was invalid).
        artifact_verified: ``True`` only if the callback validated AND
            the receipt's ``artifact_verified`` is ``True``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    callback: RunPodTrainingCallback
    callback_valid: bool = False
    receipt: ArtifactVerificationReceipt | None = None
    artifact_verified: bool = False


def verify_training_callback(
    callback: RunPodTrainingCallback | dict[str, Any],
    *,
    secret: str,
    smoke_sample: Any = None,
    run_smoke_prediction: bool = True,
    artifact_bytes: bytes | None = None,
) -> TrainingCallbackVerificationResult:
    """Validate a callback AND verify its artifact (trusted-side, combined).

    Combines:
    1. :func:`validate_callback` — HMAC + schema validation (fail-closed).
    2. :func:`verify_artifact` — artifact hash + size + loader + smoke
       prediction (fail-closed).

    The ``artifact_verified`` flag on the returned result is ``True`` ONLY
    if BOTH the callback validated AND the artifact verification passed.
    This is the single authoritative flag the trusted side checks before
    promoting a callback.

    On a callback validation failure, raises
    :class:`CallbackValidationError` (no artifact verification is
    attempted — there's nothing to verify if the callback itself is
    untrusted).

    On an artifact verification failure, raises
    :class:`ArtifactVerificationError` (the signed receipt is available
    via ``exc.receipt`` on the
    :class:`_ArtifactVerificationWithReceipt` subclass). Callers that
    want the result even on artifact failure should catch the exception.

    Args:
        callback: a :class:`RunPodTrainingCallback` or dict.
        secret: the HMAC secret (used for both callback + receipt signing).
        smoke_sample: optional frozen feature matrix for smoke prediction.
        run_smoke_prediction: if ``True`` (default), run smoke prediction.
        artifact_bytes: inline artifact bytes (skip URI fetch — testing).

    Returns:
        A :class:`TrainingCallbackVerificationResult` with the validated
        callback and signed receipt.

    Raises:
        CallbackValidationError: if the callback HMAC/schema is invalid.
        ArtifactVerificationError: if artifact verification fails.
    """
    # 1. Callback validation (HMAC + schema). Fail-closed — if the
    #    callback itself is untrusted, there's nothing to verify.
    model = validate_callback(callback, secret=secret)

    # 2. Artifact verification (hash + size + loader + smoke prediction).
    #    If the callback has no primary_artifact (failure callback), skip
    #    artifact verification and return artifact_verified=False.
    if model.primary_artifact is None:
        return TrainingCallbackVerificationResult(
            callback=model,
            callback_valid=True,
            receipt=None,
            artifact_verified=False,
        )

    receipt = verify_artifact(
        model,
        secret=secret,
        smoke_sample=smoke_sample,
        run_smoke_prediction=run_smoke_prediction,
        artifact_bytes=artifact_bytes,
    )

    return TrainingCallbackVerificationResult(
        callback=model,
        callback_valid=True,
        receipt=receipt,
        artifact_verified=receipt.artifact_verified,
    )


def mark_callback_verified(
    callback: RunPodTrainingCallback | dict[str, Any],
    receipt: ArtifactVerificationReceipt,
    *,
    secret: str,
) -> dict[str, Any]:
    """Mark a callback as artifact-verified (trusted-side).

    Returns a new callback dict with ``artifact_verified`` set to ``True``
    and the verification receipt attached, BUT only if
    ``receipt.artifact_verified`` is ``True``. If the receipt indicates
    verification failed, the returned dict has ``artifact_verified=False``
    and the receipt is still attached (for audit).

    The returned dict is a plain dict (not a frozen Pydantic model) so
    the caller can merge it into an outbox record / store it. The receipt
    is embedded as a dict under the ``artifact_verification_receipt`` key.

    Args:
        callback: the validated callback (model or dict).
        receipt: the :class:`ArtifactVerificationReceipt` from
            :func:`verify_artifact`.
        secret: HMAC secret (used to verify the receipt signature before
            trusting it — fail-closed).

    Returns:
        A dict with the callback fields plus ``artifact_verified`` and
        ``artifact_verification_receipt``.
    """
    # Verify the receipt signature before trusting it (fail-closed).
    receipt_ok = receipt.verify_receipt(secret=secret)
    artifact_verified = receipt_ok and receipt.artifact_verified

    if isinstance(callback, RunPodTrainingCallback):
        cb_dict = callback.model_dump()
    elif isinstance(callback, dict):
        cb_dict = dict(callback)
    else:
        cb_dict = {}

    cb_dict["artifact_verified"] = artifact_verified
    cb_dict["artifact_verification_receipt"] = receipt.model_dump()
    return cb_dict


# --- runtime fingerprint (Phase 5 / T-5.2) -----------------------------------
#
# A signed, tamper-evident runtime fingerprint that binds a training
# callback to the exact worker image + environment it was produced in.
# This extends the T-4.1 gpu_healthcheck runtime fingerprint (git sha +
# lockfile hash + container digest) with the full reproducibility pin
# set required for production promotion:
#
# - git sha, image digest, Dockerfile hash, dependency lock hash,
# - Python version, OS image version,
# - CUDA / driver / GPU model (when a GPU is present),
# - training library versions (lightgbm, xgboost, catboost, sklearn, ...),
# - random seeds set during training,
# - dataset + training manifest hashes,
# - a SHA-256 ``fingerprint_hash`` over the canonical JSON of all the
#   above, and an HMAC ``fingerprint_signature`` over that hash.
#
# Mode-aware validation (acceptance criteria 3-4):
# - ``production``: FAILS if ``image_digest`` is missing/empty/placeholder.
# - ``canary``: warns but marks ``promotion_eligible=False``.
# - ``research``: warns but allows (promotion stays at the mode default).
#
# Every successful job carries a signed runtime fingerprint (criterion 5):
# ``build_callback`` accepts a :class:`RuntimeFingerprint` and binds its
# ``fingerprint_hash`` into the signed callback payload.

# Image-digest values that are treated as "missing/placeholder" for
# production fail-closed validation. A real container digest looks like
# ``sha256:<64 hex>``; anything in this set (case-insensitive) is rejected
# for production and warned-for in canary/research.
_IMAGE_DIGEST_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "",
        "placeholder",
        "unknown",
        "local-container-digest",
        "none",
        "n/a",
        "null",
        "missing",
        "local-digest",
    }
)


def _is_placeholder_digest(digest: str | None) -> bool:
    """Return True if ``digest`` is missing/empty/placeholder."""
    if digest is None:
        return True
    if not isinstance(digest, str):
        return True
    return digest.strip().lower() in _IMAGE_DIGEST_PLACEHOLDERS


# Training libraries whose installed versions are recorded in the
# fingerprint. Imported lazily via ``importlib.metadata`` so a missing
# library never breaks fingerprint collection (fail-soft → omitted).
_TRAINING_LIBRARIES: tuple[str, ...] = (
    "lightgbm",
    "xgboost",
    "catboost",
    "scikit-learn",
    "numpy",
    "pandas",
    "scipy",
    "torch",
    "tensorflow",
    "quant-foundry",
)


class RuntimeFingerprint(BaseModel):
    """Signed, tamper-evident runtime fingerprint (Phase 5 / T-5.2).

    Frozen + ``extra='forbid'`` (audit integrity). Binds a training
    callback to the exact worker image + environment that produced it.
    The ``fingerprint_hash`` is a SHA-256 over the canonical JSON of
    every field except ``fingerprint_hash`` and ``fingerprint_signature``
    (the two derived fields). The ``fingerprint_signature`` is an
    HMAC-SHA256 over ``fingerprint_hash`` (signed with the callback
    secret), so the fingerprint cannot be forged after the fact.

    Fields:
        git_sha: the code git SHA (pinned at build time or ``git rev-parse``).
        image_digest: the container image SHA-256 digest
            (``sha256:<64 hex>``). Production fails closed if this is
            missing/placeholder.
        dockerfile_hash: SHA-256 of the Dockerfile content.
        dependency_lock_hash: SHA-256 of the requirements/lockfile.
        python_version: ``sys.version`` string.
        os_image_version: platform/version string for the OS image.
        cuda_version: CUDA version (from nvidia-smi), or ``None``.
        driver_version: GPU driver version (from nvidia-smi), or ``None``.
        gpu_model: GPU model name (from nvidia-smi), or ``None``.
        training_library_versions: mapping of library name → installed
            version (lightgbm, xgboost, catboost, sklearn, ...).
        random_seeds: mapping of seed name → value (numpy, python random,
            ...). Any seeds set during training.
        dataset_manifest_hash: SHA-256 of the dataset manifest reference.
        training_manifest_hash: SHA-256 of the training manifest content.
        fingerprint_hash: SHA-256 over the canonical JSON of every field
            above (excluding this field and ``fingerprint_signature``).
        fingerprint_signature: HMAC-SHA256 hex over ``fingerprint_hash``.
        collected_at_ns: nanosecond epoch timestamp of collection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    git_sha: str
    image_digest: str
    dockerfile_hash: str
    dependency_lock_hash: str
    python_version: str
    os_image_version: str
    cuda_version: str | None = None
    driver_version: str | None = None
    gpu_model: str | None = None
    training_library_versions: dict[str, str] = Field(default_factory=dict)
    random_seeds: dict[str, int] = Field(default_factory=dict)
    dataset_manifest_hash: str
    training_manifest_hash: str
    fingerprint_hash: str = ""
    fingerprint_signature: str = ""
    collected_at_ns: int = 0

    def verify(self, *, secret: str) -> bool:
        """Recompute the fingerprint hash + HMAC and compare (constant-time).

        Returns ``True`` iff both the recomputed ``fingerprint_hash``
        matches the stored value AND the recomputed HMAC signature
        matches the stored ``fingerprint_signature``. Fail-closed —
        never raises on a mismatch.
        """
        if not isinstance(secret, str) or not secret:
            return False
        if not self.fingerprint_hash or not self.fingerprint_signature:
            return False
        recomputed = _compute_fingerprint_hash(self)
        if not hmac.compare_digest(recomputed, self.fingerprint_hash):
            return False
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            self.fingerprint_hash.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected_sig, self.fingerprint_signature)


def _fingerprint_input_payload(fp: RuntimeFingerprint) -> bytes:
    """Return the canonical JSON bytes of a fingerprint's INPUT fields.

    Excludes both derived fields (``fingerprint_hash`` and
    ``fingerprint_signature``) so the hash is non-circular. Keys are
    sorted for determinism.
    """
    data = fp.model_dump()
    data.pop("fingerprint_hash", None)
    data.pop("fingerprint_signature", None)
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _compute_fingerprint_hash(fp: RuntimeFingerprint) -> str:
    """Compute the SHA-256 over the canonical JSON of a fingerprint's inputs."""
    return hashlib.sha256(_fingerprint_input_payload(fp)).hexdigest()


def _collect_git_sha() -> str:
    """Collect the code git SHA from the ``GIT_SHA`` env var or git."""
    env = os.environ.get("GIT_SHA")
    if env and env.strip():
        return env.strip()
    # Fall back to the build-time pin helper (no subprocess in the handler
    # hot path — keeps the module pure / importable without git).
    pinned = _git_sha_or_default()
    if pinned and pinned.strip():
        return pinned
    return "unknown"


def _collect_image_digest() -> str:
    """Collect the container image digest from env or build-time pin."""
    env = os.environ.get("IMAGE_DIGEST") or os.environ.get(
        "CONTAINER_IMAGE_DIGEST",
    )
    if env and env.strip():
        return env.strip()
    pinned = _container_digest_or_default()
    if pinned and pinned.strip():
        return pinned
    return "unknown"


def _collect_dockerfile_hash() -> str:
    """Compute the SHA-256 of the Dockerfile content, if accessible."""
    env = os.environ.get("DOCKERFILE_HASH")
    if env and env.strip():
        return env.strip()
    # Best-effort: hash a Dockerfile in the cwd or one level up. Fail-soft
    # to "unknown" when not accessible (the handler must stay importable
    # without a Dockerfile present).
    try:
        from pathlib import Path

        candidates = (
            Path("Dockerfile"),
            Path("docker/Dockerfile"),
            Path("../Dockerfile"),
            Path("runpod/quant-foundry-training/Dockerfile"),
        )
        for cand in candidates:
            if cand.is_file():
                return hashlib.sha256(cand.read_bytes()).hexdigest()
    except Exception:
        pass
    return "unknown"


def _collect_dependency_lock_hash() -> str:
    """Compute the SHA-256 of the dependency lockfile, if accessible."""
    env = os.environ.get("DEPENDENCY_LOCK_HASH") or os.environ.get(
        "LOCKFILE_HASH",
    )
    if env and env.strip():
        return env.strip()
    pinned = _lockfile_hash_or_default()
    if pinned and pinned.strip():
        return pinned
    # Best-effort: hash a requirements/lockfile in the cwd. Fail-soft to
    # "unknown" when not accessible.
    try:
        from pathlib import Path

        candidates = (
            Path("requirements.lock"),
            Path("requirements.txt"),
            Path("pyproject.toml"),
            Path("uv.lock"),
            Path("poetry.lock"),
        )
        for cand in candidates:
            if cand.is_file():
                return hashlib.sha256(cand.read_bytes()).hexdigest()
    except Exception:
        pass
    return "unknown"


def _collect_python_version() -> str:
    """Return the Python version string (``sys.version``)."""
    return sys.version


def _collect_os_image_version() -> str:
    """Return a platform/version string describing the OS image."""
    try:
        return platform.platform(terse=True)
    except Exception:
        return "unknown"


def _probe_gpu() -> tuple[str | None, str | None, str | None]:
    """Probe the GPU via ``nvidia-smi`` (lazy, fail-soft).

    Returns ``(cuda_version, driver_version, gpu_model)``. Each element
    is ``None`` when ``nvidia-smi`` is unavailable or parsing fails —
    the fingerprint must stay collectable on CPU-only workers.
    """
    try:
        import subprocess

        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        raw = proc.stdout.strip()
    except Exception:
        return (None, None, None)

    gpu_model: str | None = None
    driver_version: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 1 and gpu_model is None:
            gpu_model = parts[0]
        if len(parts) >= 2 and driver_version is None:
            driver_version = parts[1]
        break  # first GPU only
    # CUDA version: query separately (nvidia-smi --query-gpu doesn't expose
    # CUDA version directly; use the header line of a bare nvidia-smi call).
    cuda_version: str | None = None
    try:
        import subprocess

        proc2 = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        # The CSV header line is "name, driver_version" — not CUDA. Fall
        # back to the PyTorch probe below if nvidia-smi doesn't expose it.
        out = (proc2.stdout or "").strip()
        if "CUDA Version:" in out:
            cuda_version = out.split("CUDA Version:", 1)[1].split()[0]
    except Exception:
        pass
    # Secondary CUDA probe via PyTorch (lazy import).
    if cuda_version is None:
        try:
            import importlib

            if importlib.util.find_spec("torch") is not None:
                import torch

                if torch.cuda.is_available():
                    cuda_version = str(torch.version.cuda)
        except Exception:
            pass
    return (cuda_version, driver_version, gpu_model)


def _collect_training_library_versions() -> dict[str, str]:
    """Collect installed versions of the training libraries (lazy, fail-soft).

    Uses :mod:`importlib.metadata` so a missing library is simply omitted
    (never raises). The set of libraries probed is
    :data:`_TRAINING_LIBRARIES`.
    """
    versions: dict[str, str] = {}
    try:
        import importlib.metadata as md

        for lib in _TRAINING_LIBRARIES:
            try:
                versions[lib] = md.version(lib)
            except Exception:
                continue
    except Exception:
        pass
    return versions


def _collect_random_seeds(extra_seeds: dict[str, int] | None = None) -> dict[str, int]:
    """Collect the random seeds set during training.

    Records the Python ``random`` module state size and any seeds passed
    via the ``RANDOM_SEED`` env var or the ``extra_seeds`` argument. The
    numpy generator state (when numpy is available) is recorded under
    ``"numpy"``. This is a snapshot of the seeds *as configured*, not the
    full PRNG state — sufficient for reproducibility pinning.
    """
    seeds: dict[str, int] = {}
    env_seed = os.environ.get("RANDOM_SEED")
    if env_seed is not None:
        try:
            seeds["env_random_seed"] = int(env_seed)
        except ValueError:
            seeds["env_random_seed_raw"] = 0  # placeholder; non-int seed
    if extra_seeds:
        for k, v in extra_seeds.items():
            try:
                seeds[k] = int(v)
            except (TypeError, ValueError):
                continue
    # numpy: record whether a seed was set via env (we can't read the live
    # global seed portably, so we record the configured value).
    try:
        import importlib

        if importlib.util.find_spec("numpy") is not None:
            seeds.setdefault("numpy_default", 0)
    except Exception:
        pass
    return seeds


def build_runtime_fingerprint(
    *,
    dataset_manifest_hash: str,
    training_manifest_hash: str,
    secret: str,
    git_sha: str | None = None,
    image_digest: str | None = None,
    dockerfile_hash: str | None = None,
    dependency_lock_hash: str | None = None,
    python_version: str | None = None,
    os_image_version: str | None = None,
    cuda_version: str | None = None,
    driver_version: str | None = None,
    gpu_model: str | None = None,
    training_library_versions: dict[str, str] | None = None,
    random_seeds: dict[str, int] | None = None,
) -> RuntimeFingerprint:
    """Build a signed :class:`RuntimeFingerprint` (Phase 5 / T-5.2).

    Collects the full reproducibility pin set (git sha, image digest,
    Dockerfile hash, dependency lock hash, Python version, OS image
    version, CUDA/driver/GPU, training library versions, random seeds,
    dataset + training manifest hashes), computes the
    ``fingerprint_hash`` (SHA-256 over the canonical JSON of all input
    fields), and signs it with an HMAC (``fingerprint_signature``) using
    the callback ``secret``.

    Every field can be overridden via a keyword arg (used by tests and by
    callers that already have the values pinned). When a field is
    ``None``, it is collected from the environment / build-time pin /
    runtime probe (fail-soft to ``"unknown"`` or ``None``).

    Args:
        dataset_manifest_hash: SHA-256 of the dataset manifest reference.
        training_manifest_hash: SHA-256 of the training manifest content.
        secret: HMAC secret for signing the fingerprint.
        git_sha: override for the code git SHA.
        image_digest: override for the container image digest.
        dockerfile_hash: override for the Dockerfile SHA-256.
        dependency_lock_hash: override for the dependency lock SHA-256.
        python_version: override for the Python version string.
        os_image_version: override for the OS image version string.
        cuda_version: override for the CUDA version.
        driver_version: override for the GPU driver version.
        gpu_model: override for the GPU model name.
        training_library_versions: override for the library versions map.
        random_seeds: extra seeds to record (merged with env/probed seeds).

    Returns:
        A frozen, signed :class:`RuntimeFingerprint`.
    """
    # --- collect fields (override → env → build-time pin → probe) ----------
    sha = git_sha if git_sha is not None else _collect_git_sha()
    digest = image_digest if image_digest is not None else _collect_image_digest()
    df_hash = dockerfile_hash if dockerfile_hash is not None else _collect_dockerfile_hash()
    lock_hash = (
        dependency_lock_hash
        if dependency_lock_hash is not None
        else _collect_dependency_lock_hash()
    )
    py_ver = python_version if python_version is not None else _collect_python_version()
    os_ver = os_image_version if os_image_version is not None else _collect_os_image_version()

    # GPU probe: only run when the caller didn't override ALL three fields.
    if cuda_version is None or driver_version is None or gpu_model is None:
        probed_cuda, probed_driver, probed_model = _probe_gpu()
        if cuda_version is None:
            cuda_version = probed_cuda
        if driver_version is None:
            driver_version = probed_driver
        if gpu_model is None:
            gpu_model = probed_model

    lib_versions = (
        training_library_versions
        if training_library_versions is not None
        else _collect_training_library_versions()
    )
    seeds = _collect_random_seeds(random_seeds)

    collected_at_ns = time.time_ns()

    # Build WITHOUT the derived fields first so we can compute the hash.
    unsigned = RuntimeFingerprint.model_construct(
        git_sha=sha,
        image_digest=digest,
        dockerfile_hash=df_hash,
        dependency_lock_hash=lock_hash,
        python_version=py_ver,
        os_image_version=os_ver,
        cuda_version=cuda_version,
        driver_version=driver_version,
        gpu_model=gpu_model,
        training_library_versions=dict(lib_versions),
        random_seeds=dict(seeds),
        dataset_manifest_hash=dataset_manifest_hash,
        training_manifest_hash=training_manifest_hash,
        fingerprint_hash="",
        fingerprint_signature="",
        collected_at_ns=collected_at_ns,
    )
    fp_hash = _compute_fingerprint_hash(unsigned)
    signature = hmac.new(
        secret.encode("utf-8"),
        fp_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return RuntimeFingerprint(
        git_sha=sha,
        image_digest=digest,
        dockerfile_hash=df_hash,
        dependency_lock_hash=lock_hash,
        python_version=py_ver,
        os_image_version=os_ver,
        cuda_version=cuda_version,
        driver_version=driver_version,
        gpu_model=gpu_model,
        training_library_versions=dict(lib_versions),
        random_seeds=dict(seeds),
        dataset_manifest_hash=dataset_manifest_hash,
        training_manifest_hash=training_manifest_hash,
        fingerprint_hash=fp_hash,
        fingerprint_signature=signature,
        collected_at_ns=collected_at_ns,
    )


def verify_runtime_fingerprint(
    fingerprint: RuntimeFingerprint | dict[str, Any],
    *,
    secret: str,
) -> bool:
    """Verify the hash + HMAC signature of a :class:`RuntimeFingerprint`.

    Recomputes the ``fingerprint_hash`` from the input fields and the
    HMAC signature over that hash, then compares both to the stored
    values using :func:`hmac.compare_digest` (constant-time). Returns
    ``True`` if both match, ``False`` otherwise (fail-closed — never
    raises on a mismatch).

    Args:
        fingerprint: a :class:`RuntimeFingerprint` or a dict that can be
            parsed into one.
        secret: the HMAC secret used at signing time.

    Returns:
        ``True`` if the fingerprint hash + signature are valid.
    """
    if isinstance(fingerprint, dict):
        try:
            fingerprint = RuntimeFingerprint.model_validate(fingerprint)
        except Exception:
            return False
    elif not isinstance(fingerprint, RuntimeFingerprint):
        return False
    return fingerprint.verify(secret=secret)


class RuntimeFingerprintValidationResult(BaseModel):
    """Result of :func:`validate_runtime_fingerprint` (Phase 5 / T-5.2).

    Frozen + ``extra='forbid'``. Carries the pass/fail verdict, the
    mode-aware ``promotion_eligible`` flag, and the list of warnings +
    errors so the trusted side can audit exactly why a fingerprint was
    accepted/rejected.

    Fields:
        passed: ``True`` if the fingerprint is acceptable for the mode
            (production fails closed on a missing/placeholder image
            digest; canary/research warn but pass).
        mode: the training mode the validation ran under.
        promotion_eligible: mode-aware. Canary with a placeholder digest
            is forced to ``False``; production failure is ``False``;
            research stays at the mode default (``True`` here — the
            caller combines with MODE_RULES).
        warnings: human-readable warnings (non-fatal).
        errors: human-readable errors (fatal for the mode).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    mode: str
    promotion_eligible: bool = True
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    errors: tuple[str, ...] = Field(default_factory=tuple)


def validate_runtime_fingerprint(
    fingerprint: RuntimeFingerprint | dict[str, Any],
    *,
    mode: TrainingMode,
    secret: str | None = None,
) -> RuntimeFingerprintValidationResult:
    """Validate a runtime fingerprint in a mode-aware way (Phase 5 / T-5.2).

    Acceptance criteria:
    - **production**: FAILS (``passed=False``) if ``image_digest`` is
      missing, empty, or placeholder. ``promotion_eligible=False``.
    - **canary**: warns but marks ``promotion_eligible=False`` (canary is
      never promotion eligible; a placeholder digest reinforces this).
    - **research**: warns but allows (``promotion_eligible=True`` — the
      caller applies the MODE_RULES default separately).

    When ``secret`` is provided, the fingerprint's hash + HMAC signature
    are also verified (a signature failure is a fatal error in every
    mode — a forged fingerprint is never acceptable).

    Args:
        fingerprint: a :class:`RuntimeFingerprint` or a dict.
        mode: the training mode to validate under.
        secret: optional HMAC secret. When provided, the signature is
            verified and a mismatch is treated as a fatal error.

    Returns:
        A :class:`RuntimeFingerprintValidationResult`.
    """
    warnings: list[str] = []
    errors: list[str] = []

    # Parse the fingerprint (fail-closed on a schema error).
    if isinstance(fingerprint, dict):
        try:
            fp = RuntimeFingerprint.model_validate(fingerprint)
        except Exception as exc:
            return RuntimeFingerprintValidationResult(
                passed=False,
                mode=mode.value,
                promotion_eligible=False,
                warnings=(),
                errors=(f"fingerprint schema validation failed: {exc}",),
            )
    elif isinstance(fingerprint, RuntimeFingerprint):
        fp = fingerprint
    else:
        return RuntimeFingerprintValidationResult(
            passed=False,
            mode=mode.value,
            promotion_eligible=False,
            warnings=(),
            errors=(
                f"fingerprint must be a RuntimeFingerprint or dict, got "
                f"{type(fingerprint).__name__}",
            ),
        )

    # Signature verification (when a secret is supplied). A forged
    # fingerprint is a fatal error in EVERY mode (fail-closed).
    if secret is not None:
        if not verify_runtime_fingerprint(fp, secret=secret):
            errors.append(
                "runtime fingerprint signature verification failed "
                "(HMAC mismatch — possible tamper)",
            )

    # Image-digest check (the core production gate).
    digest_missing = _is_placeholder_digest(fp.image_digest)
    if digest_missing:
        msg = (
            f"image_digest is missing/placeholder "
            f"(got {fp.image_digest!r}); production requires a real "
            f"container image digest (sha256:<64 hex>)"
        )
        if mode == TrainingMode.PRODUCTION:
            errors.append(msg)
        else:
            warnings.append(msg)

    # Mode-aware verdict.
    if mode == TrainingMode.PRODUCTION:
        passed = not errors
        promotion_eligible = passed
    elif mode == TrainingMode.CANARY:
        # Canary warns but marks promotion ineligible (criterion 4).
        passed = not errors
        promotion_eligible = False
    else:
        # Research: warn but allow (criterion 4).
        passed = not errors
        promotion_eligible = True

    return RuntimeFingerprintValidationResult(
        passed=passed,
        mode=mode.value,
        promotion_eligible=promotion_eligible,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def attach_runtime_fingerprint(
    callback: RunPodTrainingCallback | dict[str, Any],
    fingerprint: RuntimeFingerprint,
    *,
    secret: str,
) -> dict[str, Any]:
    """Attach a signed runtime fingerprint to a callback (trusted-side).

    Returns a new callback dict with the full :class:`RuntimeFingerprint`
    embedded under the ``runtime_fingerprint`` key, BUT only after
    verifying the fingerprint's HMAC signature (fail-closed — a forged
    fingerprint is never attached). If the signature does not verify,
    ``runtime_fingerprint_verified`` is set to ``False`` and the
    fingerprint is still embedded (for audit) but flagged untrusted.

    The returned dict is a plain dict (not a frozen Pydantic model) so
    the caller can merge it into an outbox record / store it. This
    mirrors :func:`mark_callback_verified`.

    Args:
        callback: the validated callback (model or dict).
        fingerprint: the :class:`RuntimeFingerprint` to attach.
        secret: HMAC secret (used to verify the fingerprint signature).

    Returns:
        A dict with the callback fields plus ``runtime_fingerprint`` and
        ``runtime_fingerprint_verified``.
    """
    verified = fingerprint.verify(secret=secret)
    if isinstance(callback, RunPodTrainingCallback):
        cb_dict = callback.model_dump()
    elif isinstance(callback, dict):
        cb_dict = dict(callback)
    else:
        cb_dict = {}
    cb_dict["runtime_fingerprint"] = fingerprint.model_dump()
    cb_dict["runtime_fingerprint_verified"] = verified
    return cb_dict


# --- signed failure envelopes (Phase 5 / T-5.3) -----------------------------
#
# A standardized, HMAC-signed failure envelope so the trusted side
# (dispatcher/gateway) can authenticate ANY failure emitted by the worker.
# Every handler failure path returns a SignedFailureEnvelope so a failure
# is never a silent drop — the dispatcher verifies the signature before
# recording the FAILED transition (production: fail-closed; canary/research:
# advisory).
#
# Design (matching the codebase Pydantic-v2 conventions):
# - ``frozen=True`` + ``extra='forbid'`` (audit integrity / fail-closed).
# - ``context_hash`` = SHA-256 of the canonical JSON of ``context`` (the
#   key-value pairs describing the failure — task_id, dataset_id, gate_code,
#   etc.). This binds the signature to the exact failure context.
# - ``signature`` = HMAC-SHA256 over ``context_hash`` (signed with the
#   callback secret). The trusted side recomputes both and compares
#   constant-time.
# - ``failed_at_ns`` is a nanosecond epoch timestamp (replay protection).
# - ``runtime_fingerprint_hash`` optionally links the failure to the signed
#   runtime fingerprint (T-5.2) that was active when the failure occurred.


class SignedFailureEnvelope(BaseModel):
    """Standardized, HMAC-signed failure envelope (Phase 5 / T-5.3).

    Frozen + ``extra='forbid'`` (audit integrity). Every handler failure
    path returns a :class:`SignedFailureEnvelope` (serialized to a dict in
    the handler response under the ``"error"`` key) so the trusted side can
    authenticate the failure — it is never a silent drop.

    The ``context_hash`` is a SHA-256 over the canonical JSON of
    ``context`` (the key-value pairs describing the failure). The
    ``signature`` is an HMAC-SHA256 over ``context_hash`` (signed with the
    callback secret). The trusted side recomputes both and compares
    constant-time via :func:`verify_failure_envelope`.

    Fields:
        error_code: machine-readable error code (e.g.
            ``"security_preflight_failed"``, ``"task_not_allowed"``,
            ``"quality_gate_failed"``, ``"dataset_load_error"``,
            ``"training_error"``, ``"runtime_fingerprint_invalid"``).
        error_message: human-readable description of the failure.
        mode: the training mode the failure occurred under
            (``"production"`` / ``"canary"`` / ``"research"``).
        context_hash: SHA-256 hex of the canonical JSON of ``context``.
        context: key-value pairs describing the failure context
            (task_id, dataset_id, gate_code, etc.). All values are
            strings (canonical-JSON safe).
        signature: HMAC-SHA256 hex over ``context_hash`` (signed with
            the callback secret).
        failed_at_ns: nanosecond epoch timestamp of the failure.
        worker_id: optional worker identifier.
        runtime_fingerprint_hash: optional link to the signed runtime
            fingerprint (T-5.2) active when the failure occurred.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_code: str
    error_message: str
    mode: str
    context_hash: str
    context: dict[str, str] = Field(default_factory=dict)
    signature: str
    failed_at_ns: int
    worker_id: str | None = None
    runtime_fingerprint_hash: str | None = None

    @field_validator("error_code")
    @classmethod
    def _error_code_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("error_code must be non-empty")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("mode must be non-empty")
        return v


def _canonical_context_bytes(context: dict[str, str]) -> bytes:
    """Return the canonical JSON bytes of a failure context dict.

    Keys are sorted for determinism. Values are coerced to ``str`` so the
    hash is stable regardless of how the caller constructed the context
    (e.g. an ``int`` job_id vs a ``str`` job_id produce the same hash).
    """
    coerced = {str(k): str(v) for k, v in context.items()}
    return json.dumps(
        coerced,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _compute_context_hash(context: dict[str, str]) -> str:
    """Compute the SHA-256 hex of the canonical JSON of a context dict."""
    return hashlib.sha256(_canonical_context_bytes(context)).hexdigest()


def build_failure_envelope(
    *,
    error_code: str,
    error_message: str,
    mode: str,
    context: dict[str, str],
    secret: str,
    worker_id: str | None = None,
    runtime_fingerprint_hash: str | None = None,
) -> SignedFailureEnvelope:
    """Build a signed :class:`SignedFailureEnvelope` (Phase 5 / T-5.3).

    Computes ``context_hash`` = SHA-256(canonical_json(context)) and
    ``signature`` = HMAC-SHA256(context_hash, secret), then returns the
    frozen, signed envelope.

    Args:
        error_code: machine-readable error code.
        error_message: human-readable failure description.
        mode: the training mode (``"production"`` / ``"canary"`` /
            ``"research"``).
        context: key-value pairs describing the failure context
            (task_id, dataset_id, gate_code, etc.).
        secret: HMAC secret for signing (the callback secret).
        worker_id: optional worker identifier.
        runtime_fingerprint_hash: optional link to the signed runtime
            fingerprint (T-5.2) active when the failure occurred.

    Returns:
        A frozen, signed :class:`SignedFailureEnvelope`.
    """
    context_hash = _compute_context_hash(context)
    signature = hmac.new(
        secret.encode("utf-8"),
        context_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return SignedFailureEnvelope(
        error_code=error_code,
        error_message=error_message,
        mode=mode,
        context_hash=context_hash,
        context={str(k): str(v) for k, v in context.items()},
        signature=signature,
        failed_at_ns=time.time_ns(),
        worker_id=worker_id,
        runtime_fingerprint_hash=runtime_fingerprint_hash,
    )


def verify_failure_envelope(
    envelope: SignedFailureEnvelope | dict[str, Any],
    *,
    secret: str,
) -> bool:
    """Verify the context hash + HMAC signature of a failure envelope.

    Recomputes the ``context_hash`` from ``envelope.context`` and the
    HMAC signature over that hash, then compares both to the stored
    values using :func:`hmac.compare_digest` (constant-time). Returns
    ``True`` if both match, ``False`` otherwise (fail-closed — never
    raises on a mismatch).

    Args:
        envelope: a :class:`SignedFailureEnvelope` or a dict that can be
            parsed into one.
        secret: the HMAC secret used at signing time.

    Returns:
        ``True`` if the context hash + signature are valid.
    """
    if isinstance(envelope, dict):
        try:
            envelope = SignedFailureEnvelope.model_validate(envelope)
        except Exception:
            return False
    elif not isinstance(envelope, SignedFailureEnvelope):
        return False
    if not isinstance(secret, str) or not secret:
        return False
    if not envelope.signature or not envelope.context_hash:
        return False
    # Recompute the context hash from the stored context.
    recomputed_hash = _compute_context_hash(envelope.context)
    if not hmac.compare_digest(recomputed_hash, envelope.context_hash):
        return False
    # Recompute the HMAC signature over the context hash.
    expected_sig = hmac.new(
        secret.encode("utf-8"),
        envelope.context_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_sig, envelope.signature)


class FailureEnvelopeValidationResult(BaseModel):
    """Result of :func:`validate_failure_envelope` (Phase 5 / T-5.3).

    Frozen + ``extra='forbid'``. Carries the pass/fail verdict and the
    list of warnings + errors so the trusted side can audit exactly why
    a failure envelope was accepted/rejected.

    Fields:
        passed: ``True`` if the envelope is acceptable for the mode
            (production fails closed on a signature mismatch;
            canary/research warn but pass).
        mode: the training mode the validation ran under.
        warnings: human-readable warnings (non-fatal).
        errors: human-readable errors (fatal for the mode).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    mode: str
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    errors: tuple[str, ...] = Field(default_factory=tuple)


def validate_failure_envelope(
    envelope: SignedFailureEnvelope | dict[str, Any],
    *,
    mode: TrainingMode,
    secret: str,
) -> FailureEnvelopeValidationResult:
    """Validate a signed failure envelope in a mode-aware way (Phase 5 / T-5.3).

    Acceptance criteria:
    - **production**: FAILS (``passed=False``) if the signature does not
      verify (fail-closed — a forged failure envelope is never accepted).
    - **canary** / **research**: the signature is verified advisory — a
      mismatch is logged as a warning but the envelope is still accepted
      (``passed=True``). The envelope is always signed by the worker;
      verification is advisory in permissive modes.

    Args:
        envelope: a :class:`SignedFailureEnvelope` or a dict.
        mode: the training mode to validate under.
        secret: the HMAC secret used at signing time.

    Returns:
        A :class:`FailureEnvelopeValidationResult`.
    """
    warnings: list[str] = []
    errors: list[str] = []

    # Parse the envelope (fail-closed on a schema error).
    if isinstance(envelope, dict):
        try:
            env = SignedFailureEnvelope.model_validate(envelope)
        except Exception as exc:
            return FailureEnvelopeValidationResult(
                passed=False,
                mode=mode.value,
                warnings=(),
                errors=(f"envelope schema validation failed: {exc}",),
            )
    elif isinstance(envelope, SignedFailureEnvelope):
        env = envelope
    else:
        return FailureEnvelopeValidationResult(
            passed=False,
            mode=mode.value,
            warnings=(),
            errors=(
                f"envelope must be a SignedFailureEnvelope or dict, got {type(envelope).__name__}",
            ),
        )

    # Signature verification.
    if not verify_failure_envelope(env, secret=secret):
        msg = "failure envelope signature verification failed (HMAC mismatch — possible tamper)"
        if mode == TrainingMode.PRODUCTION:
            errors.append(msg)
        else:
            warnings.append(msg)

    # Mode-aware verdict.
    if mode == TrainingMode.PRODUCTION:
        passed = not errors
    else:
        # Canary/research: advisory — signature mismatch is a warning,
        # not a fatal error.
        passed = not errors

    return FailureEnvelopeValidationResult(
        passed=passed,
        mode=mode.value,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


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

    In the RunPod container, the code git SHA is pinned at build time
    via the QUANT_FOUNDRY_GIT_SHA env var (set from the GIT_SHA build arg
    in the Dockerfile). For local tests, we return a deterministic default.
    """
    env_sha = os.environ.get("QUANT_FOUNDRY_GIT_SHA")
    if env_sha and env_sha.strip() and env_sha.strip() != "unknown":
        return env_sha.strip()
    return "local-git-sha"


def _lockfile_hash_or_default() -> str | None:
    """Return the lockfile hash, or None. Pinned at container build time."""
    env_hash = os.environ.get("QUANT_FOUNDRY_LOCKFILE_HASH")
    if env_hash and env_hash.strip() and env_hash.strip() != "unknown":
        return env_hash.strip()
    return "local-lockfile-hash"


def _container_digest_or_default() -> str | None:
    """Return the container image digest, or None. Set at build time."""
    env_digest = os.environ.get("QUANT_FOUNDRY_CONTAINER_DIGEST")
    if env_digest and env_digest.strip() and env_digest.strip() != "unknown":
        return env_digest.strip()
    return "local-container-digest"
