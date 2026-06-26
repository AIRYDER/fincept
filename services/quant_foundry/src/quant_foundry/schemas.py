"""
quant_foundry.schemas — strict cross-boundary contracts for Quant Foundry (Fincept <-> external workers).

These are the *only* allowed shapes for payloads crossing to untrusted RunPod (or mock) workers.
All use Pydantic v2 with:
  - model_config = ConfigDict(frozen=True, extra="forbid")
  - explicit schema_version: int = 1
  - Shadow-only authority enforcement for predictions (no trading fields allowed ever)

See TASK-0302 acceptance + cross-cutting point-in-time / reproducibility rules.

The PlaceholderJob + helper are retained only for TASK-0301 skeleton compatibility during transition.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Authority(StrEnum):
    """Authority level for predictions/signals. Only 'shadow-only' allowed for external worker output initially."""

    SHADOW_ONLY = "shadow-only"


class JobType(StrEnum):
    TRAINING = "training"
    INFERENCE = "inference"


class QuantFoundryJob(BaseModel):
    """Top-level job descriptor dispatched to workers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    job_id: str
    job_type: JobType | str
    idempotency_key: str
    dataset_id: str
    model_family: str
    config_hash: str
    attempt_group: str = "1"
    priority: int = 0
    budget_cents: int | None = None
    timeout_seconds: int | None = None


class RunPodTrainingRequest(BaseModel):
    """Payload for a training job sent to external worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    job_id: str
    dataset_manifest_ref: str
    model_family: str
    search_space: dict[str, list[Any]] = Field(default_factory=dict)
    random_seed: int | None = None
    hardware_class: str | None = None
    extra_constraints: dict[str, str] = Field(default_factory=dict)


class RunPodInferenceRequest(BaseModel):
    """Payload for shadow inference job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    job_id: str
    artifact_ref: str
    symbols: list[str]
    horizons_ns: list[int]
    feature_snapshot_ref: str | None = None


class RunPodCallbackEnvelope(BaseModel):
    """Wrapper for any result/callback coming back from worker (signed at transport layer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    job_id: str
    worker_id: str
    result_type: str  # e.g. "training_complete", "inference_batch"
    payload: dict[str, Any]  # structured result (dossier, predictions list, etc.)
    received_at_ns: int | None = None


class ArtifactManifest(BaseModel):
    """Metadata for a trained model artifact. Used for pull-based verified import."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    artifact_id: str
    sha256: str
    size_bytes: int
    uri: str | None = None
    model_family: str
    created_at_ns: int
    feature_schema_hash: str
    label_schema_hash: str
    code_git_sha: str | None = None
    lockfile_hash: str | None = None
    container_image_digest: str | None = None


class DatasetManifest(BaseModel):
    """Point-in-time description of the dataset used for training/inference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    feature_schema_hash: str
    label_schema_hash: str
    as_of_ts: int
    universe_hash: str
    row_count: int
    source_vintage_refs: list[str] = Field(default_factory=list)


class ModelDossier(BaseModel):
    """Reproducibility + evaluation record for a candidate model. Core of promotion decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    model_id: str
    artifact_manifest_id: str
    dataset_manifest_id: str
    code_git_sha: str
    lockfile_hash: str
    container_image_digest: str
    random_seed: int | None = None
    hardware_class: str | None = None
    training_metrics: dict[str, float] = Field(default_factory=dict)
    pbo: float | None = Field(default=None, ge=0.0, le=1.0)
    deflated_sharpe: float | None = None
    authority: Authority = Authority.SHADOW_ONLY
    metadata: dict[str, str] = Field(default_factory=dict)


class ShadowPrediction(BaseModel):
    """
    Shadow (non-trading) prediction emitted by external worker.

    CRITICAL INVARIANT: This model (and any subclass or embedding) MUST NOT accept any order/trading
    authority fields. extra="forbid" + explicit test in test_schemas.py enforce this at the schema boundary.
    RunPod workers NEVER control execution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    prediction_id: str
    model_id: str
    symbol: str
    ts_event: int  # decision time (nanoseconds since epoch)
    horizon_ns: int
    direction: float = Field(ge=-1.0, le=1.0)
    magnitude: float | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    authority: Authority = Authority.SHADOW_ONLY
    expected_return: float | None = None
    p_up: float | None = Field(default=None, ge=0.0, le=1.0)
    feature_availability: dict[str, bool] | None = None
    latency_ms: float | None = None
    regime: str | None = None
    model_version: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class PredictionOutcome(BaseModel):
    """Settled outcome for a shadow (or later promoted) prediction. Used by tournament."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    prediction_id: str
    settled_at_ns: int
    realized_return: float
    vs_benchmark: float | None = None
    brier_score: float | None = None
    calibration_bucket: str | None = None
    abnormal_return: float | None = None


class TournamentScore(BaseModel):
    """Aggregated score for a model in the tournament."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    model_id: str
    deflated_sharpe: float | None = None
    pbo: float | None = Field(default=None, ge=0.0, le=1.0)
    rank: int | None = None
    promotion_recommendation: bool = False
    notes: str | None = None


class PromotionReview(BaseModel):
    """Human or automated review record that gates promotion out of shadow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    model_id: str
    dossier_ref: str
    decision: str  # approved / rejected / more_data
    reviewer: str
    rationale: str
    reviewed_at_ns: int


class WorkerHeartbeat(BaseModel):
    """Liveness + load signal from external worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    worker_id: str
    ts_ns: int
    status: str  # idle / busy / error
    current_job_id: str | None = None
    gpu_util: float | None = Field(default=None, ge=0.0, le=1.0)
    memory_util: float | None = None


# --- Skeleton compatibility (TASK-0301) ---


class PlaceholderJob(BaseModel):
    """Retained for backward compat with skeleton tests / __init__ re-export during Wave 1-2 transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    job_id: str
    job_type: str


def get_placeholder_schema() -> type[PlaceholderJob]:
    """Return the placeholder class (kept for skeleton compat)."""
    return PlaceholderJob
