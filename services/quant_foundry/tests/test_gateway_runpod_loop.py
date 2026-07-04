from __future__ import annotations

import time
from typing import Any

from quant_foundry.feature_lake import FeatureRow, FeatureValue
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outbox import JobStatus
from quant_foundry.runpod_client import DispatchResult, DispatchStatus
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
    ShadowPrediction,
)
from quant_foundry.signatures import sign_callback


class RecordingRunPodClient:
    cost_per_dispatch_cents = 0

    def __init__(self, *, endpoint_id: str) -> None:
        self.endpoint_id = endpoint_id
        self.dispatches: list[dict[str, Any]] = []
        self.statuses: dict[str, dict[str, Any]] = {}

    def dispatch(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int | None,
    ) -> DispatchResult:
        runpod_job_id = f"rp-{self.endpoint_id}-{len(self.dispatches) + 1}"
        self.dispatches.append(
            {
                "job_id": job_id,
                "request_payload": request_payload,
                "budget_cents": budget_cents,
                "runpod_job_id": runpod_job_id,
            }
        )
        return DispatchResult(
            job_id=job_id,
            status=DispatchStatus.DISPATCHED,
            runpod_job_id=runpod_job_id,
        )

    def check_status(self, runpod_job_id: str) -> dict[str, Any]:
        return self.statuses.get(runpod_job_id, {"status": "IN_PROGRESS"})

    def check_health(self) -> dict[str, Any]:
        return {"endpoint_id": self.endpoint_id, "status": "ok"}


def _training_payload(job_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "dataset:training",
        "model_family": "gbm",
        "search_space": {"n_estimators": [64]},
        "random_seed": 7,
        "hardware_class": "runpod-gpu",
        "extra_constraints": {},
    }


def _feature_rows(decision_time: int) -> list[dict[str, Any]]:
    rows = (
        FeatureRow(
            symbol="AAPL",
            event_ts=decision_time - 100,
            decision_time=decision_time,
            features=(
                FeatureValue(name="momentum", value=0.25, observed_at=decision_time - 10),
                FeatureValue(name="volatility", value=0.05, observed_at=decision_time - 10),
            ),
        ),
    )
    return [
        {
            "symbol": row.symbol,
            "event_ts": row.event_ts,
            "decision_time": row.decision_time,
            "features": [
                {
                    "name": fv.name,
                    "value": fv.value,
                    "observed_at": fv.observed_at,
                }
                for fv in row.features
            ],
        }
        for row in rows
    ]


def _inference_payload(job_id: str, *, decision_time: int = 1_000) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "artifact_ref": "artifact:trained",
        "symbols": ["AAPL"],
        "horizons_ns": [3_600_000_000_000],
        "feature_snapshot_ref": "feature-snapshot:live",
        "model_id": "model:qf:train:durable:1",
        "decision_time": decision_time,
        "feature_rows": _feature_rows(decision_time),
        "expected_features": ["momentum", "volatility"],
    }


def _signed_training_output(job_id: str, *, secret: str) -> dict[str, Any]:
    artifact = ArtifactManifest(
        artifact_id="artifact:durable",
        sha256="a" * 64,
        size_bytes=2048,
        uri=None,
        model_family="gbm",
        created_at_ns=time.time_ns(),
        feature_schema_hash="feature-hash",
        label_schema_hash="label-hash",
        code_git_sha="git-sha",
        lockfile_hash="lock-hash",
        container_image_digest="container-digest",
    )
    dossier = ModelDossier(
        model_id="model:qf:train:durable:1",
        artifact_manifest_id=artifact.artifact_id,
        dataset_manifest_id="dataset:training",
        code_git_sha="git-sha",
        lockfile_hash="lock-hash",
        container_image_digest="container-digest",
        random_seed=7,
        hardware_class="runpod-gpu",
        training_metrics={"accuracy": 0.62, "logloss": 0.49},
        pbo=0.12,
        deflated_sharpe=1.1,
        authority=Authority.SHADOW_ONLY,
        metadata={"model_family": "gbm"},
    )
    envelope = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-training",
        result_type="training_complete",
        payload={
            "model_family": "gbm",
            "dossier": dossier.model_dump(mode="json"),
            "artifact_manifest": artifact.model_dump(mode="json"),
        },
    )
    payload = envelope.model_dump_json().encode("utf-8")
    ts = int(time.time())
    return {
        "callback_payload": payload.decode("utf-8"),
        "callback_signature": sign_callback(payload, secret=secret, ts=ts, job_id=job_id),
        "callback_ts": ts,
    }


def _signed_inference_output(job_id: str, *, secret: str) -> dict[str, Any]:
    prediction = ShadowPrediction(
        prediction_id="pred:durable:1",
        model_id="model:qf:train:durable:1",
        symbol="AAPL",
        ts_event=1_000,
        horizon_ns=3_600_000_000_000,
        direction=0.42,
        confidence=0.74,
        authority=Authority.SHADOW_ONLY,
        p_up=0.61,
        feature_availability={"AAPL": True},
        latency_ms=3.5,
    )
    envelope = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-inference",
        result_type="inference_batch",
        payload={"predictions": [prediction.model_dump(mode="json")]},
    )
    payload = envelope.model_dump_json().encode("utf-8")
    ts = int(time.time())
    return {
        "callback_payload": payload.decode("utf-8"),
        "callback_signature": sign_callback(payload, secret=secret, ts=ts, job_id=job_id),
        "callback_ts": ts,
    }


def test_runpod_jobs_route_to_training_and_inference_endpoints(tmp_path) -> None:
    secret = "runpod-route-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    inference_client = RecordingRunPodClient(endpoint_id="infer-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": training_client,
            "inference": inference_client,
        },
    )

    train_job = "qf:train:route:1"
    infer_job = "qf:infer:route:1"
    gateway.create_job(
        job_id=train_job,
        job_type="training",
        idempotency_key="idem-train-route",
        request_payload=_training_payload(train_job),
    )
    gateway.create_job(
        job_id=infer_job,
        job_type="inference",
        idempotency_key="idem-infer-route",
        request_payload=_inference_payload(infer_job),
    )

    assert [call["job_id"] for call in training_client.dispatches] == [train_job]
    assert [call["job_id"] for call in inference_client.dispatches] == [infer_job]
    assert gateway.outbox.get(train_job).runpod_endpoint_id == "train-endpoint"
    assert gateway.outbox.get(infer_job).runpod_endpoint_id == "infer-endpoint"


def test_inference_dispatch_exports_feature_snapshot_for_worker(tmp_path) -> None:
    inference_client = RecordingRunPodClient(endpoint_id="infer-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret="snapshot-secret",
        base_dir=tmp_path / "qf",
        runpod_clients={"inference": inference_client},
    )

    job_id = "qf:infer:snapshot:1"
    gateway.create_job(
        job_id=job_id,
        job_type="inference",
        idempotency_key="idem-snapshot",
        request_payload=_inference_payload(job_id, decision_time=2_000),
    )

    dispatched = inference_client.dispatches[0]["request_payload"]
    assert set(dispatched) >= {"request", "snapshot", "model_id"}
    assert dispatched["request"]["job_id"] == job_id
    assert "feature_rows" not in dispatched["request"]
    assert dispatched["snapshot"]["symbols"] == ["AAPL"]
    assert dispatched["snapshot"]["features"]["AAPL"] == [0.25, 0.05]
    assert dispatched["snapshot"]["availability"]["AAPL"] is True
    assert dispatched["snapshot"]["ts_event"] == 2_000


def test_runpod_training_poll_ingests_callback_into_durable_dossier_registry(tmp_path) -> None:
    secret = "runpod-poll-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
    )

    job_id = "qf:train:durable:1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key="idem-durable-train",
        request_payload=_training_payload(job_id),
    )
    runpod_job_id = training_client.dispatches[0]["runpod_job_id"]
    training_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_training_output(job_id, secret=secret),
    }

    receipts = gateway.poll_runpod_results()

    assert receipts[0]["result"] == "processed"
    assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED
    dossiers = gateway.list_dossiers()
    assert [dossier["model_id"] for dossier in dossiers] == ["model:qf:train:durable:1"]
    assert dossiers[0]["artifact_sha256"] == "a" * 64
    assert dossiers[0]["training_metrics"]["pbo"] == 0.12


def test_runpod_inference_poll_ingests_callback_into_durable_shadow_ledger(tmp_path) -> None:
    secret = "runpod-infer-secret"
    inference_client = RecordingRunPodClient(endpoint_id="infer-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"inference": inference_client},
    )

    job_id = "qf:infer:durable:1"
    gateway.create_job(
        job_id=job_id,
        job_type="inference",
        idempotency_key="idem-durable-infer",
        request_payload=_inference_payload(job_id),
    )
    runpod_job_id = inference_client.dispatches[0]["runpod_job_id"]
    inference_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_inference_output(job_id, secret=secret),
    }

    receipts = gateway.poll_runpod_results()

    assert receipts[0]["result"] == "processed"
    assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED
    health = gateway.shadow_health()
    assert health["prediction_count"] == 1
    assert health["models_running"] == 1
    assert health["feature_availability"] == 1.0


# --------------------------------------------------------------------------- #
# Todo 15: scheduler polling produces durable health state                    #
# --------------------------------------------------------------------------- #


_VALID_CIRCUIT_BREAKER_STATES = frozenset({"closed", "half_open", "open"})


def _metrics_path(gateway: QuantFoundryGateway) -> Any:
    """Return the callback_metrics.jsonl path the gateway writes to."""
    return gateway.callback_metrics_store().metrics_dir / "callback_metrics.jsonl"


def _settled_callback_job_ids(gateway: QuantFoundryGateway) -> set[str]:
    """Job ids that have a settled (accepted or rejected) callback receipt.

    A settled receipt is an event in ``callback_metrics.jsonl`` whose event
    is ``accepted`` or ``rejected``. We map back to job ids via the outbox:
    a job is "settled" if it is no longer RUNNING (i.e. COMPLETED or FAILED),
    which is exactly the condition the poll loop enforces before recording
    an accepted/rejected event.
    """
    store = gateway.callback_metrics_store()
    # ``CallbackMetricsStore`` does not carry job_id, so "settled" is
    # defined structurally: a job is settled if its outbox status is no
    # longer RUNNING. The metrics file is the durable receipt that at
    # least one accepted/rejected event was recorded for the poll batch.
    settled: set[str] = set()
    for rec in gateway.outbox.list():
        if rec.status != JobStatus.RUNNING:
            settled.add(rec.job_id)
    # The metrics file must exist (durable receipt) for the batch to count.
    if not store.has_any_events():
        return set()
    return settled


def test_poll_records_durable_health(tmp_path) -> None:
    """Happy path: poll with all-signed callbacks leaves durable health.

    After ``poll_runpod_results``:
      1. No RUNNING job lacks a settled callback receipt.
      2. ``circuit_breaker_state`` is a non-None enum string.
      3. ``callback_rejection_rate`` is numeric (0.0 — no rejections).
    """
    secret = "runpod-durable-health-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
    )

    job_id = "qf:train:durable-health:1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key="idem-durable-health",
        request_payload=_training_payload(job_id),
    )
    runpod_job_id = training_client.dispatches[0]["runpod_job_id"]
    training_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_training_output(job_id, secret=secret),
    }

    receipts = gateway.poll_runpod_results()

    assert receipts[0]["result"] == "processed"
    assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED

    # 1. No RUNNING job without a settled callback receipt.
    running = gateway.outbox.list(status=JobStatus.RUNNING)
    settled = _settled_callback_job_ids(gateway)
    orphans = [rec.job_id for rec in running if rec.job_id not in settled]
    assert orphans == [], f"RUNNING jobs without settled receipt: {orphans}"

    # Durable receipt: the metrics JSONL file exists on disk.
    assert _metrics_path(gateway).is_file()

    # 2. circuit_breaker_state is a non-None member of the enum.
    health = gateway.shadow_health()
    assert health["circuit_breaker_state"] in _VALID_CIRCUIT_BREAKER_STATES
    assert health["circuit_breaker_state"] is not None

    # 3. callback_rejection_rate is numeric; happy path == 0.0.
    rate = health["callback_rejection_rate"]
    assert rate is not None
    assert isinstance(rate, float | int)
    assert rate == 0.0


def test_poll_records_durable_health_with_rejection(tmp_path) -> None:
    """Failure path: a bad-signature callback records a rejection.

    After ``poll_runpod_results`` with one bad-signature callback:
      1. ``callback_rejection_rate`` is numeric and > 0.
      2. A subsequent poll still returns a numeric rate (no division by
         zero, no crash) and a valid ``circuit_breaker_state``.
    """
    secret = "runpod-durable-health-reject-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
    )

    job_id = "qf:train:durable-health-reject:1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key="idem-durable-health-reject",
        request_payload=_training_payload(job_id),
    )
    runpod_job_id = training_client.dispatches[0]["runpod_job_id"]

    # Build a signed output then corrupt the signature -> bad_signature reject.
    bad_output = _signed_training_output(job_id, secret=secret)
    bad_output["callback_signature"] = "0" * 128
    training_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": bad_output,
    }

    receipts = gateway.poll_runpod_results()

    assert receipts[0]["ok"] is False
    assert receipts[0]["error_code"] == "bad_signature"
    # A bad-signature callback is fail-closed in ``receive_callback``: it
    # records a ``rejected`` metric but does NOT transition the outbox
    # (the job stays RUNNING so a future legitimate callback can still
    # settle it). The durable trace lives in callback_metrics.jsonl.
    assert gateway.outbox.get(job_id).status == JobStatus.RUNNING
    assert _metrics_path(gateway).is_file()

    health = gateway.shadow_health()
    assert health["circuit_breaker_state"] in _VALID_CIRCUIT_BREAKER_STATES
    rate = health["callback_rejection_rate"]
    assert rate is not None
    assert isinstance(rate, float | int)
    assert rate > 0.0

    # Subsequent poll (the job is still RUNNING, so the poller re-attempts
    # and re-rejects) must not crash and must stay numeric — no division
    # by zero, no exception.
    second_receipts = gateway.poll_runpod_results()
    assert all(r.get("error_code") == "bad_signature" for r in second_receipts)
    health2 = gateway.shadow_health()
    rate2 = health2["callback_rejection_rate"]
    assert rate2 is not None
    assert isinstance(rate2, float | int)
    assert rate2 > 0.0
    assert health2["circuit_breaker_state"] in _VALID_CIRCUIT_BREAKER_STATES


# ---------------------------------------------------------------------------
# Worker status file consumption (heartbeats + stale detection)
# ---------------------------------------------------------------------------


def test_heartbeats_empty_without_status_dir(tmp_path: Any) -> None:
    """heartbeats() returns [] when no worker_status_dir is configured."""
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret="secret",
        base_dir=tmp_path / "qf",
        runpod_clients={"training": RecordingRunPodClient(endpoint_id="t")},
    )
    assert gateway.heartbeats() == []
    assert gateway.detect_stale_workers() == []


def test_heartbeats_reads_status_files(tmp_path: Any) -> None:
    """heartbeats() reads JSON status files from the configured directory."""
    import json as _json

    status_dir = tmp_path / "worker_status"
    status_dir.mkdir(parents=True)
    # Write two status files.
    (status_dir / "job-1.json").write_text(
        _json.dumps({"job_id": "job-1", "status": "training", "heartbeat_at": 1000.0}),
        encoding="utf-8",
    )
    (status_dir / "job-2.json").write_text(
        _json.dumps({"job_id": "job-2", "status": "completed", "heartbeat_at": 2000.0}),
        encoding="utf-8",
    )

    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret="secret",
        base_dir=tmp_path / "qf",
        runpod_clients={"training": RecordingRunPodClient(endpoint_id="t")},
        worker_status_dir=status_dir,
    )
    hbs = gateway.heartbeats()
    job_ids = {h["job_id"] for h in hbs}
    assert job_ids == {"job-1", "job-2"}


def test_detect_stale_workers_finds_old_heartbeats(tmp_path: Any) -> None:
    """detect_stale_workers() returns only active jobs with old heartbeats."""
    import json as _json

    status_dir = tmp_path / "worker_status"
    status_dir.mkdir(parents=True)
    # Stale training job.
    (status_dir / "job-stale.json").write_text(
        _json.dumps(
            {"job_id": "job-stale", "status": "training", "heartbeat_at": 1000.0},
        ),
        encoding="utf-8",
    )
    # Old but completed — should NOT be flagged stale.
    (status_dir / "job-done.json").write_text(
        _json.dumps(
            {"job_id": "job-done", "status": "completed", "heartbeat_at": 1000.0},
        ),
        encoding="utf-8",
    )

    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret="secret",
        base_dir=tmp_path / "qf",
        runpod_clients={"training": RecordingRunPodClient(endpoint_id="t")},
        worker_status_dir=status_dir,
        stale_threshold_seconds=60.0,
    )
    # We can't control time.time() easily, but heartbeat_at=1000.0 is
    # always in the past, so the job should be stale.
    stale = gateway.detect_stale_workers()
    stale_ids = {s["job_id"] for s in stale}
    assert "job-stale" in stale_ids
    assert "job-done" not in stale_ids


def test_detect_stale_workers_skips_corrupt_files(tmp_path: Any) -> None:
    """detect_stale_workers() silently skips corrupt JSON files."""
    status_dir = tmp_path / "worker_status"
    status_dir.mkdir(parents=True)
    (status_dir / "corrupt.json").write_text("{broken", encoding="utf-8")

    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret="secret",
        base_dir=tmp_path / "qf",
        runpod_clients={"training": RecordingRunPodClient(endpoint_id="t")},
        worker_status_dir=status_dir,
    )
    assert gateway.heartbeats() == []
    assert gateway.detect_stale_workers() == []


# ---------------------------------------------------------------------------
# sweep_stale_workers — auto-fail stale RUNNING jobs
# ---------------------------------------------------------------------------


def _write_status_file(status_dir: Any, job_id: str, *, status: str, heartbeat_at: float) -> None:
    import json as _json

    (status_dir / f"{job_id}.json").write_text(
        _json.dumps({"job_id": job_id, "status": status, "heartbeat_at": heartbeat_at}),
        encoding="utf-8",
    )


def _make_runpod_gateway(
    tmp_path: Any, *, status_dir: Any, stale_threshold_seconds: float = 60.0
) -> QuantFoundryGateway:
    return QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret="secret",
        base_dir=tmp_path / "qf",
        runpod_clients={"training": RecordingRunPodClient(endpoint_id="t")},
        worker_status_dir=status_dir,
        stale_threshold_seconds=stale_threshold_seconds,
    )


def test_sweep_stale_workers_fails_running_jobs(tmp_path: Any) -> None:
    """sweep_stale_workers() marks RUNNING stale jobs as FAILED."""
    status_dir = tmp_path / "worker_status"
    status_dir.mkdir(parents=True)
    gateway = _make_runpod_gateway(tmp_path, status_dir=status_dir)

    job_id = "stale-sweep-1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key="idem-stale-sweep",
        request_payload=_training_payload(job_id),
    )
    # The dispatcher transitions the job to RUNNING; confirm and write a
    # stale heartbeat file.
    assert gateway.outbox.get(job_id).status == JobStatus.RUNNING
    _write_status_file(status_dir, job_id, status="training", heartbeat_at=1000.0)

    receipts = gateway.sweep_stale_workers()

    assert len(receipts) == 1
    assert receipts[0]["job_id"] == job_id
    assert receipts[0]["error_code"] == "worker_heartbeat_stale"
    rec = gateway.outbox.get(job_id)
    assert rec.status == JobStatus.FAILED
    assert rec.error_code == "worker_heartbeat_stale"
    assert "heartbeat stale" in rec.error_summary


def test_sweep_stale_workers_skips_completed_jobs(tmp_path: Any) -> None:
    """sweep_stale_workers() does not touch terminal outbox jobs."""
    status_dir = tmp_path / "worker_status"
    status_dir.mkdir(parents=True)
    gateway = _make_runpod_gateway(tmp_path, status_dir=status_dir)

    # A completed job with a stale heartbeat file.
    done_job = "stale-done-1"
    gateway.create_job(
        job_id=done_job,
        job_type="training",
        idempotency_key="idem-stale-done",
        request_payload=_training_payload(done_job),
    )
    gateway.outbox.update_status(done_job, JobStatus.COMPLETED)
    _write_status_file(status_dir, done_job, status="training", heartbeat_at=1000.0)

    # A failed job with a stale heartbeat file.
    failed_job = "stale-failed-1"
    gateway.create_job(
        job_id=failed_job,
        job_type="training",
        idempotency_key="idem-stale-failed",
        request_payload=_training_payload(failed_job),
    )
    gateway.outbox.update_status(failed_job, JobStatus.FAILED)
    _write_status_file(status_dir, failed_job, status="training", heartbeat_at=1000.0)

    receipts = gateway.sweep_stale_workers()

    assert receipts == []
    assert gateway.outbox.get(done_job).status == JobStatus.COMPLETED
    assert gateway.outbox.get(failed_job).status == JobStatus.FAILED


def test_sweep_stale_workers_no_status_dir(tmp_path: Any) -> None:
    """sweep_stale_workers() returns [] when no status dir is configured."""
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret="secret",
        base_dir=tmp_path / "qf",
        runpod_clients={"training": RecordingRunPodClient(endpoint_id="t")},
    )
    assert gateway.sweep_stale_workers() == []
