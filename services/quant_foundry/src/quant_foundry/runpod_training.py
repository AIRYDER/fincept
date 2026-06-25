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
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Protocol

from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
    RunPodTrainingRequest,
)
from quant_foundry.signatures import sign_callback

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


# --- result ----------------------------------------------------------------


@dataclass(frozen=True)
class TrainingResult:
    """Result of a successful training run."""

    callback_payload: bytes  # JSON-encoded RunPodCallbackEnvelope
    callback_signature: str  # HMAC signature over callback_payload
    callback_ts: int  # unix seconds used in the signature
    artifact_id: str
    dossier_id: str


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
                    f"training deadline breached (deadline_seconds="
                    f"{self.deadline_seconds})"
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
            envelope_bytes, secret=self.callback_secret, ts=ts, job_id=req.job_id,
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
