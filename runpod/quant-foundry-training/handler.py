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

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

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

from quant_foundry.runpod_training import (  # noqa: E402
    LocalTrainer,
    RunPodTrainingHandler,
    TrainingFailure,
)
from quant_foundry.schemas import RunPodTrainingRequest  # noqa: E402
from quant_foundry.signatures import sign_callback  # noqa: E402

# Phase 1 / T-1.1: typed artifact result contract. Imported at module
# level — ``quant_foundry.real_trainer`` is importable without ML deps
# (lightgbm/numpy are imported lazily inside ``train()``).
from quant_foundry.real_trainer import (  # noqa: E402
    TypedArtifactResult,
    build_artifact_result,
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
        return {
            "error_code": "bad_request",
            "error_summary": "ingest_media_sentiment requires dataset_id",
            "job_id": input_data.get("job_id"),
        }

    start_ns = input_data.get("start_ns")
    end_ns = input_data.get("end_ns")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int):
        return {
            "error_code": "bad_request",
            "error_summary": "ingest_media_sentiment requires start_ns and end_ns as integers",
            "job_id": input_data.get("job_id"),
        }

    output_dir = input_data.get("output_dir", "")
    if not output_dir:
        return {
            "error_code": "bad_request",
            "error_summary": "ingest_media_sentiment requires output_dir",
            "job_id": input_data.get("job_id"),
        }

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
        return {
            "error_code": "ingestion_failed",
            "error_summary": str(exc),
            "job_id": input_data.get("job_id"),
        }

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


def _build_trainer(n_folds: int = 3) -> Any:
    """Select the trainer based on the QUANT_FOUNDRY_USE_REAL_TRAINER env var.

    When ``QUANT_FOUNDRY_USE_REAL_TRAINER=true``, use ``RealLightGBMTrainer``
    which trains a real LightGBM model with walk-forward validation and
    produces real metrics (accuracy, logloss, brier, PBO, Sharpe, drawdown).

    Otherwise, fall back to ``LocalTrainer`` (the deterministic stub) for
    backward-compatible testing and contract proofs.
    """
    use_real = os.environ.get("QUANT_FOUNDRY_USE_REAL_TRAINER", "").lower() == "true"
    if use_real:
        from quant_foundry.real_trainer import RealLightGBMTrainer

        return RealLightGBMTrainer(n_folds=n_folds)
    return LocalTrainer()


def _heartbeat_during_training(
    job_id: str, interval: float = 10.0
) -> threading.Event:
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


def handler(event: dict[str, Any]) -> dict[str, Any]:
    """RunPod serverless handler entrypoint.

    Args:
        event: RunPod event dict. `event["input"]` must be a dict matching
            RunPodTrainingRequest.

    Returns:
        On success: dict with callback_payload, callback_signature,
        callback_ts, job_id, artifact_id, dossier_id.
        On failure: dict with error_code, error_summary, job_id.
    """
    input_data = event.get("input") if isinstance(event, dict) else None
    if not isinstance(input_data, dict):
        return {
            "error_code": "bad_request",
            "error_summary": "event['input'] must be a dict matching RunPodTrainingRequest",
            "job_id": None,
        }

    # Callback-secret canary: bypasses the training pipeline entirely.
    # The API dispatches this to verify that the worker shares the same
    # QUANT_FOUNDRY_CALLBACK_SECRET. See gateway.runpod_canary().
    if input_data.get("task") == "callback_secret_canary":
        return _handle_canary(input_data)

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
            return {
                "error_code": "bad_request",
                "error_summary": "write_volume requires volume_path and chunk_data",
                "job_id": input_data.get("job_id"),
            }
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
            files.append({
                "name": p.name,
                "size_bytes": p.stat().st_size if p.is_file() else 0,
                "is_dir": p.is_dir(),
            })
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
        return _handle_ingest_media_sentiment(input_data)

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

    try:
        req = RunPodTrainingRequest.model_validate(input_data)
    except Exception as exc:
        return {
            "error_code": "schema_validation_failed",
            "error_summary": str(exc),
            "job_id": input_data.get("job_id"),
        }

    if isinstance(inline_csv, str) and inline_csv.strip():
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
    trainer = _build_trainer(n_folds=int(n_folds) if n_folds else 3)
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
        return {
            "error_code": exc.error_code,
            "error_summary": exc.error_summary,
            "job_id": req.job_id,
        }
    finally:
        heartbeat_stop.set()

    write_status(req.job_id, "completed", artifact_id=result.artifact_id)

    # --- Phase 1 / T-1.1: resolve the typed artifact result -----------------
    # The real trainer stashes a TypedArtifactResult on itself; the local
    # (canary) trainer does not, so we synthesize a tiny inline-bytes
    # result for canary tests. A successful real training job with no
    # artifact is a contract violation — fail closed.
    typed_artifact: TypedArtifactResult | None = getattr(
        trainer, "last_artifact_result", None
    )
    model_bytes: bytes | None = getattr(trainer, "last_model_bytes", None)

    is_real_trainer = not isinstance(trainer, LocalTrainer)

    if is_real_trainer:
        # Fail closed: a successful real training job MUST produce an
        # artifact (acceptance criterion: trainer success without
        # artifact fails).
        if typed_artifact is None or not model_bytes:
            return {
                "error_code": "artifact_missing",
                "error_summary": (
                    "successful training produced no typed artifact result "
                    "(fail closed: artifact URI/hash/size required)"
                ),
                "job_id": req.job_id,
            }
    else:
        # Canary / local-stub path: keep tiny inline bytes only for
        # canary tests (never persisted to a real artifact URI unless
        # output_prefix is set). Build a typed result so the contract
        # shape is identical across modes.
        if typed_artifact is None:
            canary_bytes = (
                b"canary-model-stub:" + req.job_id.encode("utf-8")
            )
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
                return {
                    "error_code": "artifact_missing",
                    "error_summary": f"cannot build canary artifact result: {exc}",
                    "job_id": req.job_id,
                }
            model_bytes = typed_artifact.model_bytes

    # If output_prefix is set, persist the model artifact + dossier to the
    # network volume. Fail the job if the artifact write fails or the sha
    # does not match (acceptance: artifact sha mismatch fails; fail the
    # job if artifact write fails).
    artifact_uri: str | None = None
    if output_prefix:
        if typed_artifact is None or not model_bytes:
            return {
                "error_code": "artifact_missing",
                "error_summary": (
                    "output_prefix set but no artifact bytes to write "
                    "(fail closed)"
                ),
                "job_id": req.job_id,
            }
        try:
            out_dir = Path(output_prefix)
            out_dir.mkdir(parents=True, exist_ok=True)
            callback_json = json.loads(result.callback_payload.decode("utf-8"))
            payload = callback_json.get("payload", {})
            artifact_manifest = payload.get("artifact_manifest", {})
            dossier_data = payload.get("dossier", {})
            (out_dir / "callback_envelope.json").write_text(
                json.dumps(callback_json, indent=2), encoding="utf-8"
            )
            (out_dir / "artifact_manifest.json").write_text(
                json.dumps(artifact_manifest, indent=2), encoding="utf-8"
            )
            (out_dir / "dossier.json").write_text(
                json.dumps(dossier_data, indent=2), encoding="utf-8"
            )
            # Write the model artifact bytes (typed field, not getattr).
            model_path = out_dir / "model.pkl"
            model_path.write_bytes(model_bytes)
            # Verify the written bytes match the declared sha256. A
            # mismatch is a terminal failure (acceptance: artifact sha
            # mismatch fails).
            if not typed_artifact.verify_bytes(model_bytes):
                return {
                    "error_code": "artifact_sha_mismatch",
                    "error_summary": (
                        "artifact sha256 mismatch: recomputed hash does not "
                        f"match declared {typed_artifact.artifact_sha256}"
                    ),
                    "job_id": req.job_id,
                }
            artifact_uri = model_path.as_uri()
        except Exception as exc:
            # Fail the job if the artifact write fails (no longer
            # best-effort — an unverified/unpersisted artifact is a
            # contract violation for production/research runs).
            return {
                "error_code": "artifact_write_failed",
                "error_summary": f"failed to persist artifact to volume: {exc}",
                "job_id": req.job_id,
            }

    # Bind the artifact URI onto the typed result (immutable → rebuild).
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

    return {
        "job_id": req.job_id,
        "callback_payload": result.callback_payload.decode("utf-8"),
        "callback_signature": result.callback_signature,
        "callback_ts": result.callback_ts,
        "artifact_id": result.artifact_id,
        "dossier_id": result.dossier_id,
        "output_prefix": output_prefix,
        # Phase 1 / T-1.1: typed artifact result (uri/hash/size/format/
        # kind/loader_family + manifest hashes). Present on every
        # successful training job; the dispatcher/trusted verifier uses
        # it to fetch + re-verify the artifact.
        "artifact_result": (
            {
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
            }
            if typed_artifact is not None
            else None
        ),
    }


# RunPod's serverless module loader looks for a `handler` function at the
# top level. When running on RunPod serverless, use the runpod SDK to start
# the worker. When run as a script (local testing), accept JSON on stdin.
if __name__ == "__main__":  # pragma: no cover
    import sys
    import traceback

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
